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

from .radar import discogs_dump, jobs, paths, sellers

POLL = 2.0
JOB_TIMEOUT = 6 * 3600
# Scan hebdo du catalogue de vendeurs : opt-in via RADAR_SELLER_SCAN=1.
SELLER_SCAN_EVERY = 7 * 86400
_last_seller_check = 0.0
# Import mensuel du dump Discogs : opt-in via RADAR_DISCOGS_DUMP_SYNC=1.
# Un dump mensuel est un instantané complet (jamais un delta) — on revérifie
# une fois par jour si un nouveau mois est disponible, sans jamais retélécharger
# inutilement (import_discogs_dump compare lui-même au dernier dump connu).
DUMP_SYNC_CHECK_EVERY = 86400
_last_dump_check = 0.0


def _maybe_weekly_scan():
    """Enfile scan_catalog (owner) si aucun vendeur n'a été scanné depuis 7 j."""
    global _last_seller_check
    if os.environ.get("RADAR_SELLER_SCAN") != "1":
        return
    if time.time() - _last_seller_check < 3600:
        return
    _last_seller_check = time.time()
    try:
        cat = sellers.load_catalog()
        newest = max((e.get("last_scan") or "" for e in cat.values()), default="")
        stale = True
        if newest:
            from datetime import datetime
            stale = datetime.fromisoformat(newest).timestamp() < time.time() - SELLER_SCAN_EVERY
        q = jobs.load_queue()
        running = any(j["name"] == "scan_catalog" for j in q)
        if stale and not running:
            jobs.launch("scan_catalog", {}, uid=paths.DEFAULT_UID)
            print("[worker] scan_catalog hebdo enfilé", file=sys.stderr, flush=True)
    except Exception as e:                       # noqa: BLE001
        print(f"[worker] weekly scan check : {e}", file=sys.stderr, flush=True)


def _maybe_monthly_dump_sync():
    """Enfile import_discogs_dump (owner) si un nouveau dump mensuel est
    publié. Ne télécharge rien tant que le mois n'a pas changé — un simple
    appel de listing, pas le fichier de plusieurs Go."""
    global _last_dump_check
    if os.environ.get("RADAR_DISCOGS_DUMP_SYNC") != "1":
        return
    if time.time() - _last_dump_check < DUMP_SYNC_CHECK_EVERY:
        return
    _last_dump_check = time.time()
    try:
        q = jobs.load_queue()
        if any(j["name"] == "import_discogs_dump" for j in q):
            return
        latest = discogs_dump.find_latest_dump_date()
        if latest != discogs_dump.get_meta().get("dump_date"):
            jobs.launch("import_discogs_dump", {}, uid=paths.DEFAULT_UID)
            print(f"[worker] import_discogs_dump enfilé (nouveau dump {latest})",
                  file=sys.stderr, flush=True)
    except Exception as e:                       # noqa: BLE001
        print(f"[worker] monthly dump check : {e}", file=sys.stderr, flush=True)


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
    for j in jobs.reap_orphans():
        print(f"[worker] job orphelin nettoyé (redéploiement pendant l'exécution) : "
              f"{j['uid']}/{j['name']}", file=sys.stderr, flush=True)
    last_uid = None
    while True:
        q = jobs.load_queue()
        job = _pick(q, last_uid)
        if not job:
            _maybe_weekly_scan()
            _maybe_monthly_dump_sync()
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
