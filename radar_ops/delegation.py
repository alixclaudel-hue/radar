"""Agrégation LECTURE SEULE des journaux de délégation et de développement.

Répond à une question et une seule : qu'est-ce que le courtier multi-fournisseurs
(`scripts/ai_broker.py`) a réellement délégué, et à quel coût ? OpenRouter,
Gemini et DeepSeek partagent le même journal — la page ne présuppose aucun
fournisseur en particulier. Trois sources, écrites par des processus qui
ignorent tout de cette page :

- `gemini-receipts.jsonl`, une ligne par appel du courtier, écrite par
  `scripts/ai_query.py`. Le nom est **historique** (le courtier a commencé sur
  Gemini seul) — le fichier porte aujourd'hui les reçus de tous les
  fournisseurs, avec un champ `provider` qui les distingue ;
- `telemetry.jsonl`, une ligne par événement de session, écrite par
  `scripts/hooks/telemetry.py` ;
- `ai_health.json`, l'état du disjoncteur par (fournisseur, modèle), écrit par
  `scripts/ai/health.py`.

Les jetons affichés viennent de `usageMetadata`, rapporté par l'API — une
mesure. Aucune estimation d'économie côté Claude n'est calculée ici : ce
serait un contrefactuel, et le publier à côté de vraies mesures le ferait
passer pour l'une d'elles.

Les reçus récents déclarent, en clair, le quota du jour au moment de l'appel :
c'est la seule source du « reste-t-il des appels gratuits ? » affiché plus bas,
et elle n'invente aucun plafond qu'un fournisseur ne publie pas. Le disjoncteur
est lu par ses propres fonctions, jamais réimplémenté : une page qui recalculerait
un cooldown à sa façon finirait par nommer des modèles que le routage continue
d'essayer.

Les « occasions manquées » sont, comme le reste, un décompte de journaux : une
session sans le moindre reçu, un prompt dont le vocabulaire appelle une lecture
ou un résumé et qui reste sans reçu, des lectures natives que le mode `context`
couvrirait. Rien n'y est déduit d'une intention ; c'est un signalement à
inspecter, et le gabarit le dit dans ces mots.

Les lignes anciennes n'ont pas les champs récents (jetons, modèle, durée) : un
champ absent vaut 0 plutôt que de disqualifier la ligne, sans quoi le tableau
de bord ignorerait tout l'historique antérieur à l'instrumentation.

Le fenêtrage temporel (`snapshot_windowed`, `resolve_period`, `bucket_by_time`,
`per_day_breakdown`) compte les jours en heure locale Paris : un appel à 01h du
matin appartient à « aujourd'hui » pour l'utilisateur, pas à la veille en UTC.

`by_model` agrège par identifiant de modèle brut (`gemini-3.5-flash`,
`nvidia/nemotron-3-ultra`, `deepseek-v3.2`, ...) : la charte visuelle assigne
une couleur stable à chacun, quel que soit le fournisseur derrière.
"""
import bisect
import collections
import datetime
import json
import os

from zoneinfo import ZoneInfo

from scripts.ai import health as ai_health

RECEIPTS_NAME = "gemini-receipts.jsonl"
EVENTS_NAME = "telemetry.jsonl"
HEALTH_NAME = ai_health.STATE_NAME
TOP_TOOLS = 5
MAX_SESSIONS = 20
MAX_OCCASIONS = 5

# Outils natifs dont `context` ou `search` ferait le travail en un seul appel.
# `Bash` en est exclu : plus de la moitié de ses appels sont des tests, du git
# ou du docker, qu'aucun mode de délégation ne remplace.
OUTILS_LECTURE = frozenset({"Read", "Grep", "Glob"})

# Marqueurs de surface, pas d'intention : le vocabulaire des skills qui
# délèguent. Un prompt qui en porte un est signalé pour inspection, jamais
# jugé en faute — le mot peut tomber dans une phrase qui ne demande rien.
MOTIFS_DELEGABLES = ("résume", "resume", "lire", "lecture", "lis ", "explique",
                     "cherche", "trouve", "diagramme", "brief", "audit",
                     "compare", "analyse", "analyze", "review", "tradui",
                     "transcri", "diag")

# Fenêtre d'appariement prompt -> reçu, large à dessein : mieux vaut manquer
# une occasion réelle que d'en inventer une. Un appel délégué prend quelques
# secondes ; dix minutes couvrent une chaîne d'essais et de replis.
DELAI_APPARIEMENT_S = 600.0


