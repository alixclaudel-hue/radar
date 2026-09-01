"""Worker unique de la file d'attente des jobs (Option A, étape 3).

Lancé comme service compose `radar-worker` (même image que radar-web,
`command: python -m radar_web.worker`). Dépile /data/jobs/queue.json un job à
la fois — en round-robin entre utilisateurs pour qu'un gros backlog d'un
utilisateur n'affame pas les autres — et exécute `crate_jobs.py` avec RADAR_UID.

Exécution SÉRIELLE : c'est ce qui protège le rate-limit Discogs partagé (une
seule IP) quand plusieurs utilisateurs lancent des jobs.
"""
import json
import os
import subprocess
import sys
import time

from .radar import jobs, paths

POLL = 2.0
JOB_TIMEOUT = 6 * 3600


def _pick(q, last_uid):
    pend = [j for j in q if j["state"] == "queued"]
    if not pend:
        return None
    for j in pend:                      # round-robin : préfère un autre utilisateur
        if j["uid"] != last_uid:
            return j
    return pend[0]


def _run(job):
    env = {**os.environ, "CRATE_DATA_DIR": paths.DATA, "RADAR_UID": job["uid"]}
    try:
        subprocess.run(
            [sys.executable, paths.JOBS_SCRIPT, job["name"], json.dumps(job["params"] or {})],
            cwd=os.path.dirname(paths.JOBS_SCRIPT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=JOB_TIMEOUT)
    except Exception as e:                       # noqa: BLE001 — on log et on continue
        print(f"[worker] {job['uid']}/{job['name']} : {type(e).__name__} {e}",
              file=sys.stderr, flush=True)


def main():
    print("[worker] démarré", file=sys.stderr, flush=True)
    last_uid = None
    while True:
        q = jobs.load_queue()
        job = _pick(q, last_uid)
        if not job:
            time.sleep(POLL)
            continue
        job["state"] = "running"
        jobs.save_queue(q)
        print(f"[worker] run {job['uid']}/{job['name']}", file=sys.stderr, flush=True)
        _run(job)
        jobs.save_queue([j for j in jobs.load_queue() if j["id"] != job["id"]])
        last_uid = job["uid"]


if __name__ == "__main__":
    main()
