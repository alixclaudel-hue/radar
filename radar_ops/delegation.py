"""Agrégation LECTURE SEULE des journaux de délégation et de développement.

Répond à une question et une seule : qu'est-ce qui a réellement été délégué à
Gemini, et à quel coût ? Deux journaux JSONL, écrits par deux processus qui
ignorent tout de cette page :

- `gemini-receipts.jsonl`, une ligne par appel, écrite par `scripts/ai_query.py` ;
- `telemetry.jsonl`, une ligne par événement de session, écrite par
  `scripts/hooks/telemetry.py`.

Les jetons affichés viennent de `usageMetadata`, rapporté par l'API — une
mesure. Aucune estimation d'économie côté Claude n'est calculée ici : ce
serait un contrefactuel, et le publier à côté de vraies mesures le ferait
passer pour l'une d'elles.

Les lignes anciennes n'ont pas les champs récents (jetons, modèle, durée) : un
champ absent vaut 0 plutôt que de disqualifier la ligne, sans quoi le tableau
de bord ignorerait tout l'historique antérieur à l'instrumentation.
"""
import collections
import datetime
import json
import os

RECEIPTS_NAME = "gemini-receipts.jsonl"
EVENTS_NAME = "telemetry.jsonl"
REMOTE_SUBDIR = "remote"
TOP_TOOLS = 5
MAX_SESSIONS = 20


def log_dir(data_root):
    """Dossier des journaux, `RADAR_OPS_TELEMETRY_DIR` prioritaire.

    Le défaut est `<data>/ops` : le conteneur `radar-ops` ne voit que le volume
    de données, jamais le checkout git d'où les journaux sont écrits."""
    override = (os.environ.get("RADAR_OPS_TELEMETRY_DIR") or "").strip()
    return override or os.path.join(data_root, "ops")


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


def summarize(receipts, events, now=None, days=14):
    """Tout ce que la page affiche, dérivé des deux journaux."""
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


def sources(directory, name):
    """Les deux origines d'un même journal, locale puis rapatriée.

    `<dir>/<nom>` est ce qu'une session tournant sur cette machine écrit
    directement ; `<dir>/remote/<nom>` est ce que `telemetry_pull.py` rapatrie
    des autres sessions. Deux fichiers et non un seul parce que la réception
    filtre sur le `ts` déjà stocké : mélangée à des écritures locales à
    l'instant présent, elle rejetterait tout ce qu'elle rapatrie."""
    return (os.path.join(directory, name),
            os.path.join(directory, REMOTE_SUBDIR, name))


def snapshot(data_root, days=14):
    """Agrégat des `days` derniers jours, plus de quoi situer ce qui est lu."""
    directory = log_dir(data_root)
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    since = now_ts - days * 86400

    read = {}
    missing = []
    for name in (RECEIPTS_NAME, EVENTS_NAME):
        paths = sources(directory, name)
        rows = []
        for p in paths:
            rows.extend(read_jsonl(p, since_ts=since))
        # Les deux origines sont indépendantes : sans ce tri, la page mélangerait
        # deux suites chronologiques et les sessions s'afficheraient dans le
        # désordre.
        rows.sort(key=lambda r: r["ts"])
        read[name] = rows
        # Un journal n'est signalé absent que si AUCUNE des deux origines n'existe :
        # n'avoir jamais rien rapatrié est normal sur une machine qui produit tout.
        if not any(os.path.exists(p) for p in paths):
            missing.append(name)

    receipts, events = read[RECEIPTS_NAME], read[EVENTS_NAME]
    out = summarize(receipts, events, now=now_ts, days=days)
    out.update({
        "dir": directory,
        "receipts_seen": len(receipts),
        "events_seen": len(events),
        "days": days,
        # Nommer les fichiers absents permet à la page de dire « le transport
        # n'a rien déposé » plutôt que d'aligner des zéros sans explication.
        "missing": missing,
    })
    return out
