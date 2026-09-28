"""Socle partagé des jobs : répertoires de données, chemins des fichiers,
config utilisateur, E/S JSON, normalisation, et la classe `Job` (statut/progression).
"""

import json
import os
import re
import sys
from datetime import datetime

from radar_web.radar import store


# racine du dépôt (ce module vit dans radar_jobs/) : DATA par défaut et le
# fichier de cookies YouTube des DJ sets en dépendent
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Répertoire des données : partagé avec radar_web via CRATE_DATA_DIR (volume
# persistant en conteneur). Défaut aligné sur radar_web/radar/paths.py (data/
# à côté du script, jamais la racine du repo elle-même) — sans ça un lancement
# manuel sans CRATE_DATA_DIR exporté écrit hors du volume monté par Docker
# (incident du 12/09 : réimport dump écrit dans <repo>/shared au lieu de
# <repo>/data/shared).
DATA = os.environ.get("CRATE_DATA_DIR") or os.path.join(HERE, "data")
print(f"[crate_jobs] DATA={DATA}", file=sys.stderr)
# Multi-utilisateur (cf. docs/architecture.md étape 1) : chaque job tourne pour un
# utilisateur (RADAR_UID, défaut "owner") — données sous users/<uid>/, caches
# neutres sous shared/. Doit rester aligné avec radar_web/radar/paths.py.
RADAR_UID = (os.environ.get("RADAR_UID") or "owner").strip() or "owner"
USER_DIR = os.path.join(DATA, "users", RADAR_UID)
SHARED_DIR = os.path.join(DATA, "shared")
JOBS_DIR = os.path.join(DATA, "jobs")
JOBS_USER_DIR = os.path.join(JOBS_DIR, RADAR_UID)  # statuts/inputs par utilisateur (étape 3)
for _d in (USER_DIR, SHARED_DIR, JOBS_DIR, JOBS_USER_DIR):
    os.makedirs(_d, exist_ok=True)


def _migrate_layout():
    """<DATA>/*.json -> users/<uid>/ + shared/. Idempotent."""
    legacy_cfg = os.path.join(DATA, "crate_radar_config.json")
    if not os.path.isfile(legacy_cfg):
        return
    per_user = ("crate_radar_config.json", "taste_corpus.json", "labels_resolved.json",
                "labels_profile.json", "collection_cache.json", "artists_resolved.json",
                "producer_graph.json", "search_history.json", "reco_feedback.json",
                "scoring_profiles.json", "pending_enrich.json", "youtube_meta.json",
                "spotify_meta.json", "radar_web_searches.json", "veille_new.json",
                "veille_seen.json", "seller_new.json", "sellers_seen.json",
                "djset_seen.json", "search_results.json", "canonicalize.state.json")
    shared = ("lookup_cache.json", "release_meta_cache.json")
    for fn in per_user:
        s, d = os.path.join(DATA, fn), os.path.join(USER_DIR, fn)
        if os.path.isfile(s) and not os.path.exists(d):
            os.rename(s, d)
    for fn in shared:
        s, d = os.path.join(DATA, fn), os.path.join(SHARED_DIR, fn)
        if os.path.isfile(s) and not os.path.exists(d):
            os.rename(s, d)


_migrate_layout()

CONFIG_PATH = os.path.join(USER_DIR, "crate_radar_config.json")
CORPUS_PATH = os.path.join(USER_DIR, "taste_corpus.json")
LOOKUP_CACHE_PATH = os.path.join(SHARED_DIR, "lookup_cache.json")
RESOLVED_PATH = os.path.join(USER_DIR, "labels_resolved.json")
PROFILE_PATH = os.path.join(USER_DIR, "labels_profile.json")
COLLECTION_CACHE_PATH = os.path.join(USER_DIR, "collection_cache.json")
CART_PATH = os.path.join(USER_DIR, "cart.json")  # wantlist Discogs locale (F11)
ARTISTS_RESOLVED_PATH = os.path.join(USER_DIR, "artists_resolved.json")
PRODUCER_GRAPH_PATH = os.path.join(USER_DIR, "producer_graph.json")
YOUTUBE_META_PATH = os.path.join(USER_DIR, "youtube_meta.json")
SPOTIFY_META_PATH = os.path.join(USER_DIR, "spotify_meta.json")
SEARCH_INPUT_PATH = os.path.join(JOBS_USER_DIR, "search_base.input.json")
SEARCH_RESULTS_PATH = os.path.join(USER_DIR, "search_results.json")
DJSET_INPUT_PATH = os.path.join(JOBS_USER_DIR, "djsets.input.json")
DJSET_SEEN_PATH = os.path.join(USER_DIR, "djset_seen.json")
SELLERS_SEEN_PATH = os.path.join(USER_DIR, "sellers_seen.json")
SELLERS_NEW_PATH = os.path.join(USER_DIR, "seller_new.json")
RECOS_CANDIDATES_PATH = os.path.join(USER_DIR, "recos_candidates.json")
RECOS_HISTORY_PATH = os.path.join(USER_DIR, "recos_playlist_history.json")
RECOS_PLAYLIST_PATH = os.path.join(USER_DIR, "recos_playlist.json")
RECOS_SEARCH_BUDGET_PATH = os.path.join(USER_DIR, "recos_search_budget.json")


# =================================================== enrichissement auto + canonique

PENDING_ENRICH_PATH = os.path.join(USER_DIR, "pending_enrich.json")
VEILLE_SEEN_PATH = os.path.join(USER_DIR, "veille_seen.json")
VEILLE_NEW_PATH = os.path.join(USER_DIR, "veille_new.json")
ARTIST_STOPWORDS = {"various artists", "various", "va", "unknown artist", "unknown",
                    "release", "progressive classics", "no artist", "traxsource"}


# ============================================================= util

def load_json(path, default):
    """Volontairement PAS `store.load` : un JSON corrompu doit faire échouer le
    job bruyamment. Ici la plupart des lectures sont suivies d'une réécriture du
    même fichier (corpus, caches, seen) — retomber en silence sur le défaut vide
    écraserait les données au lieu de signaler le problème."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    """Écriture atomique partagée avec l'appli web (nom temporaire unique :
    worker et web écrivent les mêmes fichiers, cf. store.save)."""
    store.save(path, data)


def cfg_load():
    """Config + secrets d'environnement (déploiement).

    Les secrets du .env n'amorcent QUE le compte propriétaire : un job lancé pour
    un invité ne doit jamais tourner avec le token Discogs du propriétaire."""
    d = load_json(CONFIG_PATH, {})
    if RADAR_UID != "owner":
        return d
    for key, env in (("token", "DISCOGS_TOKEN"), ("youtube_api_key", "YOUTUBE_API_KEY"),
                     ("spotify_client_id", "SPOTIFY_CLIENT_ID"),
                     ("spotify_client_secret", "SPOTIFY_CLIENT_SECRET"),
                     ("bandcamp_sub_user", "BANDCAMP_SUB_USER"),
                     ("bandcamp_sub_pass", "BANDCAMP_SUB_PASS")):
        if not d.get(key) and os.environ.get(env):
            d[key] = os.environ[env]
    return d


def normalize_label(name):
    n = (name or "").strip().lower()
    n = re.sub(r"\s*\(\d+\)\s*$", "", n)
    n = re.sub(r"\s+", " ", n)
    return n.strip()


def style_key(s):
    return re.sub(r"\s+", " ", (s or "").lower().replace("-", " ")).strip()


class Job:
    """Écrit jobs/<name>.status.json et surveille jobs/<name>.stop."""

    def __init__(self, name, total=0):
        os.makedirs(JOBS_USER_DIR, exist_ok=True)
        self.name = name
        self.status_path = os.path.join(JOBS_USER_DIR, f"{name}.status.json")
        self.stop_path = os.path.join(JOBS_USER_DIR, f"{name}.stop")
        if os.path.exists(self.stop_path):
            os.remove(self.stop_path)
        self.st = {"job": name, "running": True, "done": 0, "total": total,
                   "last": "", "message": "", "error": None, "log": [],
                   "started_at": datetime.now().isoformat(timespec="seconds"),
                   "finished_at": None}
        self.flush()

    def _log(self, line):
        """Journal complet (contrairement à `last`, écrasé à chaque tick) —
        sert au diagnostic à distance (bouton télécharger, cf. page Reco Radar)."""
        if not line:
            return
        self.st.setdefault("log", []).append(line)
        self.st["log"] = self.st["log"][-1000:]

    def flush(self):
        save_json(self.status_path, self.st)

    def stopped(self):
        return os.path.exists(self.stop_path)

    def tick(self, last="", inc=1, total=None):
        self.st["done"] += inc
        if last:
            self.st["last"] = last
            self._log(last)
        if total is not None:
            self.st["total"] = total
        self.flush()

    def msg(self, m):
        self.st["message"] = m
        self._log(m)
        self.flush()

    def sub(self, done=None, total=None, label=None):
        """Sous-progression optionnelle (ex. articles du vendeur en cours dans
        scan_catalog), affichée par job_status_frag en plus de done/total."""
        if done is not None:
            self.st["sub_done"] = done
        if total is not None:
            self.st["sub_total"] = total
        if label is not None:
            self.st["sub_label"] = label
        self.flush()

    def finish(self, message="", error=None):
        self.st.update(running=False, message=message or self.st["message"],
                       error=error, finished_at=datetime.now().isoformat(timespec="seconds"))
        self._log(message or error)
        if self.stopped():
            os.remove(self.stop_path)
        self.flush()