def log_dir(data_root):
    """Dossier des journaux, `RADAR_OPS_TELEMETRY_DIR` prioritaire.

    Le défaut est `<data>/ops` : le conteneur `radar-ops` ne voit que le volume
    de données, jamais le checkout git d'où les journaux sont écrits."""
    override = (os.environ.get("RADAR_OPS_TELEMETRY_DIR") or "").strip()
    return override or os.path.join(data_root, "ops")


def _receipts_path(data_root):
    """Chemin des reçus du courtier — directement dans le dossier ops.

    Depuis la suppression du transport git (commit e35af25), `ai_query.py`
    écrit ici en direct via `RADAR_TELEMETRY_DIR` (même override que les
    événements de `log_dir()`) : plus de copie fusionnée à attendre, plus de
    repli `CLAUDE_PROJECT_DIR` — cette variable n'est jamais définie dans ce
    conteneur, qui ne voit que le volume de données, jamais un checkout git.
    Un ancien repli qui préférait une copie figée existante avait fini par
    masquer indéfiniment les reçus réels une fois le transport supprimé."""
    return os.path.join(log_dir(data_root), RECEIPTS_NAME)


def _health_path(data_root):
    """Chemin de l'état du disjoncteur — à côté du ledger d'usage.

    `RADAR_AI_USAGE_DIR` d'abord (le banc pointe cette variable vers un dossier
    jetable, pour qu'une mesure hors ligne ne pèse pas sur la vraie page), sinon
    `<ops>/ai` : c'est là que `quota.usage_dir()` écrit sur le VPS, où les reçus
    sont déjà sous `<ops>`. On n'appelle pas `usage_dir()` ici parce que, sans
    `RADAR_TELEMETRY_DIR`, elle retombe sur un dossier relatif au dépôt — que le
    conteneur de diagnostic ne voit pas."""
    override = (os.environ.get("RADAR_AI_USAGE_DIR") or "").strip()
    base = override or os.path.join(log_dir(data_root), "ai")
    return os.path.join(base, HEALTH_NAME)


def _num(value):
    """Valeur numérique, ou None si le champ n'en porte pas.

    Distinct de « vaut 0 » : une durée absente ne doit pas tirer une médiane
    vers le bas comme le ferait une mesure à zéro milliseconde."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(row, key):
    n = _num(row.get(key))
    return int(n) if n is not None else 0


def read_jsonl(path, since_ts=None, limit=None):
    """Lignes valides du journal, filtrées sur l'horodatage.

    Une ligne illisible est ignorée sans bruit : ces fichiers sont lus pendant
    qu'un autre processus y écrit, et la dernière ligne peut être tronquée au
    moment précis de la lecture. Faire échouer la page pour cela reviendrait à
    la rendre indisponible d'autant plus souvent qu'il se passe des choses."""
    rows = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                ts = _num(row.get("ts"))
                if ts is None:
                    continue
                if since_ts is not None and ts < since_ts:
                    continue
                row["ts"] = ts
                rows.append(row)
    except OSError:
        return []
    if limit is not None and limit > 0:
        return rows[-limit:]
    return rows


def _median(values):
    """Médiane, ou None sur une liste vide — l'absence de mesure n'est pas 0."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n % 2:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0


def _group(receipts, field, label, unknown):
    """Agrégat par valeur d'un champ du reçu (`mode`, `model`, …).

    Une seule fonction pour les deux regroupements : deux copies auraient fini
    par diverger sur la définition d'une colonne, et le tableau de bord aurait
    affiché deux totaux différents pour la même chose."""
    buckets = collections.defaultdict(
        lambda: {"calls": 0, "ok": 0, "err": 0, "prompt_tokens": 0,
                 "output_tokens": 0, "total_tokens": 0, "fallbacks": 0,
                 "_ms": []})
    for r in receipts:
        b = buckets[str(r.get(field) or unknown)]
        b["calls"] += 1
        if r.get("status") == "ok":
            b["ok"] += 1
        else:
            b["err"] += 1
        for k in ("prompt_tokens", "output_tokens", "total_tokens", "fallbacks"):
            b[k] += _int(r, k)
        ms = _num(r.get("elapsed_ms"))
        if ms is not None:
            b["_ms"].append(ms)

    out = []
    for key, b in buckets.items():
        row = {k: v for k, v in b.items() if k != "_ms"}
        row[label] = key
        row["median_ms"] = _median(b["_ms"])
        out.append(row)
    out.sort(key=lambda r: (-r["total_tokens"], -r["calls"], r[label]))
    return out


