"""Emplacements des données.

Multi-utilisateur (Option A, cf. docs/architecture.md étape 1) :

    <DATA>/
      users/<uid>/   ← tout ce qui touche au goût d'un utilisateur
      shared/        ← caches neutres (métadonnées Discogs/YouTube publiques)
      jobs/          ← statuts des jobs (file par-user viendra à l'étape 3)

`user_paths(uid)` renvoie l'ensemble des chemins d'un utilisateur (+ les
partagés). Les constantes de module (`CONFIG`, `PROFILE`, …) restent définies
pour l'`uid` par défaut afin que le code pas encore migré continue de tourner.
"""
import os
from types import SimpleNamespace

# racine du projet = dossier parent de radar_web/
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.environ.get("CRATE_DATA_DIR") or _REPO
JOBS_SCRIPT = os.path.join(_REPO, "crate_jobs.py")

DEFAULT_UID = "owner"
USERS_DIR = os.path.join(DATA, "users")
SHARED_DIR = os.path.join(DATA, "shared")
JOBS_DIR = os.path.join(DATA, "jobs")

os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(SHARED_DIR, exist_ok=True)

# fichier -> portée
_PER_USER = {
    "config": "crate_radar_config.json",
    "resolved": "labels_resolved.json",
    "profile": "labels_profile.json",
    "collection": "collection_cache.json",
    "corpus": "taste_corpus.json",
    "graph": "producer_graph.json",
    "artists_res": "artists_resolved.json",
    "history": "search_history.json",
    "feedback": "reco_feedback.json",
    "scoring_prof": "scoring_profiles.json",
    "pending_enrich": "pending_enrich.json",
    "youtube_meta": "youtube_meta.json",
    "spotify_meta": "spotify_meta.json",
    "search_hist": "radar_web_searches.json",
    "veille_new": "veille_new.json",
    "veille_seen": "veille_seen.json",
    "sellers_new": "seller_new.json",
    "sellers_seen": "sellers_seen.json",
    "djset_seen": "djset_seen.json",
    "canon_state": "canonicalize.state.json",
    "search_results": "search_results.json",
    "label_graphs": "label_graphs.json",
    "artist_graphs": "artist_graphs.json",
    "cart": "cart.json",
    "ui_notes": "ui_notes.json",
    "recos_seen": "recos_seen.json",
    "recos_candidates": "recos_candidates.json",
    "recos_history": "recos_playlist_history.json",
    "youtube_oauth": "youtube_oauth.json",
    "youtube_watch_state": "youtube_watch_state.json",
}
_SHARED = {
    "lookup_cache": "lookup_cache.json",
    "release_meta": "release_meta_cache.json",
}

# noms historiques des fichiers, à la racine de <DATA> (avant migration)
_LEGACY_NAMES = {**_PER_USER, **_SHARED}


def _valid_uid(uid):
    if uid is None:
        raise RuntimeError(
            "uid courant absent : requête hors du middleware d'authentification "
            "(fail-closed volontaire — ne jamais retomber sur le propriétaire).")
    uid = str(uid).strip()
    if not uid or "/" in uid or uid.startswith(".") or "\x00" in uid:
        raise ValueError(f"uid invalide : {uid!r}")
    return uid


def user_dir(uid=DEFAULT_UID):
    d = os.path.join(USERS_DIR, _valid_uid(uid))
    os.makedirs(d, exist_ok=True)
    return d


def user_paths(uid=DEFAULT_UID):
    """Namespace des chemins d'un utilisateur (per-user sous users/<uid>/,
    partagés sous shared/)."""
    ud = user_dir(uid)
    ns = {k: os.path.join(ud, fn) for k, fn in _PER_USER.items()}
    ns.update({k: os.path.join(SHARED_DIR, fn) for k, fn in _SHARED.items()})
    ns["dir"] = ud
    ns["uid"] = _valid_uid(uid)
    ns["jobs_dir"] = JOBS_DIR
    return SimpleNamespace(**ns)


def migrate_layout(uid=DEFAULT_UID):
    """Déplace les fichiers historiques de <DATA>/*.json vers users/<uid>/ et
    shared/. Idempotent : ne fait rien si <DATA>/crate_radar_config.json est absent
    (déjà migré) et n'écrase jamais une cible existante."""
    legacy_cfg = os.path.join(DATA, _LEGACY_NAMES["config"])
    if not os.path.isfile(legacy_cfg):
        return []
    moved = []
    for key, fn in _PER_USER.items():
        src, dst = os.path.join(DATA, fn), os.path.join(user_dir(uid), fn)
        if os.path.isfile(src) and not os.path.exists(dst):
            os.rename(src, dst)
            moved.append(fn)
    for key, fn in _SHARED.items():
        src, dst = os.path.join(DATA, fn), os.path.join(SHARED_DIR, fn)
        if os.path.isfile(src) and not os.path.exists(dst):
            os.rename(src, dst)
            moved.append(fn)
    return moved


# ---- constantes de compat : chemins de l'utilisateur par défaut ----
_D = user_paths(DEFAULT_UID)
CONFIG         = _D.config
RESOLVED       = _D.resolved
PROFILE        = _D.profile
COLLECTION     = _D.collection
CORPUS         = _D.corpus
LOOKUP_CACHE   = _D.lookup_cache
GRAPH          = _D.graph
ARTISTS_RES    = _D.artists_res
HISTORY        = _D.history
FEEDBACK       = _D.feedback
SCORING_PROF   = _D.scoring_prof
PENDING_ENRICH = _D.pending_enrich
YOUTUBE_META   = _D.youtube_meta
SPOTIFY_META   = _D.spotify_meta
RELEASE_META   = _D.release_meta
SEARCH_HIST    = _D.search_hist
VEILLE_NEW     = _D.veille_new
VEILLE_SEEN    = _D.veille_seen
SELLERS_NEW    = _D.sellers_new
SELLERS_SEEN   = _D.sellers_seen
