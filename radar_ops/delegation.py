"""Agrégation LECTURE SEULE des journaux de délégation et de développement.

Répond à une question et une seule : qu'est-ce qui a réellement été délégué à
Gemini, et à quel coût ? Deux journaux JSONL, écrits par deux processus qui
ignorent tout de cette page :

- `gemini-receipts.jsonl`, une ligne par appel, écrite par `scripts/ai_query.py` ;
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
"""
import bisect
import collections
import datetime
import json
import os

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
    """Chemin des reçus Gemini — directement dans le dossier ops.

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
    2. un prompt portant un marqueur de tâche délégable (lecture, résumé,
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
