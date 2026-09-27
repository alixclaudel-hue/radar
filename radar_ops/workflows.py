"""Cartographie des requêtes Claude : lecture des workflows ingérés.

`scripts/workflow_ingest.py` écrit, côté hôte, un fichier par session dans
`<ops>/workflows/<sessionId>.json` ; ce module ne fait que le relire (le
conteneur `radar-ops` ne voit jamais `~/.claude/projects`). Deux vues : un
Sankey agrégé en tokens sur une période, et le détail d'une requête (étapes)
servi à la demande — le détail peut peser lourd, la liste n'en porte rien."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from .delegation import log_dir

_SESSION_RE = re.compile(r"[A-Za-z0-9_-]+")


def workflows_dir(data_root):
    return os.path.join(log_dir(data_root), "workflows")


def _iso_to_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _int(d, key):
    try:
        return int((d or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_all(data_root):
    """Toutes les requêtes connues, plus récentes d'abord."""
    wdir = workflows_dir(data_root)
    try:
        names = sorted(n for n in os.listdir(wdir) if n.endswith(".json"))
    except OSError:
        return []
    reqs = []
    for name in names:
        data = _read(os.path.join(wdir, name))
        if data and isinstance(data.get("requests"), list):
            reqs.extend(r for r in data["requests"] if isinstance(r, dict))
    reqs.sort(key=lambda r: _iso_to_ts(r.get("started")) or 0, reverse=True)
    return reqs


def _filter(reqs, since_ts=None, until_ts=None, project=None):
    out = []
    for r in reqs:
        ts = _iso_to_ts(r.get("started"))
        if ts is None:
            continue
        if since_ts is not None and ts < since_ts:
            continue
        if until_ts is not None and ts > until_ts:
            continue
        if project and r.get("project") != project:
            continue
        out.append(r)
    return out


def _claude_split(totals):
    c = (totals or {}).get("claude") or {}
    return {k: _int(c, k) for k in ("input", "cache_read", "cache_create", "output")}


def _share(delegated, fresh):
    # Le cache relu est exclu du dénominateur : c'est la même fenêtre refacturée,
    # pas du travail — le compter écraserait toute part déléguée vers zéro.
    denom = delegated + fresh
    return round(delegated / denom, 3) if denom else 0.0


def summary(r):
    """Ligne de liste d'une requête, sans étapes ni texte intégral."""
    totals = r.get("totals") or {}
    c = _claude_split(totals)
    d = totals.get("delegated") or {}
    t = totals.get("tools") or {}
    fresh = c["input"] + c["cache_create"] + c["output"]
    delegated = _int(d, "prompt") + _int(d, "output")
    wasted = _int(d, "failed_prompt") + _int(d, "failed_output")
    start, end = _iso_to_ts(r.get("started")), _iso_to_ts(r.get("ended"))
    return {
        "id": r.get("id"),
        "session": r.get("session"),
        "project": r.get("project"),
        "project_short": (r.get("project") or "").rstrip("/").rsplit("/", 1)[-1],
        "started": r.get("started"),
        "ended": r.get("ended"),
        "duration_s": round(end - start, 1) if start and end else 0,
        "title": r.get("title") or "",
        "model": r.get("model") or "",
        "claude_tokens": sum(c.values()),
        "claude_fresh": fresh,
        "claude_calls": _int(totals.get("claude"), "calls"),
        "delegated_tokens": delegated,
        "delegated_calls": _int(d, "calls"),
        "delegated_failed": _int(d, "failed"),
        "delegated_failed_tokens": wasted,
        "tools_calls": _int(t, "calls"),
        "tools_errors": _int(t, "errors"),
        "cost_usd": float(d.get("cost_usd") or 0),
        "share_delegated": _share(delegated, fresh),
    }


def get_request(data_root, req_id):
    """Requête complète `<sessionId>:<n>`, ou None.

    Le sessionId sert de nom de fichier : tout caractère hors [A-Za-z0-9_-]
    est refusé avant d'ouvrir quoi que ce soit (pas de traversée de chemin)."""
    session_id, sep, _ = str(req_id or "").partition(":")
    if not sep or not _SESSION_RE.fullmatch(session_id):
        return None
    data = _read(os.path.join(workflows_dir(data_root), session_id + ".json"))
    for r in (data or {}).get("requests") or []:
        if isinstance(r, dict) and r.get("id") == req_id:
            return r
    return None