def _by_day(receipts, now_ts, days):
    """Volume jour par jour en UTC, trous compris.

    Les jours sans appel sont présents à zéro : une courbe qui saute les jours
    vides raccourcit visuellement les périodes d'inactivité, donc ment."""
    counts = collections.Counter()
    tokens = collections.Counter()
    for r in receipts:
        day = datetime.datetime.fromtimestamp(
            r["ts"], datetime.timezone.utc).date()
        counts[day] += 1
        tokens[day] += _int(r, "total_tokens")

    today = datetime.datetime.fromtimestamp(
        now_ts, datetime.timezone.utc).date()
    return [{"date": (today - datetime.timedelta(days=n)).isoformat(),
             "calls": counts[today - datetime.timedelta(days=n)],
             "total_tokens": tokens[today - datetime.timedelta(days=n)]}
            for n in range(days - 1, -1, -1)]


def _stamp(ts):
    """Horodatage lisible en heure locale du serveur, ou None."""
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts).strftime("%d/%m %H:%M")


def _sessions(events):
    """Une ligne par session de développement, la plus récente en tête."""
    seen = collections.defaultdict(
        lambda: {"first_ts": None, "last_ts": None, "prompts": 0,
                 "tool_calls": 0, "tool_errs": 0,
                 "_tools": collections.Counter()})
    for ev in events:
        s = seen[str(ev.get("session") or "inconnue")]
        ts = ev["ts"]
        if s["first_ts"] is None or ts < s["first_ts"]:
            s["first_ts"] = ts
        if s["last_ts"] is None or ts > s["last_ts"]:
            s["last_ts"] = ts
        kind = ev.get("kind")
        if kind == "prompt":
            s["prompts"] += 1
        elif kind == "tool":
            s["tool_calls"] += 1
            if ev.get("ok") is False:
                s["tool_errs"] += 1
            name = ev.get("tool")
            if name:
                s["_tools"][str(name)] += 1

    rows = []
    for sid, s in seen.items():
        row = {k: v for k, v in s.items() if k != "_tools"}
        row["session"] = sid
        row["top_tools"] = [{"name": n, "count": c}
                            for n, c in s["_tools"].most_common(TOP_TOOLS)]
        # Dates formatées ici et pas dans le gabarit : Jinja n'a pas de filtre
        # de date, et une conversion improvisée côté gabarit se répéterait à
        # chaque colonne.
        row["first_at"] = _stamp(s["first_ts"])
        row["last_at"] = _stamp(s["last_ts"])
        rows.append(row)
    rows.sort(key=lambda r: r["last_ts"], reverse=True)
    return rows[:MAX_SESSIONS]


def _quota_du_jour(receipts):
    """Dernier quota déclaré, par fournisseur — un compteur, pas une constante.

    Les reçus antérieurs à l'enrichissement n'ont pas de bloc `quota` : ils sont
    ignorés au lieu d'être lus comme « zéro appel utilisé », ce qui ferait
    passer un fournisseur intact pour un fournisseur intact par chance. Un
    plafond absent reste absent (`None`) : afficher 0 ferait passer une limite
    inconnue pour une limite épuisée."""
    dernier = {}
    for r in receipts:
        bloc = r.get("quota")
        if not isinstance(bloc, dict):
            continue
        ts = _num(r.get("ts")) or 0.0
        fournisseur = str(r.get("provider") or "inconnu")
        if fournisseur not in dernier or ts >= dernier[fournisseur][0]:
            dernier[fournisseur] = (ts, bloc)

    rows = []
    for fournisseur, (_, bloc) in dernier.items():
        depense = _num(bloc.get("paid_spent_usd"))
        plafond = _num(bloc.get("paid_cap_usd"))
        rows.append({
            "provider": fournisseur,
            "window": bloc.get("window"),
            "limit": bloc.get("limit"),
            "used": bloc.get("used"),
            "remaining": bloc.get("remaining"),
            "paid_spent_usd": depense,
            "paid_cap_usd": plafond,
            # Le reste payant ne se déduit que si le plafond est publié : c'est
            # la règle du courtier, qui refuse un paiement sans plafond connu.
            "paid_remaining_usd": (round(plafond - depense, 6)
                                   if plafond is not None and depense is not None
                                   else None),
        })
    rows.sort(key=lambda r: r["provider"])
    return rows


def _part_gratuite(receipts):
    """Répartition gratuit / payant, et coût estimé cumulé.

    Le ratio se calcule sur les appels qui **déclarent** leur nature. Y mêler
    les reçus plus anciens, qui ne la déclarent pas, reviendrait à compter une
    absence de mesure comme une mesure : ils sont donc comptés séparément sous
    `undeclared`, et le ratio reste `None` tant qu'aucun appel ne se déclare."""
    free = sum(1 for r in receipts if r.get("paid") is False)
    paid = sum(1 for r in receipts if r.get("paid") is True)
    declares = free + paid
    return {
        "calls": len(receipts),
        "free": free,
        "paid": paid,
        "undeclared": len(receipts) - declares,
        "free_share": (round(free / declares, 3) if declares else None),
        "cost_usd": round(sum(_num(r.get("cost_usd")) or 0.0 for r in receipts), 6),
    }


