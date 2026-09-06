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
from datetime import datetime

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

# Entretien de fond (nettoyage canonique, profilage des labels, graphe
# producteur global) : opt-in via RADAR_AUTO_MAINTENANCE=1, pour ne plus
# dépendre de boutons dans Réglages. Cadence par job lue directement depuis
# le statut persisté du job (finished_at, sans erreur) — jamais depuis
# l'instant où on l'a lancé (cf. plus bas) : un déploiement interrompt un job
# en cours en plein milieu (n'importe lequel, pas seulement ceux qui touchent
# au dump), et il ne doit alors jamais compter comme "fait" pour sa cadence,
# sous peine d'attendre jusqu'à 30 j (build_graph) avant d'être retenté.
AUTO_MAINT_EVERY = {"canonicalize": 7 * 86400, "profile_labels": 7 * 86400, "build_graph": 30 * 86400}
_last_auto_maint_check = 0.0

# RECOS RADAR (lots 1+2, candidats + publication) : opt-in via RADAR_RECOS_SCAN=1, un
# scan par jour. Fonctionnalité personnelle (un seul owner en pratique) -> pas de
# round-robin par utilisateur nécessaire, mêmes conventions que les scans ci-dessus.
RECOS_SCAN_EVERY = 86400
_last_recos_check = 0.0

# RECOS RADAR (lot 3, nettoyage par historique de visionnage) : opt-in SÉPARÉ via
# RADAR_RECOS_CLEANUP=1 — seule vraie inconnue technique du lot (scraping Playwright
# non documenté, cf. radar/ytwatch.py) : un opt-in dédié pour ne jamais bloquer
# scan_recos/publish_recos (API stables) si ce nettoyage casse.
RECOS_CLEANUP_EVERY = 86400
_last_recos_cleanup_check = 0.0


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


def _last_successful_run(name):
    """Timestamp de la DERNIÈRE exécution qui est allée à son terme sans erreur
    (0.0 si jamais complétée) — lu depuis le statut persisté du job lui-même,
    jamais depuis une marque posée au lancement (cf. commentaire plus haut :
    c'était le bug qui a laissé build_graph "oublié" 30 j après une
    interruption par déploiement)."""
    s = jobs.status(name, uid=paths.DEFAULT_UID)
    if not s or s.get("running") or s.get("error") or not s.get("finished_at"):
        return 0.0
    try:
        return datetime.fromisoformat(s["finished_at"]).timestamp()
    except ValueError:
        return 0.0


def _maybe_auto_maintenance():
    """Enfile canonicalize / profile_labels / build_graph (owner) chacun selon
    sa propre cadence, sans action de l'utilisateur — remplace les boutons
    manuels retirés de Réglages."""
    global _last_auto_maint_check
    if os.environ.get("RADAR_AUTO_MAINTENANCE") != "1":
        return
    if time.time() - _last_auto_maint_check < 3600:
        return
    _last_auto_maint_check = time.time()
    try:
        queued_names = {j["name"] for j in jobs.load_queue()}
        now = time.time()
        for name, params in (("canonicalize", {"scope": "corpus"}),
                              ("profile_labels", {"limit": 150}),
                              ("build_graph", {"mode": "taste"})):
            if name in queued_names or now - _last_successful_run(name) < AUTO_MAINT_EVERY[name]:
                continue
            jobs.launch(name, params, uid=paths.DEFAULT_UID)
            print(f"[worker] entretien de fond enfilé : {name}", file=sys.stderr, flush=True)
    except Exception as e:                       # noqa: BLE001
        print(f"[worker] auto maintenance check : {e}", file=sys.stderr, flush=True)


def _maybe_recos_scan():
    """Enfile scan_recos (owner) une fois par jour — remplit recos_candidates.json,
    consommé plus tard par l'ajout à la playlist YouTube (lot 2, pas encore fait)."""
    global _last_recos_check
    if os.environ.get("RADAR_RECOS_SCAN") != "1":
        return
    if time.time() - _last_recos_check < 3600:
        return
    _last_recos_check = time.time()
    try:
        queued_names = {j["name"] for j in jobs.load_queue()}
        if "scan_recos" in queued_names or time.time() - _last_successful_run("scan_recos") < RECOS_SCAN_EVERY:
            return
        jobs.launch("scan_recos", {}, uid=paths.DEFAULT_UID)
        print("[worker] scan_recos quotidien enfilé", file=sys.stderr, flush=True)
    except Exception as e:                       # noqa: BLE001
        print(f"[worker] recos scan check : {e}", file=sys.stderr, flush=True)


def _maybe_recos_cleanup():
    """Enfile clean_recos (owner) une fois par jour — retire de la playlist RECOS
    RADAR ce qui a déjà été écouté. Opt-in séparé de scan_recos/publish_recos (cf.
    RECOS_CLEANUP_EVERY plus haut)."""
    global _last_recos_cleanup_check
    if os.environ.get("RADAR_RECOS_CLEANUP") != "1":
        return
    if time.time() - _last_recos_cleanup_check < 3600:
        return
    _last_recos_cleanup_check = time.time()
    try:
        queued_names = {j["name"] for j in jobs.load_queue()}
        if ("clean_recos" in queued_names
                or time.time() - _last_successful_run("clean_recos") < RECOS_CLEANUP_EVERY):
            return
        jobs.launch("clean_recos", {}, uid=paths.DEFAULT_UID)
        print("[worker] clean_recos quotidien enfilé", file=sys.stderr, flush=True)
    except Exception as e:                       # noqa: BLE001
        print(f"[worker] recos cleanup check : {e}", file=sys.stderr, flush=True)


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
            _maybe_auto_maintenance()
            _maybe_recos_scan()
            _maybe_recos_cleanup()
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