def _model_key(provider, model):
    return f"{provider or '?'}/{model or '?'}"


def sankey(reqs):
    """Flux de tokens sur 3 colonnes : requêtes → Claude/Courtier → destination.

    L'entrée du Courtier est la somme de ses sorties (modèles retenus + tentatives
    en échec) : le Sankey reste conservatif même quand un reçu manque."""
    claude = {"input": 0, "cache_read": 0, "cache_create": 0, "output": 0}
    by_model, failed_tokens, by_mode = {}, 0, {}
    delegated_calls = delegated_failed = tools_calls = 0
    cost = 0.0
    for r in reqs:
        totals = r.get("totals") or {}
        for k, v in _claude_split(totals).items():
            claude[k] += v
        d = totals.get("delegated") or {}
        delegated_calls += _int(d, "calls")
        delegated_failed += _int(d, "failed")
        cost += float(d.get("cost_usd") or 0)
        tools_calls += _int(totals.get("tools"), "calls")
        for step in r.get("steps") or []:
            if not isinstance(step, dict) or step.get("kind") != "broker":
                continue
            label = str(step.get("label") or "")
            if label.startswith("mode "):
                mode = label[5:].strip()
                by_mode[mode] = by_mode.get(mode, 0) + 1
            rec = step.get("receipt")
            if isinstance(rec, dict) and rec.get("status") == "ok":
                key = _model_key(rec.get("provider"), rec.get("model"))
                by_model[key] = by_model.get(key, 0) + _int(rec, "prompt_tokens") + _int(rec, "output_tokens")
            for att in step.get("attempts") or []:
                if isinstance(att, dict) and att.get("status") != "ok":
                    tok = att.get("tokens") or {}
                    failed_tokens += _int(tok, "prompt_tokens") + _int(tok, "output_tokens")

    nodes, links = [], []

    def node(nid, label, col):
        nodes.append({"id": nid, "label": label, "col": col})

    def link(src, dst, value, kind):
        if value > 0:
            links.append({"source": src, "target": dst, "value": value, "kind": kind})

    claude_total = sum(claude.values())
    broker_total = sum(by_model.values()) + failed_tokens
    node("req", f"Requêtes ({len(reqs)})", 0)
    link("req", "claude", claude_total, "claude")
    link("req", "broker", broker_total, "delegated")
    if claude_total:
        node("claude", "Claude", 1)
        for nid, key, label in (("cache_read", "cache_read", "Contexte relu (cache)"),
                                ("cache_create", "cache_create", "Contexte écrit"),
                                ("fresh_in", "input", "Entrée fraîche"),
                                ("claude_out", "output", "Sortie Claude")):
            if claude[key]:
                node(nid, label, 2)
                link("claude", nid, claude[key], "claude")
    if broker_total:
        node("broker", "Courtier", 1)
        for key, val in sorted(by_model.items(), key=lambda kv: -kv[1]):
            if val:
                node("m:" + key, key, 2)
                link("broker", "m:" + key, val, "delegated")
        if failed_tokens:
            node("failed", "Tentatives en échec", 2)
            link("broker", "failed", failed_tokens, "failed")

    fresh = claude["input"] + claude["cache_create"] + claude["output"]
    return {
        "nodes": nodes,
        "links": links,
        "totals": {
            "requests": len(reqs),
            "claude_tokens": claude_total,
            "claude_fresh": fresh,
            "delegated_tokens": broker_total,
            "delegated_calls": delegated_calls,
            "delegated_failed": delegated_failed,
            "failed_tokens": failed_tokens,
            "tools_calls": tools_calls,
            "cost_usd": round(cost, 6),
            # Les tentatives en échec restent visibles dans le Sankey (gaspillage)
            # mais hors numérateur : elles n'ont rien produit.
            "useful_delegated_tokens": broker_total - failed_tokens,
            "share_delegated": _share(broker_total - failed_tokens, fresh),
            "broker_by_mode": by_mode,
        },
    }


def overview(data_root, since_ts=None, until_ts=None, project=None):
    """Liste + Sankey + projets, en une seule lecture des fichiers."""
    reqs = load_all(data_root)
    kept = _filter(reqs, since_ts, until_ts, project)
    return {
        "requests": [summary(r) for r in kept],
        "sankey": sankey(kept),
        "projects": sorted({r.get("project") for r in reqs if r.get("project")}),
    }
