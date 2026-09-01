"""Lecture/écriture des fichiers de données + config + paramètres de scoring."""
import contextvars
import json
import os
import re
import tempfile

from . import paths

# --- utilisateur courant (posé par le middleware d'auth, une valeur par requête) ---
_current_uid = contextvars.ContextVar("radar_uid", default=paths.DEFAULT_UID)


def set_current_uid(uid):
    _current_uid.set(uid or paths.DEFAULT_UID)


def current_uid():
    return _current_uid.get()

# ------------------------------------------------------------------ JSON atomique

def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# Cache process-local pour les gros fichiers relus à chaque requête
# (labels_profile ~8 Mo, producer_graph ~4 Mo…). Invalidé sur (mtime, taille).
_LOAD_CACHE = {}


def load_cached(path, default):
    """Comme load(), mais réutilise le résultat tant que le fichier n'a pas changé.
    Renvoie l'objet mis en cache (à ne PAS muter) ; les jobs écrivent via save()."""
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        _LOAD_CACHE.pop(path, None)
        return default
    hit = _LOAD_CACHE.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    data = load(path, default)
    _LOAD_CACHE[path] = (sig, data)
    return data


def save(path, data):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ------------------------------------------------------------------ normalisation

def normalize_label(name):
    n = (name or "").strip().lower()
    n = re.sub(r"\s*\(\d+\)\s*$", "", n)
    n = re.sub(r"\s+", " ", n)
    return n.strip()


def style_key(s):
    return re.sub(r"\s+", " ", (s or "").lower().replace("-", " ")).strip()


# ------------------------------------------------------------------ scoring params

DEFAULT_TASTE_CATEGORIES = {
    "1": ["House", "Deep House", "Tech House", "Progressive House", "Deep Techno",
          "Hip-House", "Synth-pop"],
    "2": ["Downtempo", "Electro House", "Funk", "Future Jazz", "Acid House",
          "Techno", "Electro", "Minimal", "Breaks", "Prog Rock"],
}
DEFAULT_ARTIST_CATEGORIES = {"1": [], "2": []}
CAT2 = {"1": "Cœur", "2": "Aimés"}

DEFAULT_SCORING = {
    "taste_tiers":  {"1": 1.0, "2": 0.6, "3": 0.3},
    "artist_tiers": {"1": 1.0, "2": 0.5},
    "reco":  {"collection": 0.6, "corpus": 0.5, "artist": 0.4,
              "affinity": 0.4, "want_factor": 0.6},
    "album": {"label": 0.4, "artist": 0.4, "style": 0.2, "artist_max_vs_mean": 0.6},
    "artist_score": {"manual": 0.5, "corpus": 0.18, "collection": 0.1,
                     "graph": 0.14, "djset": 0.08},
    "graph": {"tier_w": {"1": 3.5, "2": 1.5, "3": 0.6, "none": 0.3},
              "artist_breadth": 0.4, "label_breadth": 0.5, "cat1_bonus": 2.0,
              "role_main": 1.0, "role_remix": 0.7, "role_other": 0.4,
              "max_credits": 6},
    "sources": {"discogs_collection": 1.0, "discogs_want": 0.6, "youtube": 0.5,
                "spotify": 0.5, "bandcamp": 0.9, "djset": 0.4},
    "label_affinity_floor": 0,
    "learn": {"l2": 2.0, "min_feedback": 12, "min_per_class": 3},
}
SOURCE_WEIGHTS = DEFAULT_SCORING["sources"]


def deep_merge(base, over):
    over = over or {}
    out = {}
    for k, v in base.items():
        if isinstance(v, dict):
            ov = over.get(k)
            out[k] = deep_merge(v, ov if isinstance(ov, dict) else {})
        else:
            out[k] = over.get(k, v)
    for k, v in over.items():
        if k not in base:
            out[k] = deep_merge(v, {}) if isinstance(v, dict) else v
    return out


_FLAT_WEIGHT_KEYS = ("taste_weights", "artist_weights", "reco_weights",
                     "album_weights", "artist_score_weights", "label_affinity_floor")

_ENV_SECRETS = (("token", "DISCOGS_TOKEN"), ("youtube_api_key", "YOUTUBE_API_KEY"),
                ("spotify_client_id", "SPOTIFY_CLIENT_ID"),
                ("spotify_client_secret", "SPOTIFY_CLIENT_SECRET"),
                ("bandcamp_sub_user", "BANDCAMP_SUB_USER"),
                ("bandcamp_sub_pass", "BANDCAMP_SUB_PASS"))


def load_config(uid=None):
    data = load(paths.user_paths(uid or current_uid()).config, {}) or {}
    data.setdefault("token", "")
    data.setdefault("labels", [])
    data.setdefault("watchlist", [])
    data.setdefault("sellers", [])
    data.setdefault("veille_rules", [])
    data.setdefault("taste_categories", {k: list(v) for k, v in DEFAULT_TASTE_CATEGORIES.items()})
    data.setdefault("artist_categories", {"1": [], "2": []})
    # migration poids éparses -> scoring
    if "scoring" not in data:
        sc = deep_merge(DEFAULT_SCORING, {})
        for src, dst in (("taste_weights", "taste_tiers"), ("artist_weights", "artist_tiers"),
                         ("reco_weights", "reco"), ("album_weights", "album"),
                         ("artist_score_weights", "artist_score")):
            if data.get(src):
                sc[dst] = {**sc[dst], **data[src]}
        if data.get("label_affinity_floor"):
            sc["label_affinity_floor"] = data["label_affinity_floor"]
        data["scoring"] = sc
    data["scoring"] = deep_merge(DEFAULT_SCORING, data.get("scoring", {}))
    data["scoring"]["artist_tiers"].pop("3", None)
    for k in _FLAT_WEIGHT_KEYS:
        data.pop(k, None)
    # fusion cat.3 -> cat.2 (artistes ET styles) puis 2 catégories seulement
    for key in ("artist_categories", "taste_categories"):
        cc = data.setdefault(key, {})
        if cc.get("3"):
            seen2 = {normalize_label(x) for x in cc.get("2", [])}
            cc["2"] = list(cc.get("2", [])) + [n for n in cc["3"]
                                               if normalize_label(n) not in seen2]
        cc.pop("3", None)
        for k in ("1", "2"):
            cc.setdefault(k, [])
    for key, env in _ENV_SECRETS:
        if not data.get(key) and os.environ.get(env):
            data[key] = os.environ[env]
    return data


def save_config(cfg, uid=None):
    save(paths.user_paths(uid or current_uid()).config, cfg)
