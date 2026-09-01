"""Lancement / suivi des tâches de fond — réutilise crate_jobs.py tel quel."""
import json
import os
import subprocess
import sys
import time

from . import paths, store


def _status_path(name):
    return os.path.join(paths.JOBS_DIR, f"{name}.status.json")


def status(name):
    p = _status_path(name)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        s["_age"] = time.time() - os.path.getmtime(p)
        return s
    except (OSError, ValueError):
        return None


def running(name):
    s = status(name)
    return bool(s and s.get("running"))


def launch(name, params=None, uid=None):
    """Refuse si une exécution fraîche (< 150 s) tourne déjà. Le job tourne pour
    `uid` (défaut : l'utilisateur de la requête courante) via RADAR_UID."""
    uid = uid or store.current_uid()
    s = status(name)
    if s and s.get("running") and s.get("_age", 999) < 150:
        return False
    os.makedirs(paths.JOBS_DIR, exist_ok=True)
    with open(_status_path(name), "w", encoding="utf-8") as f:
        json.dump({"job": name, "running": True, "done": 0, "total": 0,
                   "message": "démarrage…", "last": "", "error": None}, f)
    subprocess.Popen(
        [sys.executable, paths.JOBS_SCRIPT, name, json.dumps(params or {})],
        cwd=os.path.dirname(paths.JOBS_SCRIPT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "CRATE_DATA_DIR": paths.DATA, "RADAR_UID": uid})
    return True


def stop(name):
    open(os.path.join(paths.JOBS_DIR, f"{name}.stop"), "w").close()
