"""Reconstruit le workflow complet de chaque requête depuis les transcripts Claude Code.

Les reçus du courtier et la télémétrie ne portent que des compteurs ; seul le
transcript (`~/.claude/projects/<projet>/<session>.jsonl`) garde le texte de la
demande, chaque appel d'outil et sa sortie, et l'usage de chaque appel API.
On le recoupe avec les reçus (`gemini-receipts.jsonl`) et les tentatives par
modèle (`ai/ai_usage.jsonl`) pour chiffrer les étapes déléguées.

Sortie : `<ops>/workflows/<sessionId>.json`, lu par `radar_ops/workflows.py`.
Tourne côté hôte (le conteneur `radar-ops` ne voit pas `~/.claude`) : en CLI
pour un rattrapage, ou par le hook `Stop` pour la session courante.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SINCE = "2026-09-26T20:00:00Z"      # 26/09 22h heure de Paris
LIMIT_TEXT = 60000
LIMIT_TOOL_OUTPUT = 4000                     # sorties d'outils locaux : un extrait suffit
MATCH_BEFORE_S = 1.0
MATCH_AFTER_S = 5.0                          # le reçu est écrit juste après la fin de l'appel
BACKGROUND_WINDOW_S = 1800.0                 # appel lancé en arrière-plan : le reçu arrive bien après
LOCAL_COMMANDS = ("<local-command", "<command-name>/clear", "<command-name>/model",
                  "<command-name>/compact")
RECEIPT_FIELDS = ("mode", "status", "model", "provider", "prompt_tokens", "output_tokens",
                  "cost_usd", "paid", "elapsed_ms", "fallbacks", "attempts",
                  "routing_reason", "prompt_chars")
_BROKER_RE = re.compile(r"\bai_(?:broker|query)\.py\b")
_MODE_RE = re.compile(r"--mode[=\s]+['\"]?([\w-]+)")
_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def load_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def parse_iso(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def to_iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def truncate(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [tronqué, {len(text)} car. au total]"


def block_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(b.get("text", "") for b in content
                   if isinstance(b, dict) and b.get("type") == "text")


def _has_tool_result(content):
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def user_text(line):
    """Texte humain d'une ligne `user` (hors résultats d'outils et rappels système)."""
    if line.get("type") != "user" or line.get("isMeta") or line.get("isSidechain"):
        return None
    content = (line.get("message") or {}).get("content")
    if _has_tool_result(content):
        return None
    return _REMINDER_RE.sub("", block_text(content)).strip() or None


def prompt_of(line):
    """Texte de la demande si la ligne ouvre une requête, sinon None.

    Une notification de fin de tâche de fond arrive comme un message `user` mais
    prolonge la requête en cours : elle n'en ouvre pas une nouvelle."""
    text = user_text(line)
    if not text or text.startswith(LOCAL_COMMANDS) or text.startswith("<task-notification>"):
        return None
    if text.startswith("<command-name>"):
        name = re.search(r"<command-name>([^<]*)</command-name>", text)
        args = re.search(r"<command-args>([^<]*)</command-args>", text)
        text = " ".join(p for p in ((name.group(1).strip() if name else ""),
                                    (args.group(1).strip() if args else "")) if p)
        if not text.startswith("/"):
            text = "/" + text
    return text