def _cooldowns(state, now_ts):
    """Modèles écartés en ce moment, cooldowns et retraits confondus.

    On interroge le disjoncteur au lieu de relire ses champs à la main : la page
    doit nommer exactement les modèles que le routeur évite, sinon elle
    expliquerait une absence que le routage ne pratique pas."""
    rows = [{"provider": p, "model": m, "seconds_left": int(round(restant)),
             "retired": False, "reason": ai_health.reason(state, p, m, now_ts)}
            for p, m, restant in ai_health.cooling(state, now_ts)]
    rows += [{"provider": p, "model": m, "seconds_left": None, "retired": True,
              "reason": ai_health.reason(state, p, m, now_ts)}
             for p, m in ai_health.retired(state)]
    return rows


def _occasions_manquees(receipts, events):
    """Ce que la règle « déléguer d'abord » n'a pas attrapé — mesuré, pas jugé.

    Trois signaux, tous tirés des journaux bruts de la fenêtre affichée :

    1. une session où l'utilisateur a écrit au moins une fois sans qu'aucun
       reçu ne tombe pendant son activité ;
    2. un prompt portant un marqueur de tâche déléguable (lecture, résumé,
       brief, diagramme...) resté sans reçu dans la fenêtre d'appariement ;
    3. les lectures natives (`Read`, `Grep`, `Glob`) que le mode `context`
       paverait en un seul appel, sans modèle à réveiller.

    L'appariement se fait par le **temps**, pas par la session : les reçus n'ont
    jamais porté d'identifiant de session. Ce n'est pas une imprécision qu'on
    corrigerait en cherchant mieux — la donnée n'existe pas dans le journal, et
    l'écrire ici évite qu'on invente un appariement plus fin que le réel."""
    recus = sorted(_num(r.get("ts")) or 0.0 for r in receipts)

    def _recu_entre(debut, fin):
        i = bisect.bisect_left(recus, debut)
        return i < len(recus) and recus[i] <= fin

    bornes = {}
    prompts = []
    lectures = collections.Counter()
    for ev in events:
        sid = str(ev.get("session") or "inconnue")
        ts = _num(ev.get("ts")) or 0.0
        b = bornes.setdefault(sid, [None, None, 0])
        if b[0] is None or ts < b[0]:
            b[0] = ts
        if b[1] is None or ts > b[1]:
            b[1] = ts
        kind = ev.get("kind")
        if kind == "prompt":
            b[2] += 1
            prompts.append((ts, sid, str(ev.get("head") or "")))
        elif kind == "tool" and ev.get("tool") in OUTILS_LECTURE:
            lectures[str(ev.get("tool"))] += 1

    actives = [v for v in bornes.values() if v[2]]
    sans_recu = [v for v in actives
                 if not _recu_entre(v[0], (v[1] or v[0]) + DELAI_APPARIEMENT_S)]

    signales = []
    for ts, sid, head in prompts:
        texte = head.strip().lower()
        # `<task-notification>` vient d'une tâche de fond, pas d'une demande :
        # son texte peut porter n'importe quel marqueur, et le compter
        # signalerait une occasion qui n'a jamais été posée.
        if not texte or texte.startswith("<task-notification"):
            continue
        marqueur = next((m for m in MOTIFS_DELEGABLES if m in texte), None)
        if marqueur and not _recu_entre(ts, ts + DELAI_APPARIEMENT_S):
            signales.append((ts, {"session": sid, "at": _stamp(ts),
                                  "marqueur": marqueur,
                                  "head": head.strip()[:160]}))
    signales.sort(key=lambda p: p[0], reverse=True)

    return {
        "fenetre_s": int(DELAI_APPARIEMENT_S),
        "marqueurs": list(MOTIFS_DELEGABLES),
        "sessions_actives": len(actives),
        "sessions_sans_delegation": len(sans_recu),
        "prompts": len(prompts),
        "prompts_signales": len(signales),
        "lectures_natives": {
            "read": lectures.get("Read", 0),
            "grep": lectures.get("Grep", 0),
            "glob": lectures.get("Glob", 0),
            "total": sum(lectures.values()),
        },
        "exemples": [d for _, d in signales[:MAX_OCCASIONS]],
    }


