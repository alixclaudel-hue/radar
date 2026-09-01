"""Emplacements des données — PARTAGÉS avec l'appli Streamlit et crate_jobs.py.
Les deux fronts lisent/écrivent les mêmes fichiers JSON (même CRATE_DATA_DIR)."""
import os

# racine du projet = dossier parent de radar_web/
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.environ.get("CRATE_DATA_DIR") or _REPO
JOBS_SCRIPT = os.path.join(_REPO, "crate_jobs.py")

os.makedirs(os.path.join(DATA, "jobs"), exist_ok=True)

CONFIG        = os.path.join(DATA, "crate_radar_config.json")
RESOLVED      = os.path.join(DATA, "labels_resolved.json")
PROFILE       = os.path.join(DATA, "labels_profile.json")
COLLECTION    = os.path.join(DATA, "collection_cache.json")
CORPUS        = os.path.join(DATA, "taste_corpus.json")
LOOKUP_CACHE  = os.path.join(DATA, "lookup_cache.json")
GRAPH         = os.path.join(DATA, "producer_graph.json")
ARTISTS_RES   = os.path.join(DATA, "artists_resolved.json")
HISTORY       = os.path.join(DATA, "search_history.json")
FEEDBACK      = os.path.join(DATA, "reco_feedback.json")
SCORING_PROF  = os.path.join(DATA, "scoring_profiles.json")
PENDING_ENRICH = os.path.join(DATA, "pending_enrich.json")
YOUTUBE_META  = os.path.join(DATA, "youtube_meta.json")
SPOTIFY_META  = os.path.join(DATA, "spotify_meta.json")
RELEASE_META  = os.path.join(DATA, "release_meta_cache.json")
SEARCH_HIST   = os.path.join(DATA, "radar_web_searches.json")
VEILLE_NEW    = os.path.join(DATA, "veille_new.json")
VEILLE_SEEN   = os.path.join(DATA, "veille_seen.json")
SELLERS_NEW   = os.path.join(DATA, "seller_new.json")
SELLERS_SEEN  = os.path.join(DATA, "sellers_seen.json")
JOBS_DIR      = os.path.join(DATA, "jobs")
