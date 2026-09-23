"""Lecture de la file d'attente et des statuts de jobs, sans effet de bord.

Disposition réelle sur disque (cf. `radar_web/radar/jobs.py`) :
  <JOBS_DIR>/queue.json              liste d'entrées {id, uid, name, ts, state, priority}
  <JOBS_DIR>/<uid>/<name>.status.json  un fichier PAR job ET par utilisateur

Ce module ne réutilise pas `jobs.status()` : cette fonction résout l'uid
courant depuis le contexte de requête de l'appli web et ne répond que pour un
job nommé. Le diagnostic veut l'inverse — tout le monde, tous les jobs, sans
contexte utilisateur.
"""
import json
import os
import time

_SUFFIX = ".status.json"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None                       # fichier absent ou écriture en cours


def _num(v, default=0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def read_queue(jobs_dir):
    """File d'attente, dans l'ordre de service du worker.

    `priority` vaut 1 par défaut et non 0 : c'est la valeur que pose
    `jobs.launch()` pour un clic utilisateur, et que relit `worker._pick()`.
    Prendre 0 par défaut afficherait les jobs interactifs derrière les jobs de
    fond, l'inverse du comportement réel."""
    raw = _read_json(os.path.join(jobs_dir, "queue.json"))
    if not isinstance(raw, list):
        return []
    now = time.time()
    entries = [e for e in raw if isinstance(e, dict)]

    def order(e):
        running_first = 0 if e.get("state") == "running" else 1
        return (running_first, -_num(e.get("priority"), 1), _num(e.get("ts")))

    out, position = [], 0
    for e in sorted(entries, key=order):
        item = dict(e)
        item["wait_s"] = max(0.0, now - _num(e.get("ts"), now))
        if e.get("state") == "queued":
            position += 1
            item["position"] = position
        else:
            item["position"] = None
        out.append(item)
    return out


def read_statuses(jobs_dir):
    """Un dict par fichier de statut trouvé, tous utilisateurs confondus."""
    now, out = time.time(), []
    try:
        uids = sorted(d for d in os.listdir(jobs_dir)
                      if os.path.isdir(os.path.join(jobs_dir, d)))
    except OSError:
        return out
    for uid in uids:
        d = os.path.join(jobs_dir, uid)
        try:
            names = sorted(f for f in os.listdir(d) if f.endswith(_SUFFIX))
        except OSError:
            continue
        for fn in names:
            p = os.path.join(d, fn)
            data = _read_json(p)
            if not isinstance(data, dict):
                continue
            try:
                age = max(0.0, now - os.path.getmtime(p))
            except OSError:
                continue
            done, total = _num(data.get("done")), _num(data.get("total"))
            item = dict(data)
            item.update({
                "uid": uid,
                "name": fn[:-len(_SUFFIX)],
                "done": done,
                "total": total,
                "age_s": age,
                "pct": round(done / total * 100, 1) if total > 0 else None,
            })
            out.append(item)
    return out


def snapshot(jobs_dir):
    return {"ts": time.time(),
            "queue": read_queue(jobs_dir),
            "statuses": read_statuses(jobs_dir)}
