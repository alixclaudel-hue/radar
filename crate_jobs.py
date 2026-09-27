"""
crate_jobs.py — point d'entrée du worker des tâches longues de Crate Radar.

N'est PAS lancé à la main : radar_web/worker.py l'invoque en sous-processus
    python crate_jobs.py <job> <params_json>
et suit la progression via jobs/<uid>/<job>.status.json. Un fichier
jobs/<uid>/<job>.stop demande l'arrêt propre.

Le code des jobs vit dans le paquet radar_jobs/ (un module par domaine, cf.
radar_jobs/__init__.py). Seul le module du job demandé est importé : un
sous-processus ne charge pas le code des 23 autres.
"""

import importlib
import json
import sys

# job -> module de radar_jobs qui définit job_<nom>
JOBS = {
    "seller_inventory": "search",
    "scan_catalog": "search",
    "import_discogs_dump": "dump",
    "build_catalog_labelgraph": "dump",
    "ingest_youtube": "ingest",
    "ingest_spotify": "ingest",
    "ingest_bandcamp": "ingest",
    "fetch_collection": "ingest",
    "merge_corpus": "ingest",
    "profile_labels": "ingest",
    "resolve_artists": "graph",
    "build_graph": "graph",
    "search_base": "search",
    "ingest_djsets": "djsets",
    "scan_sellers": "search",
    "enrich": "curation",
    "canonicalize": "curation",
    "scan_veille": "search",
    "scan_recos": "recos",
    "publish_recos": "recos",
    "scorestore_releases": "scorestore",
    "scorestore_tracks": "scorestore",
    "prune_labels": "curation",
    "reco_index": "scorestore",
}


def job_function(name):
    return getattr(importlib.import_module(f"radar_jobs.{JOBS[name]}"), f"job_{name}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in JOBS:
        raise SystemExit(f"usage: python crate_jobs.py <{'|'.join(JOBS)}> [params_json]")
    from radar_jobs.common import Job

    name = sys.argv[1]
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else {}
    job = Job(name, total=int(params.get("_total", 0)))
    try:
        job_function(name)(job, params)
    except Exception as e:
        job.finish(error=f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