def summarize(receipts, events, now=None, days=14, health_state=None):
    """Tout ce que la page affiche, dérivé des journaux et du disjoncteur."""
    now_ts = float(now) if now is not None else \
        datetime.datetime.now(datetime.timezone.utc).timestamp()

    ms = [m for m in (_num(r.get("elapsed_ms")) for r in receipts) if m is not None]
    totals = {
        "calls": len(receipts),
        "ok": sum(1 for r in receipts if r.get("status") == "ok"),
        "err": sum(1 for r in receipts if r.get("status") != "ok"),
        "prompt_tokens": sum(_int(r, "prompt_tokens") for r in receipts),
        "output_tokens": sum(_int(r, "output_tokens") for r in receipts),
        "total_tokens": sum(_int(r, "total_tokens") for r in receipts),
        "elapsed_ms": sum(_int(r, "elapsed_ms") for r in receipts),
        "median_ms": _median(ms),
        "fallbacks": sum(_int(r, "fallbacks") for r in receipts),
    }

    tool_calls = sum(1 for e in events if e.get("kind") == "tool")
    return {
        "totals": totals,
        "by_mode": _group(receipts, "mode", "mode", "inconnu"),
        "by_model": _group(receipts, "model", "model", "inconnu"),
        "by_day": _by_day(receipts, now_ts, days),
        "sessions": _sessions(events),
        "quota_du_jour": _quota_du_jour(receipts),
        "part_gratuite": _part_gratuite(receipts),
        "cooldowns": _cooldowns(health_state or {}, now_ts),
        "occasions_manquees": _occasions_manquees(receipts, events),
        "delegation": {
            "gemini_calls": len(receipts),
            "user_prompts": sum(1 for e in events if e.get("kind") == "prompt"),
            "tool_calls": tool_calls,
            # Pas de ratio sans dénominateur : afficher 0 laisserait croire que
            # rien n'a été délégué, alors que rien n'a été mesuré.
            "per_tool_call": (round(len(receipts) / tool_calls, 3)
                              if tool_calls else None),
        },
    }


def resolve_period(preset, from_ts, to_ts, now_ts, tz="Europe/Paris"):
    """Résout un préréglage de période en (since_ts, until_ts, granularity, label)."""
    tzinfo = ZoneInfo(tz)
    now_local = datetime.datetime.fromtimestamp(now_ts, tzinfo)

    if preset == "today":
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        since_ts = start_local.timestamp()
        until_ts = now_ts
        granularity = "hour"
        label = "Aujourd'hui"
    elif preset == "yesterday":
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_local -= datetime.timedelta(days=1)
        end_local = start_local + datetime.timedelta(days=1)
        since_ts = start_local.timestamp()
        until_ts = end_local.timestamp()
        granularity = "hour"
        label = "Hier"
    elif preset == "7d":
        since_ts = now_ts - 7 * 86400
        until_ts = now_ts
        granularity = "hour"
        label = "7 derniers jours"
    elif preset == "30d":
        since_ts = now_ts - 30 * 86400
        until_ts = now_ts
        granularity = "day"
        label = "30 derniers jours"
    elif preset == "all":
        since_ts = None
        until_ts = now_ts
        granularity = "day"
        label = "Depuis toujours"
    elif preset == "custom":
        since_ts = float(from_ts) if from_ts not in (None, "", 0) else None
        until_ts = float(to_ts) if to_ts not in (None, "", 0) else now_ts
        if since_ts is not None:
            delta = until_ts - since_ts
            granularity = "hour" if delta <= 3 * 86400 else "day"
            dt_from = datetime.datetime.fromtimestamp(since_ts, tzinfo)
            dt_to = datetime.datetime.fromtimestamp(until_ts, tzinfo)
            label = (f"du {dt_from.strftime('%d/%m/%y %H:%M')} "
                     f"au {dt_to.strftime('%d/%m/%y %H:%M')}")
        else:
            granularity = "day"
            dt_to = datetime.datetime.fromtimestamp(until_ts, tzinfo)
            label = f"jusqu'au {dt_to.strftime('%d/%m/%y %H:%M')}"
    else:
        since_ts = now_ts - 7 * 86400
        until_ts = now_ts
        granularity = "hour"
        label = "7 derniers jours"

    return since_ts, until_ts, granularity, label