def tool_label(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    if name in ("Read", "Edit", "Write"):
        return f"{name} {inp.get('file_path', '')}"
    if name in ("Grep", "Glob"):
        return f"{name} {inp.get('pattern', '')}"
    if name == "Bash":
        return "Bash " + (inp.get("description") or str(inp.get("command", ""))[:80])
    if name in ("Agent", "Task"):
        return f"{name} {inp.get('description', '')}"
    return name


def broker_mode(name, inp):
    """Mode du courtier si cet appel Bash le lance, sinon None."""
    if name != "Bash" or not isinstance(inp, dict):
        return None
    cmd = str(inp.get("command", ""))
    if not _BROKER_RE.search(cmd):
        return None
    m = _MODE_RE.search(cmd)
    return m.group(1) if m else None


class _Matcher:
    """Rattache reçus et tentatives du courtier à un appel Bash par fenêtre de temps.

    Plusieurs sessions peuvent appeler le courtier en parallèle : on exige aussi
    le même mode, et un reçu n'est rattaché qu'une fois."""

    def __init__(self, receipts, attempts):
        self.receipts = sorted((r for r in receipts if isinstance(r.get("ts"), (int, float))),
                               key=lambda r: r["ts"])
        self.attempts = sorted((a for a in attempts if isinstance(a.get("ts"), (int, float))),
                               key=lambda a: a["ts"])
        self.used = set()

    def receipt(self, mode, t0, t1):
        best, best_d = None, None
        for idx, r in enumerate(self.receipts):
            if idx in self.used or r.get("mode") not in (mode, None):
                continue
            if t0 - MATCH_BEFORE_S <= r["ts"] <= t1 + MATCH_AFTER_S:
                d = abs(r["ts"] - t1)
                if best_d is None or d < best_d:
                    best, best_d = idx, d
        if best is None:
            return None
        self.used.add(best)
        r = self.receipts[best]
        return dict({k: r.get(k) for k in RECEIPT_FIELDS}, t=to_iso(r["ts"]))

    def attempts_between(self, mode, t0, t1):
        out = []
        for a in self.attempts:
            if a.get("mode") not in (mode, None):
                continue
            if t0 - MATCH_BEFORE_S <= a["ts"] <= t1 + MATCH_AFTER_S:
                tok = a.get("tokens") or {}
                out.append({
                    "t": to_iso(a["ts"]),
                    "provider": a.get("provider"),
                    "model": a.get("model"),
                    "status": a.get("status"),
                    "error_type": a.get("error_type"),
                    "error_detail": a.get("error_detail"),
                    "tokens": {"prompt_tokens": tok.get("prompt_tokens") or 0,
                               "output_tokens": tok.get("output_tokens") or 0},
                    "elapsed_ms": a.get("elapsed_ms"),
                    "cost_usd": a.get("cost_usd"),
                    "paid": a.get("paid"),
                })
        return out


def _new_request(rid, session, project, t, prompt, redact):
    return {
        "id": rid, "session": session, "project": project,
        "started": to_iso(t), "ended": to_iso(t),
        "prompt": truncate(redact(prompt), LIMIT_TEXT),
        "title": " ".join(prompt.split())[:120],
        "model": "",
        "totals": {
            "claude": {"calls": 0, "input": 0, "cache_read": 0, "cache_create": 0, "output": 0},
            "delegated": {"calls": 0, "with_receipt": 0, "prompt": 0, "output": 0,
                          "cost_usd": 0.0, "failed": 0, "attempts_failed": 0},
            "tools": {"calls": 0, "errors": 0, "by_name": {}},
        },
        "steps": [{"i": 0, "t": to_iso(t), "kind": "prompt", "from": "user", "to": "claude",
                   "label": "demande", "tokens": {}, "input": truncate(redact(prompt), LIMIT_TEXT),
                   "output": "", "is_error": False}],
        "_last_text": None, "_open": {}, "_calls": {}, "_background": [],
    }


def _add_step(req, **step):
    step["i"] = len(req["steps"])
    req["steps"].append(step)
    return step


def _on_assistant(req, line, t, redact):
    msg = line.get("message") or {}
    if msg.get("model") and not req["model"]:
        req["model"] = msg["model"]
    rid = line.get("requestId") or msg.get("id") or line.get("uuid")
    call = req["_calls"].get(rid)
    if call is None:
        # Un appel API est éclaté en une ligne par bloc, chacune répétant le même
        # `usage` : on ne le compte qu'à sa première ligne.
        u = msg.get("usage") or {}
        tokens = {"input": int(u.get("input_tokens") or 0),
                  "cache_read": int(u.get("cache_read_input_tokens") or 0),
                  "cache_create": int(u.get("cache_creation_input_tokens") or 0),
                  "output": int(u.get("output_tokens") or 0)}
        c = req["totals"]["claude"]
        c["calls"] += 1
        for k, v in tokens.items():
            c[k] += v
        call = _add_step(req, t=to_iso(t), kind="claude_call", **{"from": "claude", "to": "claude"},
                         label=msg.get("model") or "", n=c["calls"], tokens=tokens,
                         input="", output="", is_error=False)
        req["_calls"][rid] = call
    for b in msg.get("content") or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and b.get("text"):
            call["output"] = truncate(call["output"] + redact(b["text"]), LIMIT_TEXT)
            req["_last_text"] = (t, b["text"])
        elif b.get("type") == "tool_use":
            name, inp = b.get("name", ""), b.get("input") or {}
            mode = broker_mode(name, inp)
            if mode:
                req["totals"]["delegated"]["calls"] += 1
                step = _add_step(req, t=to_iso(t), kind="broker", **{"from": "claude", "to": "broker"},
                                 label=f"mode {mode}", mode=mode, tokens={},
                                 input=truncate(redact(str(inp.get("command", ""))), LIMIT_TEXT),
                                 output="", is_error=False, receipt=None, attempts=[])
            else:
                tools = req["totals"]["tools"]
                tools["calls"] += 1
                tools["by_name"][name] = tools["by_name"].get(name, 0) + 1
                step = _add_step(req, t=to_iso(t), kind="tool", **{"from": "claude", "to": "tools"},
                                 label=tool_label(name, inp), tool=name, tokens={},
                                 input=truncate(redact(json.dumps(inp, ensure_ascii=False, indent=1)), LIMIT_TEXT),
                                 output="", is_error=False)
            req["_open"][b.get("id")] = (step, t)


def _on_tool_results(req, line, t, matcher, redact):
    for b in (line.get("message") or {}).get("content") or []:
        if not isinstance(b, dict) or b.get("type") != "tool_result":
            continue
        opened = req["_open"].pop(b.get("tool_use_id"), None)
        if opened is None:
            continue
        step, t0 = opened
        text, err = block_text(b.get("content")), bool(b.get("is_error"))
        step.update(t_end=to_iso(t), duration_s=round(t - t0, 2), is_error=err,
                    output_chars=len(text))
        if step["kind"] == "tool":
            step["output"] = truncate(redact(text), LIMIT_TOOL_OUTPUT)
            if err:
                req["totals"]["tools"]["errors"] += 1
            continue
        step["output"] = truncate(redact(text), LIMIT_TEXT)
        d = req["totals"]["delegated"]
        rec = matcher.receipt(step["mode"], t0, t)
        step["attempts"] = matcher.attempts_between(step["mode"], t0, t)
        d["attempts_failed"] += sum(1 for a in step["attempts"] if a["status"] != "ok")
        if rec:
            step["receipt"] = rec
            pt, ot = int(rec.get("prompt_tokens") or 0), int(rec.get("output_tokens") or 0)
            step["tokens"] = {"prompt": pt, "output": ot}
            d["with_receipt"] += 1
            d["prompt"] += pt
            d["output"] += ot
            d["cost_usd"] = round(d["cost_usd"] + float(rec.get("cost_usd") or 0), 6)
            if rec.get("status") != "ok":
                d["failed"] += 1
        elif "running in background" in text:
            step["background"] = True
            req["_background"].append((step, t0))
        elif err:
            d["failed"] += 1


def _on_notification(req, line, t, text, redact):
    tid = re.search(r"<task-id>([^<]*)</task-id>", text)
    status = re.search(r"<status>([^<]*)</status>", text)
    _add_step(req, t=to_iso(t), kind="notification", **{"from": "tools", "to": "claude"},
              label="fin tâche de fond " + (status.group(1) if status else ""),
              task_id=tid.group(1) if tid else "", tokens={}, input="",
              output=truncate(redact(text), LIMIT_TOOL_OUTPUT), is_error=False)


def _settle_background(req, matcher):
    """Reçus des appels courtier lancés en arrière-plan : fenêtre élargie après le lancement."""
    d = req["totals"]["delegated"]
    for step, t0 in req.pop("_background"):
        t1 = t0 + BACKGROUND_WINDOW_S
        rec = matcher.receipt(step["mode"], t0, t1)
        if not rec:
            continue
        step["attempts"] = [a for a in matcher.attempts_between(step["mode"], t0, t1)
                            if a["t"] <= rec["t"]]
        d["attempts_failed"] += sum(1 for a in step["attempts"] if a["status"] != "ok")
        step["receipt"] = rec
        pt, ot = int(rec.get("prompt_tokens") or 0), int(rec.get("output_tokens") or 0)
        step["tokens"] = {"prompt": pt, "output": ot}
        d["with_receipt"] += 1
        d["prompt"] += pt
        d["output"] += ot
        d["cost_usd"] = round(d["cost_usd"] + float(rec.get("cost_usd") or 0), 6)
        if rec.get("status") != "ok":
            d["failed"] += 1


def _close(req, redact, matcher):
    _settle_background(req, matcher)
    last = req.pop("_last_text")
    req.pop("_open")
    calls = req.pop("_calls")
    if last:
        t, text = last
        _add_step(req, t=to_iso(t), kind="answer", **{"from": "claude", "to": "user"},
                  label="réponse", tokens={}, input="",
                  output=truncate(redact(text), LIMIT_TEXT), is_error=False)
    req["ended"] = max((s.get("t_end") or s["t"] for s in req["steps"]), default=req["started"])
    return req if calls else None


def parse_transcript(path, receipts, attempts, redact=lambda s: s, since_ts=None, matcher=None):
    """Requêtes d'un transcript, dans l'ordre, avec leurs étapes chronologiques.

    L'identifiant `<sessionId>:<n>` compte toutes les demandes depuis le début du
    fichier, filtrées ou non : il reste stable d'une ingestion à l'autre."""
    matcher = matcher or _Matcher(receipts, attempts)
    out, req, n = [], None, 0
    session = project = None
    for line in load_jsonl(path):
        if line.get("isSidechain"):
            continue
        t = parse_iso(line.get("timestamp"))
        if t is None:
            continue
        session = session or line.get("sessionId")
        project = project or line.get("cwd")
        prompt = prompt_of(line)
        if prompt is not None:
            if req is not None:
                done = _close(req, redact, matcher)
                if done:
                    out.append(done)
            n += 1
            keep = since_ts is None or t >= since_ts
            req = _new_request(f"{session}:{n}", session, project, t, prompt, redact) if keep else None
            continue
        if req is None:
            continue
        if line.get("type") == "assistant":
            _on_assistant(req, line, t, redact)
        elif line.get("type") == "user":
            text = user_text(line)
            if text and text.startswith("<task-notification>"):
                _on_notification(req, line, t, text, redact)
            else:
                _on_tool_results(req, line, t, matcher, redact)
    if req is not None:
        done = _close(req, redact, matcher)
        if done:
            out.append(done)
    return out


def _write(ops_dir, requests):
    wdir = Path(ops_dir) / "workflows"
    wdir.mkdir(parents=True, exist_ok=True)
    session = requests[0]["session"]
    if not session or not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        return 0
    data = {"session": session, "project": requests[0]["project"],
            "updated": to_iso(datetime.now(timezone.utc).timestamp()), "requests": requests}
    tmp = wdir / f".{session}.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, wdir / f"{session}.json")
    return len(requests)


def _load_side(ops_dir):
    ops = Path(ops_dir)
    return load_jsonl(ops / "gemini-receipts.jsonl"), load_jsonl(ops / "ai" / "ai_usage.jsonl")


def ingest(projects_dir, ops_dir, since_ts=None, redact=lambda s: s):
    """Rattrapage : tous les transcripts modifiés depuis `since_ts`."""
    receipts, attempts = _load_side(ops_dir)
    # Un seul matcher pour tout le lot : un reçu ne peut pas servir deux sessions.
    matcher = _Matcher(receipts, attempts)
    files = sorted(Path(projects_dir).glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)
    total = 0
    for path in files:
        if since_ts is not None and path.stat().st_mtime < since_ts:
            continue
        reqs = parse_transcript(path, receipts, attempts, redact, since_ts, matcher)
        if reqs:
            total += _write(ops_dir, reqs)
    return total


def ingest_one(transcript_path, ops_dir, since_ts=None, redact=lambda s: s):
    """Session courante, depuis le hook `Stop` : ne lève jamais."""
    try:
        receipts, attempts = _load_side(ops_dir)
        reqs = parse_transcript(transcript_path, receipts, attempts, redact, since_ts)
        return _write(ops_dir, reqs) if reqs else 0
    except Exception:
        return 0


def default_ops_dir():
    env = (os.environ.get("RADAR_TELEMETRY_DIR") or "").strip()
    return env or str(Path(__file__).resolve().parents[1] / "data" / "ops")


def load_redact():
    """Masquage des secrets du courtier ; identité si indisponible."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from ai.redact import redact
        return redact
    except Exception:
        return lambda s: s


def parse_since(value):
    try:
        return float(value)
    except ValueError:
        ts = parse_iso(value)
        if ts is None:
            raise SystemExit(f"--since illisible : {value!r}")
        return ts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--ops-dir", default=default_ops_dir())
    ap.add_argument("--since", default=DEFAULT_SINCE, help="ISO 8601 ou epoch")
    ap.add_argument("--transcript", help="un seul transcript (hook Stop)")
    args = ap.parse_args(argv)
    since = parse_since(args.since)
    redact = load_redact()
    if args.transcript:
        n = ingest_one(args.transcript, args.ops_dir, since, redact)
    else:
        n = ingest(args.projects, args.ops_dir, since, redact)
    print(f"{n} requête(s) écrite(s) dans {Path(args.ops_dir) / 'workflows'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
