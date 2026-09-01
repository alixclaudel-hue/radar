"""File d'attente des tâches de fond (Option A, étape 3).

`launch()` **empile** dans /data/jobs/queue.json. Un worker unique
(`radar_web.worker`, service compose `radar-worker`) dépile un job à la fois,
en round-robin entre utilisateurs, et lance `crate_jobs.py` avec RADAR_UID.
Le suivi par utilisateur est écrit par `crate_jobs.Job` dans
/data/jobs/<uid>/<name>.status.json.
"""
import json
import os
import secrets
import time

from . import paths, store

QUEUE_PATH = os.path.join(paths.JOBS_DIR, "queue.json")


def _user_jobs_dir(uid):
    d = os.path.join(paths.JOBS_DIR, uid)
    os.makedirs(d, exist_ok=True)
    return d


def _status_path(name, uid):
    return os.path.join(_user_jobs_dir(uid), f"{name}.status.json")


def load_queue():
    try:
        with open(QUEUE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_queue(q):
    os.makedirs(paths.JOBS_DIR, exist_ok=True)
    tmp = QUEUE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(q, f)
    os.replace(tmp, QUEUE_PATH)


def _raw_status(name, uid):
    p = _status_path(name, uid)
    try:
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        s["_age"] = time.time() - os.path.getmtime(p)
        return s
    except (OSError, ValueError):
        return None


def status(name, uid=None):
    """Statut compatible templates : {running, done, total, message, error, ...}
    + éventuellement {queued: True, position: N}."""
    uid = uid or store.current_uid()
    q = load_queue()
    mine = [j for j in q if j["uid"] == uid and j["name"] == name]
    if mine and mine[0]["state"] == "queued":
        pos = sum(1 for j in q if j["state"] == "queued"
                  and q.index(j) <= q.index(mine[0]))
        return {"job": name, "queued": True, "running": False, "done": 0, "total": 0,
                "message": f"en file d'attente (position {pos})", "last": "", "error": None}
    s = _raw_status(name, uid)
    if s and any(j["uid"] == uid and j["name"] == name and j["state"] == "running" for j in q):
        s["running"] = True
    return s


def running(name, uid=None):
    s = status(name, uid)
    return bool(s and (s.get("running") or s.get("queued")))


def launch(name, params=None, uid=None):
    """Empile le job. Refuse un doublon (déjà en file ou en cours pour cet uid)."""
    uid = uid or store.current_uid()
    q = load_queue()
    if any(j["uid"] == uid and j["name"] == name and j["state"] in ("queued", "running")
           for j in q):
        return False
    s = _raw_status(name, uid)
    if s and s.get("running") and s.get("_age", 999) < 150:
        return False
    q.append({"id": secrets.token_hex(6), "uid": uid, "name": name,
              "params": params or {}, "ts": time.time(), "state": "queued"})
    save_queue(q)
    return True


def stop(name, uid=None):
    uid = uid or store.current_uid()
    # retire de la file si encore en attente
    q = [j for j in load_queue()
         if not (j["uid"] == uid and j["name"] == name and j["state"] == "queued")]
    save_queue(q)
    open(os.path.join(_user_jobs_dir(uid), f"{name}.stop"), "w").close()