def bucket_by_time(receipts, since_ts, until_ts, granularity, tz="Europe/Paris"):
    """Groupe les reçus par intervalle temporel, buckets vides inclus."""
    tzinfo = ZoneInfo(tz)

    # Sans borne inférieure ET sans reçu, il n'y a pas de fenêtre à peindre.
    if not receipts and since_ts is None:
        return []
    if since_ts is None:
        since_ts = receipts[0]["ts"]

    if granularity == "hour":
        bucket_secs = 3600
        fmt_bucket = "%Y-%m-%dT%H:00"
        fmt_label = "%d/%m %Hh"
    else:
        bucket_secs = 86400
        fmt_bucket = "%Y-%m-%d"
        fmt_label = "%d/%m"

    # Aligner since_ts au début du bucket en heure locale
    since_local = datetime.datetime.fromtimestamp(since_ts, tzinfo)
    if granularity == "hour":
        since_aligned = since_local.replace(minute=0, second=0, microsecond=0)
    else:
        since_aligned = since_local.replace(hour=0, minute=0, second=0, microsecond=0)
    since_aligned_ts = since_aligned.timestamp()

    # Aligner until_ts au début du bucket suivant pour inclure le bucket final
    until_local = datetime.datetime.fromtimestamp(until_ts, tzinfo)
    if granularity == "hour":
        until_aligned = until_local.replace(minute=0, second=0, microsecond=0)
    else:
        until_aligned = until_local.replace(hour=0, minute=0, second=0, microsecond=0)
    until_aligned_ts = until_aligned.timestamp()

    # Pré-calculer les buckets
    buckets = []
    cursor = since_aligned_ts
    while cursor <= until_aligned_ts:
        bucket_local = datetime.datetime.fromtimestamp(cursor, tzinfo)
        buckets.append({
            "bucket": bucket_local.strftime(fmt_bucket),
            "label": bucket_local.strftime(fmt_label),
            "ts": cursor,
            "calls": 0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "by_model": {},
        })
        cursor += bucket_secs

    # Remplir les buckets
    for r in receipts:
        ts = r["ts"]
        if ts < since_ts or ts > until_ts:
            continue
        # Trouver l'index du bucket
        idx = int((ts - since_aligned_ts) // bucket_secs)
        if 0 <= idx < len(buckets):
            b = buckets[idx]
            b["calls"] += 1
            b["prompt_tokens"] += _int(r, "prompt_tokens")
            b["output_tokens"] += _int(r, "output_tokens")
            b["total_tokens"] += _int(r, "total_tokens")
            model = str(r.get("model") or "inconnu")
            m = b["by_model"].setdefault(model, {"calls": 0, "prompt_tokens": 0,
                                                  "output_tokens": 0, "total_tokens": 0})
            m["calls"] += 1
            m["prompt_tokens"] += _int(r, "prompt_tokens")
            m["output_tokens"] += _int(r, "output_tokens")
            m["total_tokens"] += _int(r, "total_tokens")

    return buckets


def error_breakdown(receipts):
    """Décomposition des erreurs par type, ordonnée par count décroissant."""
    errors = collections.defaultdict(lambda: {"count": 0, "last_ts": None, "last_detail": None})
    for r in receipts:
        if r.get("status") == "ok":
            continue
        etype = str(r.get("error_type") or "inconnu")
        ts = _num(r.get("ts"))
        detail = r.get("error_detail")
        e = errors[etype]
        e["count"] += 1
        if ts is not None and (e["last_ts"] is None or ts > e["last_ts"]):
            e["last_ts"] = ts
            e["last_detail"] = str(detail)[:120] if detail else None

    out = []
    for etype, e in errors.items():
        out.append({
            "error_type": etype,
            "count": e["count"],
            "last_at": _stamp(e["last_ts"]),
            "last_detail": e["last_detail"],
        })
    out.sort(key=lambda x: -x["count"])
    return out


def small_prompts(receipts, threshold=10):
    """Statistique des prompts courts (soupçon de gate-gaming)."""
    measured = 0
    small = 0
    for r in receipts:
        pc = _num(r.get("prompt_chars"))
        if pc is not None:
            measured += 1
            if pc < threshold:
                small += 1
    share = round(small / measured, 3) if measured else None
    return {"threshold": threshold, "small": small, "measured": measured, "share": share}


def top_items(receipts, field, n=5, unknown="inconnu"):
    """Top N par nombre d'appels sur un champ donné."""
    if not receipts:
        return []
    counts = collections.defaultdict(lambda: {"calls": 0, "total_tokens": 0})
    for r in receipts:
        key = str(r.get(field) or unknown)
        counts[key]["calls"] += 1
        counts[key]["total_tokens"] += _int(r, "total_tokens")
    total_calls = len(receipts)
    items = [{"name": k, "calls": v["calls"], "total_tokens": v["total_tokens"],
              "share": round(v["calls"] / total_calls, 3)}
             for k, v in counts.items()]
    items.sort(key=lambda x: (-x["calls"], -x["total_tokens"], x["name"]))
    return items[:n]


def per_day_breakdown(receipts, since_ts, until_ts, tz="Europe/Paris"):
    """Détail jour par jour dans la fenêtre, jours vides omis.

    `since_ts=None` (préréglage « Depuis toujours ») équivaut à « tout » : la
    borne basse ne filtre alors plus rien."""
    tzinfo = ZoneInfo(tz)
    borne_basse = float("-inf") if since_ts is None else since_ts

    # Grouper par jour local
    days = collections.defaultdict(lambda: {
        "calls": 0, "ok": 0, "err": 0,
        "prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "errors": collections.defaultdict(lambda: {"count": 0, "last_ts": None, "last_detail": None}),
        "by_model": collections.defaultdict(lambda: {"calls": 0, "prompt_tokens": 0,
                                                      "output_tokens": 0, "total_tokens": 0}),
        "quota_snapshots": {},  # (provider, model) -> (ts, quota_block)
        "small_prompts": {"measured": 0, "small": 0},
    })

    for r in receipts:
        ts = r["ts"]
        if ts < borne_basse or ts > until_ts:
            continue
        local = datetime.datetime.fromtimestamp(ts, tzinfo)
        day_key = local.date().isoformat()
        d = days[day_key]
        d["calls"] += 1
        if r.get("status") == "ok":
            d["ok"] += 1
        else:
            d["err"] += 1
        d["prompt_tokens"] += _int(r, "prompt_tokens")
        d["output_tokens"] += _int(r, "output_tokens")
        d["total_tokens"] += _int(r, "total_tokens")

        # Erreurs
        if r.get("status") != "ok":
            etype = str(r.get("error_type") or "inconnu")
            e = d["errors"][etype]
            e["count"] += 1
            if e["last_ts"] is None or ts > e["last_ts"]:
                e["last_ts"] = ts
                detail = r.get("error_detail")
                e["last_detail"] = str(detail)[:120] if detail else None

        # Par modèle
        model = str(r.get("model") or "inconnu")
        m = d["by_model"][model]
        m["calls"] += 1
        m["prompt_tokens"] += _int(r, "prompt_tokens")
        m["output_tokens"] += _int(r, "output_tokens")
        m["total_tokens"] += _int(r, "total_tokens")

        # Quota snapshot
        bloc = r.get("quota")
        if isinstance(bloc, dict):
            provider = str(r.get("provider") or "inconnu")
            key = (provider, model)
            if key not in d["quota_snapshots"] or ts > d["quota_snapshots"][key][0]:
                d["quota_snapshots"][key] = (ts, bloc)

        # Small prompts
        pc = _num(r.get("prompt_chars"))
        if pc is not None:
            d["small_prompts"]["measured"] += 1
            if pc < 10:
                d["small_prompts"]["small"] += 1

    # Construire la sortie ordonnée chronologiquement décroissante
    out = []
    for day_key in sorted(days.keys(), reverse=True):
        d = days[day_key]
        # Errors formatées
        errors_fmt = []
        for etype, e in d["errors"].items():
            errors_fmt.append({
                "error_type": etype,
                "count": e["count"],
                "last_at": _stamp(e["last_ts"]),
                "last_detail": e["last_detail"],
            })
        errors_fmt.sort(key=lambda x: -x["count"])

        # By model formaté
        by_model_fmt = []
        for model, m in d["by_model"].items():
            by_model_fmt.append({
                "model": model,
                "calls": m["calls"],
                "prompt_tokens": m["prompt_tokens"],
                "output_tokens": m["output_tokens"],
                "total_tokens": m["total_tokens"],
            })
        by_model_fmt.sort(key=lambda x: -x["total_tokens"])

        # Quota snapshot formaté
        quota_fmt = []
        for (provider, model), (ts, bloc) in d["quota_snapshots"].items():
            quota_fmt.append({
                "provider": provider,
                "model": model,
                "window": bloc.get("window"),
                "limit": bloc.get("limit"),
                "used": bloc.get("used"),
                "remaining": bloc.get("remaining"),
                "at": _stamp(ts),
            })
        quota_fmt.sort(key=lambda x: (x["provider"], x["model"]))

        sp = d["small_prompts"]
        share = round(sp["small"] / sp["measured"], 3) if sp["measured"] else None

        out.append({
            "date": day_key,
            "label": datetime.date.fromisoformat(day_key).strftime("%d/%m"),
            "calls": d["calls"],
            "ok": d["ok"],
            "err": d["err"],
            "prompt_tokens": d["prompt_tokens"],
            "output_tokens": d["output_tokens"],
            "total_tokens": d["total_tokens"],
            "small_prompts": {"threshold": 10, "small": sp["small"],
                               "measured": sp["measured"], "share": share},
            "errors": errors_fmt,
            "by_model": by_model_fmt,
            "quota_snapshot": quota_fmt,
        })
    return out


def snapshot(data_root, days=14):
    """Agrégat des `days` derniers jours, plus de quoi situer ce qui est lu."""
    directory = log_dir(data_root)
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    since = now_ts - days * 86400

    missing = []

    rpath = _receipts_path(data_root)
    receipts = read_jsonl(rpath, since_ts=since)
    receipts.sort(key=lambda r: r["ts"])
    if not os.path.exists(rpath):
        missing.append(RECEIPTS_NAME)

    epath = os.path.join(directory, EVENTS_NAME)
    events = read_jsonl(epath, since_ts=since)
    events.sort(key=lambda r: r["ts"])
    if not os.path.exists(epath):
        missing.append(EVENTS_NAME)

    hpath = _health_path(data_root)
    health_state = ai_health.load(hpath)
    if not os.path.exists(hpath):
        missing.append(HEALTH_NAME)

    out = summarize(receipts, events, now=now_ts, days=days,
                    health_state=health_state)
    out.update({
        "dir": directory,
        "receipts_seen": len(receipts),
        "events_seen": len(events),
        "health_seen": len(health_state.get("entries") or {}),
        "days": days,
        "missing": missing,
    })
    return out


def snapshot_windowed(data_root, preset="7d", from_ts=None, to_ts=None,
                       granularity=None, tz="Europe/Paris"):
    """Snapshot avec fenêtrage arbitraire, buckets, top items, per-day, couleurs."""
    directory = log_dir(data_root)
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

    since_ts, until_ts, resolved_granularity, label = resolve_period(
        preset, from_ts, to_ts, now_ts, tz)
    if granularity is not None:
        resolved_granularity = granularity

    missing = []

    rpath = _receipts_path(data_root)
    receipts = read_jsonl(rpath, since_ts=since_ts)
    receipts.sort(key=lambda r: r["ts"])
    if not os.path.exists(rpath):
        missing.append(RECEIPTS_NAME)

    epath = os.path.join(directory, EVENTS_NAME)
    events = read_jsonl(epath, since_ts=since_ts)
    events.sort(key=lambda r: r["ts"])
    if not os.path.exists(epath):
        missing.append(EVENTS_NAME)

    hpath = _health_path(data_root)
    health_state = ai_health.load(hpath)
    if not os.path.exists(hpath):
        missing.append(HEALTH_NAME)

    # Totals et agrégats de base
    base = summarize(receipts, events, now=now_ts, days=14,
                     health_state=health_state)

    # Buckets
    buckets = bucket_by_time(receipts, since_ts, until_ts, resolved_granularity, tz)

    # Top models / modes
    top_models = top_items(receipts, "model", n=5)
    top_modes = top_items(receipts, "mode", n=5)

    # Small prompts global
    sp_global = small_prompts(receipts, threshold=10)

    # Errors global
    errors_global = error_breakdown(receipts)

    # Per day breakdown
    per_day = per_day_breakdown(receipts, since_ts, until_ts, tz)

    # Model colors
    PALETTE = ["#60a5fa", "#4ade80", "#fbbf24", "#f87171", "#a78bfa",
               "#fb923c", "#22d3ee", "#f472b6", "#84cc16", "#94a3b8"]
    by_model_sorted = base["by_model"]  # déjà trié par total_tokens desc
    model_colors = {}
    for i, m in enumerate(by_model_sorted):
        model_colors[m["model"]] = PALETTE[i % len(PALETTE)]

    return {
        "dir": directory,
        "period": {
            "preset": preset,
            "since_ts": since_ts,
            "until_ts": until_ts,
            "granularity": resolved_granularity,
            "label": label,
            "tz": tz,
        },
        "receipts_seen": len(receipts),
        "events_seen": len(events),
        "health_seen": len(health_state.get("entries") or {}),
        "missing": missing,
        "totals": base["totals"],
        "by_mode": base["by_mode"],
        "by_model": base["by_model"],
        "top_models": top_models,
        "top_modes": top_modes,
        "buckets": buckets,
        "sessions": base["sessions"],
        "quota_du_jour": base["quota_du_jour"],
        "part_gratuite": base["part_gratuite"],
        "cooldowns": base["cooldowns"],
        "occasions_manquees": base["occasions_manquees"],
        "delegation": base["delegation"],
        "small_prompts": sp_global,
        "errors": errors_global,
        "per_day": per_day,
        "model_colors": model_colors,
    }
