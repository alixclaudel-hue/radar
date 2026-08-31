"""
crate_radar.py — Crate Radar, version locale (Streamlit)

Pourquoi une version locale : l'API Discogs ne peut pas être appelée depuis un artifact
Claude (bac à sable du navigateur qui bloque les requêtes vers des domaines tiers). Une appli
Python locale n'a pas cette restriction : elle appelle l'API directement, comme n'importe
quel script.

------------------------------------------------------------------
INSTALLATION (une seule fois)
------------------------------------------------------------------
pip3 install -r requirements_streamlit.txt

------------------------------------------------------------------
LANCEMENT
------------------------------------------------------------------
streamlit run crate_radar.py

Ça ouvre automatiquement l'appli dans ton navigateur (http://localhost:8501).
Ferme le Terminal (ou Ctrl+C) pour l'arrêter.

------------------------------------------------------------------
DONNÉES
------------------------------------------------------------------
Ton token, ta base de labels, ta liste de veille et tes vendeurs sont sauvegardés dans un
fichier local `crate_radar_config.json` à côté de ce script — rien n'est envoyé ailleurs qu'à
l'API Discogs elle-même.
"""

import difflib
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

import requests
import streamlit as st

_HERE = os.path.dirname(os.path.abspath(__file__))
# Répertoire des données (config, base de labels, corpus, graphe, jobs…). En local :
# à côté du script. En conteneur : un volume persistant via CRATE_DATA_DIR.
_DATA = os.environ.get("CRATE_DATA_DIR") or _HERE
os.makedirs(os.path.join(_DATA, "jobs"), exist_ok=True)

CONFIG_PATH = os.path.join(_DATA, "crate_radar_config.json")
RESOLVED_PATH = os.path.join(_DATA, "labels_resolved.json")
PROFILE_PATH = os.path.join(_DATA, "labels_profile.json")
HISTORY_PATH = os.path.join(_DATA, "search_history.json")
HISTORY_MAX = 40
COLLECTION_CACHE_PATH = os.path.join(_DATA, "collection_cache.json")
CORPUS_PATH = os.path.join(_DATA, "taste_corpus.json")
LOOKUP_CACHE_PATH = os.path.join(_DATA, "lookup_cache.json")
PRODUCER_GRAPH_PATH = os.path.join(_DATA, "producer_graph.json")
ARTISTS_RESOLVED_PATH = os.path.join(_DATA, "artists_resolved.json")
SEARCH_INPUT_PATH = os.path.join(_DATA, "jobs", "search_base.input.json")
SEARCH_RESULTS_PATH = os.path.join(_DATA, "search_results.json")
FEEDBACK_PATH = os.path.join(_DATA, "reco_feedback.json")
SELLERS_SEEN_PATH = os.path.join(_DATA, "sellers_seen.json")
SELLERS_NEW_PATH = os.path.join(_DATA, "seller_new.json")
SCORING_PROFILES_PATH = os.path.join(_DATA, "scoring_profiles.json")
PENDING_ENRICH_PATH = os.path.join(_DATA, "pending_enrich.json")
JOBS_DIR = os.path.join(_DATA, "jobs")
JOBS_SCRIPT = os.path.join(_HERE, "crate_jobs.py")

DEFAULT_RECO_WEIGHTS = {"collection": 0.6, "affinity": 0.4, "want_factor": 0.6,
                        "corpus": 0.5, "artist": 0.4}

# Poids d'une occurrence (artiste/track) selon sa source dans le corpus de goût.
SOURCE_WEIGHTS = {"discogs_collection": 1.0, "discogs_want": 0.6, "youtube": 0.5, "bandcamp": 0.9, "djset": 0.4}
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
DISCOGS_UA = "CrateRadar/1.0 +personal-use"
CURRENT_YEAR = datetime.now().year

# Profilage hiérarchisé : 3 catégories de styles, chacune avec un poids.
DEFAULT_TASTE_CATEGORIES = {
    "1": ["House", "Deep House", "Tech House", "Progressive House", "Deep Techno",
          "Hip-House", "Synth-pop"],
    "2": ["Downtempo", "Electro House", "Funk", "Future Jazz", "Acid House"],
    "3": ["Techno", "Electro", "Minimal", "Breaks", "Prog Rock"],
}
DEFAULT_TASTE_WEIGHTS = {"1": 1.0, "2": 0.6, "3": 0.3}
DEFAULT_ALBUM_WEIGHTS = {"label": 0.4, "artist": 0.4, "style": 0.2}
CAT_LABELS = {"1": "Cœur", "2": "2ᵉ rang", "3": "Périphérie"}

# Artistes : mêmes 3 catégories/poids, alimentées manuellement (par défaut vides).
DEFAULT_ARTIST_CATEGORIES = {"1": [], "2": [], "3": []}
DEFAULT_ARTIST_WEIGHTS = {"1": 1.0, "2": 0.6, "3": 0.3}
DEFAULT_ARTIST_SCORE_WEIGHTS = {"manual": 0.5, "corpus": 0.18, "collection": 0.1,
                                "graph": 0.14, "djset": 0.08}
ARTIST_STOPWORDS = {"various artists", "various", "va", "unknown artist", "unknown",
                    "release", "progressive classics", "no artist", "traxsource"}

# ---------------------------------------------------------------------------
# Tous les paramètres de notation / classement, regroupés (board « 🎛️ Réglages »).
# Un seul point de vérité, lu partout via scoring(). crate_jobs.py en a une copie
# (clé "graph" seulement, pour la construction du graphe).
# ---------------------------------------------------------------------------
DEFAULT_SCORING = {
    "taste_tiers":  {"1": 1.0, "2": 0.6, "3": 0.3},          # rangs de styles
    "artist_tiers": {"1": 1.0, "2": 0.6, "3": 0.3},          # rangs d'artistes
    "reco": {"collection": 0.6, "corpus": 0.5, "artist": 0.4,
             "affinity": 0.4, "want_factor": 0.6},           # score label (reco)
    "album": {"label": 0.4, "artist": 0.4, "style": 0.2,
              "artist_max_vs_mean": 0.6},                     # score album
    "artist_score": {"manual": 0.5, "corpus": 0.18, "collection": 0.1,
                     "graph": 0.14, "djset": 0.08},          # score artiste
    "graph": {"tier_w": {"1": 3.5, "2": 1.5, "3": 0.6, "none": 0.3},
              "artist_breadth": 0.4, "label_breadth": 0.5, "cat1_bonus": 2.0,
              "role_main": 1.0, "role_remix": 0.7, "role_other": 0.4,
              "max_credits": 6},                              # graphe producteurs
    "sources": {"discogs_collection": 1.0, "discogs_want": 0.6, "youtube": 0.5,
                "bandcamp": 0.9, "djset": 0.4},              # poids d'occurrence corpus
    "label_affinity_floor": 0,                                # seuil global
    "learn": {"l2": 2.0, "min_feedback": 12, "min_per_class": 3},
}


def _deep_merge(base, over):
    """Copie profonde de `base` avec les surcharges de `over` (récursif sur les dicts)."""
    over = over or {}
    out = {}
    for k, v in base.items():
        if isinstance(v, dict):
            ov = over.get(k)
            out[k] = _deep_merge(v, ov if isinstance(ov, dict) else {})
        else:
            out[k] = over.get(k, v)
    for k, v in over.items():
        if k not in base:
            out[k] = _deep_merge(v, {}) if isinstance(v, dict) else v
    return out


def scoring():
    """Paramètres de notation effectifs = défauts + surcharges config (mémoïsé/rerun)."""
    cur = st.session_state.cfg.get("scoring", {})
    cache = st.session_state.get("_scoring_merged")
    if not cache or cache[0] is not cur:
        cache = (cur, _deep_merge(DEFAULT_SCORING, cur))
        st.session_state["_scoring_merged"] = cache
    return cache[1]


def _sig_scoring():
    return json.dumps(st.session_state.cfg.get("scoring", {}), sort_keys=True)

GENRES = ["", "Electronic", "Hip Hop", "Funk / Soul", "Rock", "Jazz", "Latin", "Reggae",
          "Pop", "Folk, World, & Country", "Classical", "Non-Music"]
STYLES = ["", "House", "Techno", "Deep House", "Tech House", "Minimal", "Electro", "Disco",
          "Dub", "Drum n Bass", "Breakbeat", "Ambient", "Downtempo", "Acid", "Trance",
          "Boogie", "Soul", "Funk"]


# ---------------------------------------------------------------- config persistence

_FLAT_WEIGHT_KEYS = ("taste_weights", "artist_weights", "reco_weights",
                     "album_weights", "artist_score_weights", "label_affinity_floor")


def load_config():
    default = {"token": "", "watchlist": [], "sellers": [], "labels": [],
               "taste_categories": {k: list(v) for k, v in DEFAULT_TASTE_CATEGORIES.items()},
               "youtube_api_key": "", "youtube_playlists": "",
               "bandcamp_sub_user": "", "bandcamp_sub_pass": "", "djset_sources": "",
               "artist_categories": {k: list(v) for k, v in DEFAULT_ARTIST_CATEGORIES.items()},
               "scoring": _deep_merge(DEFAULT_SCORING, {})}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if "taste_categories" not in data and data.get("taste_styles"):
            data["taste_categories"] = {"1": list(data["taste_styles"]), "2": [], "3": []}
        # migration : anciennes clés de poids éparses -> objet unique "scoring"
        if "scoring" not in data:
            sc = _deep_merge(DEFAULT_SCORING, {})
            if data.get("taste_weights"):
                sc["taste_tiers"] = {**sc["taste_tiers"], **data["taste_weights"]}
            if data.get("artist_weights"):
                sc["artist_tiers"] = {**sc["artist_tiers"], **data["artist_weights"]}
            if data.get("reco_weights"):
                sc["reco"] = {**sc["reco"], **data["reco_weights"]}
            if data.get("album_weights"):
                sc["album"] = {**sc["album"], **data["album_weights"]}
            if data.get("artist_score_weights"):
                sc["artist_score"] = {**sc["artist_score"], **data["artist_score_weights"]}
            if data.get("label_affinity_floor"):
                sc["label_affinity_floor"] = data["label_affinity_floor"]
            data["scoring"] = sc
        data["scoring"] = _deep_merge(DEFAULT_SCORING, data.get("scoring", {}))
        for k in _FLAT_WEIGHT_KEYS:
            data.pop(k, None)
        for k, v in default.items():
            data.setdefault(k, v)
        for k in ("1", "2", "3"):
            data["taste_categories"].setdefault(k, [])
            data["artist_categories"].setdefault(k, [])
        _env_secrets(data)
        return data
    _env_secrets(default)
    return default


def _env_secrets(data):
    """Secrets fournis par l'environnement (déploiement) quand ils ne sont pas déjà
    dans la config. Priorité à la valeur saisie dans l'UI si elle existe."""
    for key, env in (("token", "DISCOGS_TOKEN"), ("youtube_api_key", "YOUTUBE_API_KEY"),
                     ("bandcamp_sub_user", "BANDCAMP_SUB_USER"),
                     ("bandcamp_sub_pass", "BANDCAMP_SUB_PASS")):
        if not data.get(key) and os.environ.get(env):
            data[key] = os.environ[env]


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def cfg():
    return st.session_state.cfg


def persist():
    save_config(st.session_state.cfg)
    # on vient d'écrire la config nous-mêmes : évite que sync_job_outputs() la
    # recharge/reparse (240 Ko) au rerun suivant juste à cause de notre écriture.
    try:
        st.session_state.setdefault("_file_mtimes", {})[CONFIG_PATH] = os.path.getmtime(CONFIG_PATH)
    except OSError:
        pass


def _memo(key, sig, fn):
    """Cache d'une valeur dérivée coûteuse, réutilisée entre reruns tant que `sig`
    (peu coûteux à calculer) est identique."""
    store = st.session_state.setdefault("_memo", {})
    ent = store.get(key)
    if ent is not None and ent[0] == sig:
        return ent[1]
    val = fn()
    store[key] = (sig, val)
    return val


def _sig_artist_signal():
    coll = st.session_state.get("collection", {})
    return (len(st.session_state.get("corpus", [])),
            len(coll.get("artist_counts", {})),
            len(st.session_state.get("artists_resolved", {})),
            len(st.session_state.get("producer_graph", {}).get("artists", {})))


def _sig_cats():
    c = st.session_state.cfg
    return tuple(tuple(c.get("artist_categories", {}).get(k, [])) for k in ("1", "2", "3"))


def _sig_graph():
    g = st.session_state.get("producer_graph", {})
    return (g.get("built_at", ""), len(g.get("edges", g.get("artists", {}))), _sig_cats(),
            _sig_scoring(),
            tuple(sorted(st.session_state.cfg.get("watchlist", []))),
            len(st.session_state.cfg.get("labels", [])))


def _sig_artist_scores():
    c = st.session_state.cfg
    return (_sig_artist_signal(), _sig_graph(), _sig_cats(), _sig_scoring(),
            c.get("djset_sources", ""))


def _sig_reco():
    c = st.session_state.cfg
    coll = st.session_state.get("collection", {})
    return (_sig_artist_scores(), len(st.session_state.get("profile", {})),
            len(c.get("labels", [])), len(coll.get("label_counts", {})),
            len(st.session_state.get("producer_graph", {}).get("labels", {})),
            tuple(sorted(c.get("watchlist", []))), _sig_scoring(),
            tuple(tuple(c.get("taste_categories", {}).get(k, [])) for k in ("1", "2", "3")))


def enqueue_enrich(kind, name):
    """File d'attente d'enrichissement auto (résolution Discogs + profilage).
    `kind` = 'labels' | 'artists'. Drainée en tâche de fond par le job `enrich`."""
    name = (name or "").strip()
    if not name:
        return
    q = load_json(PENDING_ENRICH_PATH, {})
    lst = q.setdefault(kind, [])
    if normalize_label(name) not in {normalize_label(x) for x in lst}:
        lst.append(name)
        save_json(PENDING_ENRICH_PATH, q)


def add_watch(name):
    """Ajoute un label à la veille sans doublon (comparaison normalisée)."""
    name = (name or "").strip()
    if not name:
        return False
    keys = {normalize_label(w) for w in cfg().get("watchlist", [])}
    if normalize_label(name) in keys:
        return False
    cfg()["watchlist"].append(name)
    persist()
    enqueue_enrich("labels", name)
    return True


def add_label_to_base(name):
    """Ajoute un label à la base (dédup). La résolution Discogs canonique + le
    profilage sont faits ensuite en tâche de fond (job `enrich`)."""
    name = (name or "").strip()
    if not name:
        return False
    k = normalize_label(name)
    if k in {normalize_label(l) for l in st.session_state.labels}:
        return False
    set_labels(list(st.session_state.labels) + [name])
    res = st.session_state.resolved
    if res.get(k, {}).get("status") not in ("exact", "confirmed"):
        res[k] = {"original": name, "discogs_name": name, "discogs_id": None,
                  "status": "pending", "reviewed_by": "auto"}
        save_resolved(res)
    enqueue_enrich("labels", name)
    return True


RECO_FEAT_KEYS = ("collection", "corpus", "artist", "affinity")   # kind="label"
ALBUM_FEAT_KEYS = ("label", "artist", "style")                    # kind="album"
# (titre, sous-clé de scoring(), features)
FEEDBACK_KINDS = {"label": ("Poids reco (labels)", "reco", RECO_FEAT_KEYS),
                  "album": ("Poids score album (recherche + sets)", "album", ALBUM_FEAT_KEYS)}


def log_feedback(kind, key, name, verdict, score_shown, feat):
    """Journalise un retour utilisateur sur une reco (étape 7).
    `feat` = sous-signaux normalisés 0–1 au moment du clic. Ignore un clic
    identique au dernier retour déjà enregistré pour ce (kind, key)."""
    fb = st.session_state.feedback
    for e in reversed(fb):
        if e.get("kind") == kind and e.get("key") == key:
            if e.get("verdict") == verdict:
                return
            break
    keys = FEEDBACK_KINDS.get(kind, (None, None, tuple(feat)))[2]
    fb.append({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind, "key": key, "name": name, "verdict": verdict,
        "score_shown": score_shown,
        "feat": {k: round(float((feat or {}).get(k) or 0.0), 4) for k in keys},
    })
    save_json(FEEDBACK_PATH, fb)


def _sigmoid(z):
    import math
    if z < -60:
        return 0.0
    if z > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def fit_logreg(X, y, prior, l2=2.0, iters=600, lr=0.4):
    """Régression logistique maison, régularisée vers `prior` (MAP, pas de remplacement
    brutal). X = list[dict feat→val], y = list[0/1], prior = {feat: poids courant}.
    Retourne {feat: poids} (négatifs ramenés à 0, somme renormalisée sur celle du prior)."""
    keys = list(prior.keys())
    w = [float(prior[k]) for k in keys]
    b = 0.0
    n = max(len(y), 1)
    for _ in range(iters):
        gw = [0.0] * len(keys)
        gb = 0.0
        for xi, yi in zip(X, y):
            p = _sigmoid(b + sum(w[j] * xi.get(keys[j], 0.0) for j in range(len(keys))))
            err = p - yi
            for j in range(len(keys)):
                gw[j] += err * xi.get(keys[j], 0.0)
            gb += err
        for j in range(len(keys)):
            w[j] -= lr * (gw[j] / n + l2 * (w[j] - prior[keys[j]]) / n)
        b -= lr * (gb / n)
    w = [max(0.0, v) for v in w]
    s_prior, s_now = sum(prior.values()), sum(w) or 1.0
    return {keys[j]: round(w[j] * s_prior / s_now, 3) for j in range(len(keys))}


# ---------------------------------------------------------------- jobs en tâche de fond (crate_jobs.py)

def job_status(name):
    p = os.path.join(JOBS_DIR, f"{name}.status.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def job_running(name):
    s = job_status(name)
    return bool(s and s.get("running"))


def job_launch(name, params=None):
    """Lance le worker en sous-processus. Refuse si un run récent est déjà actif."""
    os.makedirs(JOBS_DIR, exist_ok=True)
    sp = os.path.join(JOBS_DIR, f"{name}.status.json")
    s = job_status(name)
    if s and s.get("running"):
        try:
            fresh = time.time() - os.path.getmtime(sp) < 150
        except OSError:
            fresh = False
        if fresh:
            st.warning(f"« {name} » tourne déjà — laisse-le finir ou clique ⏹ Arrêter.")
            return
    for suffix in (".stop",):
        try:
            os.remove(os.path.join(JOBS_DIR, f"{name}{suffix}"))
        except OSError:
            pass
    with open(sp, "w", encoding="utf-8") as f:
        json.dump({"job": name, "running": True, "done": 0, "total": 0,
                   "last": "", "message": "démarrage…", "error": None,
                   "started_at": datetime.now().isoformat(timespec="seconds"),
                   "finished_at": None}, f)
    subprocess.Popen([sys.executable, JOBS_SCRIPT, name, json.dumps(params or {})],
                     cwd=_HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def job_stop(name):
    open(os.path.join(JOBS_DIR, f"{name}.stop"), "w").close()


def render_job(name, title, poll=3):
    """Affiche la progression d'un job. Retourne True si le job tourne."""
    s = job_status(name)
    if not s:
        return False
    if s.get("running"):
        status_path = os.path.join(JOBS_DIR, f"{name}.status.json")
        try:
            stale = time.time() - os.path.getmtime(status_path) > 150
        except OSError:
            stale = False
        done, total = s.get("done", 0), s.get("total", 0)
        frac = done / total if total else 0.0
        st.progress(min(frac, 1.0),
                    text=f"⏳ {title} : {done}/{total or '?'} — "
                         f"{s.get('last') or s.get('message') or '…'}")
        c1, c2 = st.columns([3, 1])
        c1.caption("La page se rafraîchit automatiquement toutes les "
                   f"{poll} s tant que le job tourne.")
        if c2.button("⏹ Arrêter", key=f"stopjob_{name}"):
            job_stop(name)
            st.rerun()
        if stale:
            st.warning("Aucune mise à jour depuis >2 min — le worker s'est peut-être arrêté.")
            if st.button("Forcer la fin", key=f"forcejob_{name}"):
                s["running"] = False
                s["error"] = "arrêt forcé (worker sans réponse)"
                save_json(status_path, s)
                st.rerun()
            return False
        time.sleep(poll)
        st.rerun()
        return True
    if s.get("error"):
        st.error(f"{title} — échec : {s['error']}")
    elif s.get("finished_at"):
        st.success(f"{title} — terminé. {s.get('message', '')}")
    return False


def set_labels(names):
    """Remplace la base de labels (mémoire + disque) en dédupliquant sur le nom
    normalisé — garde le premier libellé rencontré pour chaque nom."""
    seen, unique = set(), []
    for n in names:
        n = (n or "").strip()
        key = normalize_label(n)
        if key and key not in seen:
            seen.add(key)
            unique.append(n)
    st.session_state.labels = unique
    cfg()["labels"] = unique
    persist()
    return unique


# ---------------------------------------------------------------- Discogs API helpers

def discogs_get(path, params=None, token=None):
    token = token or cfg().get("token", "")
    params = dict(params or {})
    params["token"] = token
    resp = requests.get(f"https://api.discogs.com{path}", params=params,
                         headers={"User-Agent": DISCOGS_UA}, timeout=20)
    if resp.status_code == 401:
        raise RuntimeError("Token invalide ou manquant (401).")
    if resp.status_code == 429:
        raise RuntimeError("Limite de requêtes Discogs atteinte (429) — attends une minute.")
    if not resp.ok:
        raise RuntimeError(f"Erreur Discogs ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


def discogs_search(**params):
    params["type"] = "release"
    return discogs_get("/database/search", params)


def search_label_releases(label_name, genre="", style="", fmt="", year="", max_pages=3):
    """Toutes les sorties d'un label pour ces filtres (year peut être "2005-2014"),
    en paginant jusqu'à max_pages — pause d'1,1 s entre chaque page."""
    out, page = [], 1
    while page <= max_pages:
        data = discogs_search(label=label_name, genre=genre, style=style, format=fmt,
                              year=year, per_page=100, page=page,
                              sort="year", sort_order="desc")
        out.extend(data.get("results", []))
        if page >= data.get("pagination", {}).get("pages", 1):
            break
        page += 1
        time.sleep(1.1)
    return out


def finalize_base_search():
    """Dédoublonne l'accumulateur par id, filtre par intervalle d'années (côté client)
    et trie du plus récent au plus ancien -> st.session_state.results."""
    p = st.session_state.search_params
    yf = int(p["year_from"]) if str(p.get("year_from", "")).strip().isdigit() else None
    yt = int(p["year_to"]) if str(p.get("year_to", "")).strip().isdigit() else None
    seen, out = set(), []
    for r in st.session_state.search_acc:
        rid = r.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        # L'API a déjà filtré par year=range ; on ne retire ici que ce qu'on
        # sait positivement hors intervalle (année connue et hors bornes).
        y = int(r.get("year") or 0)
        if y and yf and y < yf:
            continue
        if y and yt and y > yt:
            continue
        out.append(r)
    out.sort(key=lambda r: int(r.get("year") or 0), reverse=True)
    st.session_state.results = out
    if p.get("_hist"):
        add_history({**p["_hist"], "n_results": len(out)})


def year_param(y_from, y_to):
    """Construit la valeur du paramètre Discogs `year` : "" (aucun filtre), "2008"
    (une année) ou "2005-2014" (intervalle — géré nativement par l'API)."""
    y_from = (y_from or "").strip()
    y_to = (y_to or "").strip()
    if not y_from and not y_to:
        return ""
    lo = int(y_from) if y_from else int(y_to)
    hi = int(y_to) if y_to else int(y_from)
    lo, hi = min(lo, hi), max(lo, hi)
    return str(lo) if lo == hi else f"{lo}-{hi}"


def normalize_label(name):
    """Normalise un nom de label pour comparer malgré les petites différences
    (casse, espaces, suffixe de désambiguïsation Discogs comme "(2)")."""
    import re
    n = (name or "").strip().lower()
    n = re.sub(r'\s*\(\d+\)\s*$', '', n)  # retire un suffixe " (2)" en fin de nom
    n = re.sub(r'\s+', ' ', n)
    return n.strip()


def name_similarity(a, b):
    """Score 0–1 entre deux noms de labels (après normalisation). Bonus si l'un
    contient l'autre (ex. « Clone » ↔ « Clone Records »), sauf noms très courts."""
    a, b = normalize_label(a), normalize_label(b)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if len(min(a, b, key=len)) >= 4 and (a in b or b in a):
        return max(0.9, ratio)
    return ratio


# ---------------------------------------------------------------- résolution des noms vers Discogs

def load_resolved():
    if os.path.exists(RESOLVED_PATH):
        with open(RESOLVED_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_resolved(d):
    with open(RESOLVED_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def label_candidates(name, token=None, per_page=8):
    """Liste des labels Discogs proposés pour un nom donné (nom + id)."""
    data = discogs_get("/database/search",
                       {"type": "label", "q": name, "per_page": per_page}, token=token)
    return [{"name": c.get("title"), "id": c.get("id"), "thumb": c.get("thumb")}
            for c in data.get("results", []) if c.get("title")]


def resolve_one_label(name, token=None):
    """Cherche le nom canonique Discogs correspondant à un nom de label de ta base.
    Retourne (discogs_name, discogs_id, status, candidates) où status vaut
    'exact', 'approx' ou 'not_found'."""
    cands = label_candidates(name, token=token, per_page=5)
    if not cands:
        return None, None, "not_found", []
    target = normalize_label(name)
    for c in cands:
        if normalize_label(c.get("name", "")) == target:
            return c.get("name"), c.get("id"), "exact", cands
    top = cands[0]
    return top.get("name"), top.get("id"), "approx", cands


def get_canonical(name):
    """Renvoie le nom Discogs résolu si disponible, sinon le nom d'origine."""
    resolved = st.session_state.get("resolved", {})
    entry = resolved.get(normalize_label(name))
    if entry and entry.get("status") in ("exact", "approx", "confirmed") and entry.get("discogs_name"):
        return entry["discogs_name"]
    return name


# ---------------------------------------------------------------- profilage des labels (genres/styles)

def load_profile():
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_profile(d):
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- historique des recherches

def load_history():
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(lst):
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(lst, f, ensure_ascii=False, indent=2)


def add_history(entry):
    """Ajoute une recherche en tête, dédoublonne les paramètres identiques, plafonne."""
    entry["at"] = datetime.now().isoformat(timespec="seconds")
    sig = {k: entry.get(k) for k in ("picked_label", "genre", "style", "fmt",
                                     "year_from", "year_to", "label_filter", "aff_min")}
    hist = [h for h in st.session_state.history
            if {k: h.get(k) for k in sig} != sig]
    hist.insert(0, entry)
    st.session_state.history = hist[:HISTORY_MAX]
    save_history(st.session_state.history)


def history_label(h):
    bits = [h.get("picked_label") or "· scan base ·"]
    if h.get("genre"):
        bits.append(h["genre"])
    if h.get("style"):
        bits.append(h["style"])
    yr = year_param(h.get("year_from", ""), h.get("year_to", ""))
    if yr:
        bits.append(yr)
    if h.get("fmt"):
        bits.append(h["fmt"])
    if not h.get("picked_label") and h.get("aff_min"):
        bits.append(f"aff≥{h['aff_min']}")
    tail = f" — {h.get('n_results', '?')} rés." if h.get("n_results") is not None else ""
    return " · ".join(bits) + tail


def style_key(s):
    """Normalise un style pour comparer malgré casse et tirets ("Deep-House" == "deep house")."""
    return re.sub(r"\s+", " ", (s or "").lower().replace("-", " ")).strip()


# ---------------------------------------------------------------- collection & wantlist Discogs

def load_collection_cache():
    if os.path.exists(COLLECTION_CACHE_PATH):
        with open(COLLECTION_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_collection_cache(d):
    with open(COLLECTION_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _aggregate_items(items, label_counts, label_ids, artist_counts):
    for it in items:
        bi = it.get("basic_information", {})
        for lb in bi.get("labels", []):
            nm = (lb.get("name") or "").strip()
            if not nm:
                continue
            k = normalize_label(nm)
            label_counts[k] = label_counts.get(k, 0) + 1
            if lb.get("id") and k not in label_ids:
                label_ids[k] = {"name": nm, "id": lb.get("id")}
        for ar in bi.get("artists", []):
            nm = (ar.get("name") or "").strip()
            if nm and nm.lower() != "various":
                artist_counts[nm] = artist_counts.get(nm, 0) + 1


def fetch_collection_cache(progress=None, max_pages=200):
    """Agrège collection + wantlist en ~ceil(N/100) appels. Aucune résolution :
    les IDs de labels viennent directement de Discogs."""
    username = discogs_get("/oauth/identity").get("username")
    if not username:
        raise RuntimeError("Impossible de lire l'identité Discogs (token ?).")
    label_counts, want_label_counts, label_ids, artist_counts = {}, {}, {}, {}
    n_coll = n_want = 0

    for kind, path, res_key, target in (
        ("collection", f"/users/{username}/collection/folders/0/releases", "releases", label_counts),
        ("wantlist", f"/users/{username}/wants", "wants", want_label_counts),
    ):
        page, pages = 1, 1
        while page <= pages and page <= max_pages:
            d = discogs_get(path, {"page": page, "per_page": 100})
            items = d.get(res_key, [])
            _aggregate_items(items, target, label_ids, artist_counts)
            if kind == "collection":
                n_coll += len(items)
            else:
                n_want += len(items)
            pages = d.get("pagination", {}).get("pages", 1)
            if progress:
                progress(f"{kind} — page {page}/{pages}", (page, pages, kind))
            page += 1
            if page <= pages:
                time.sleep(1.1)

    return {
        "username": username, "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "n_collection": n_coll, "n_wants": n_want,
        "label_counts": label_counts, "want_label_counts": want_label_counts,
        "label_ids": label_ids, "artist_counts": artist_counts,
    }


def merge_collection_into_base(cache, add_to_base=True):
    """Ajoute les labels de la collection à la base et sème `labels_resolved.json`
    avec leurs IDs Discogs (résolution gratuite). Retourne le nb de labels ajoutés."""
    label_ids = cache.get("label_ids", {})
    added = 0
    if add_to_base:
        existing = {normalize_label(l) for l in st.session_state.labels}
        new_names = [info["name"] for k, info in label_ids.items() if k not in existing]
        if new_names:
            set_labels(list(st.session_state.labels) + new_names)
            added = len(new_names)
    res = st.session_state.resolved
    changed = False
    for k, info in label_ids.items():
        cur = res.get(k)
        if cur and cur.get("status") in ("exact", "confirmed"):
            continue
        res[k] = {"original": info["name"], "discogs_name": info["name"],
                  "discogs_id": info["id"], "status": "confirmed", "reviewed_by": "collection"}
        changed = True
    if changed:
        save_resolved(res)
    return added


# ---------------------------------------------------------------- socle commun : corpus de goût

def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def lookup_key(artist, title):
    return f"{style_key(artist)}||{style_key(title)}"


def discogs_lookup(artist, title, cache, kind="track", deep=True):
    """(artiste, titre) -> (hit, nb_appels). hit = sortie Discogs la plus probable
    {label, release_id, year, style} ou None. `kind="release"` cherche un album,
    sinon un morceau. `deep` ajoute une 2ᵉ passe en texte libre si la 1ʳᵉ échoue.
    Mis en cache dans `lookup_cache.json`."""
    k = lookup_key(artist, title)
    if k in cache:
        return cache[k], 0
    artist, title = (artist or "").strip(), (title or "").strip()
    field = "release_title" if kind == "release" else "track"
    if artist and title:
        attempts = [{"artist": artist, field: title}]
        if deep:
            attempts.append({"q": f"{artist} {title}"})
    else:
        attempts = [{"q": f"{artist} {title}".strip()}]
    hit, calls = None, 0
    for i, params in enumerate(attempts):
        if i:
            time.sleep(0.3)
        calls += 1
        results = discogs_search(per_page=5, **params).get("results", [])
        if results:
            r = results[0]
            labels = r.get("label") or []
            hit = {"label": labels[0] if labels else None, "release_id": r.get("id"),
                   "year": r.get("year"), "style": r.get("style") or []}
            break
    cache[k] = hit
    return cache[k], calls


def corpus_add_rows(new_rows, source):
    """Ajoute des lignes {artist,title,label,release_id} au corpus, dédoublonnées."""
    corpus = st.session_state.corpus
    seen = {(r.get("source"), style_key(r.get("artist", "")), style_key(r.get("title", "")))
            for r in corpus}
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    for r in new_rows:
        sig = (source, style_key(r.get("artist", "")), style_key(r.get("title", "")))
        if sig in seen or not (r.get("artist") or r.get("title")):
            continue
        seen.add(sig)
        corpus.append({**r, "source": source, "added_at": now})
        added += 1
    save_json(CORPUS_PATH, corpus)
    return added


def corpus_label_scores():
    """{label normalisé: score pondéré} agrégé sur tout le corpus (YouTube, Bandcamp…)."""
    agg = {}
    for r in st.session_state.get("corpus", []):
        lab = r.get("label")
        if not lab:
            continue
        agg[normalize_label(lab)] = agg.get(normalize_label(lab), 0.0) \
            + scoring()["sources"].get(r.get("source"), 0.4)
    return agg


# ---------------------------------------------------------------- artistes : score & liste manuelle

def canonical_artist_key(name):
    """Clé d'agrégation des artistes : 'id:<discogs_id>' si l'artiste est résolu
    (artists_resolved.json), sinon le nom normalisé. Fusionne les alias
    (« Buck » / « DJ Buck », « Fred everything » / « Fred Everything »…)."""
    e = st.session_state.get("artists_resolved", {}).get(normalize_label(name))
    if e and e.get("discogs_id") and e.get("status") in ("exact", "approx", "confirmed"):
        return f"id:{e['discogs_id']}"
    return normalize_label(name)


def canonical_artist_name(name):
    e = st.session_state.get("artists_resolved", {}).get(normalize_label(name))
    if e and e.get("discogs_name") and e.get("status") in ("exact", "confirmed"):
        return e["discogs_name"]
    return name


def _graph_rescore_impl():
    """Recalcule les scores du graphe à partir des arêtes brutes + catégories courantes.
    Retourne {'artists': {clé: {...}}, 'labels': {clé: {...}}}."""
    g = st.session_state.get("producer_graph", {})
    gp = scoring()["graph"]
    _tw = gp["tier_w"]
    TIER_W = {"1": _tw["1"], "2": _tw["2"], "3": _tw["3"], None: _tw["none"]}
    _abr, _lbr, _c1b = gp["artist_breadth"], gp["label_breadth"], gp["cat1_bonus"]
    tier_map = artist_tier_map()          # {clé canonique: '1'|'2'|'3'}
    edges = g.get("edges")
    if not edges:                        # compat ancien format pré-arêtes
        out = {}
        for k, v in g.get("artists", {}).items():
            ck = canonical_artist_key(v.get("name", k))
            out[ck] = {"name": canonical_artist_name(v.get("name", k)), "id": v.get("id"),
                       "score": v.get("score", 0), "why": v.get("why", []),
                       "cat1_hits": v.get("cat1_hits", 0)}
        return {"artists": out, "labels": g.get("labels", {})}

    seed_names = g.get("seeds", {})
    artists = {}
    for ck, e in edges.items():
        if ck in tier_map:               # déjà dans mes catégories -> pas un candidat
            continue
        base, cat1, byseed = 0.0, 0, {}
        for sk, d in e.get("co", {}).items():
            t = tier_map.get(sk)
            base += d["n"] * d.get("rw", 1.0) * TIER_W.get(t, 0.3)
            if t == "1":
                cat1 += d["n"]
            byseed[seed_names.get(sk, sk)] = d["n"]
        breadth = len(e.get("co", {}))
        score = round(base * (1 + _abr * (breadth - 1)) + (_c1b if cat1 else 0), 2)
        why = [f"{n} sortie(s) avec {sn}"
               for sn, n in sorted(byseed.items(), key=lambda kv: -kv[1])[:3]]
        if cat1:
            why.insert(0, f"⭐ {cat1} sortie(s) avec un artiste catégorie 1")
        artists[ck] = {"name": e["name"], "id": e.get("id"), "score": score,
                       "why": why, "cat1_hits": cat1}
    watch = {normalize_label(w) for w in cfg().get("watchlist", [])}
    base_keys = {normalize_label(x) for x in cfg().get("labels", [])}
    labels = {}
    for lk, le in g.get("label_edges", {}).items():
        co = le.get("co", {})
        if not co:
            continue
        base = sum(n * TIER_W.get(tier_map.get(sk), 0.3) for sk, n in co.items())
        cat1_seeds = sum(1 for sk in co if tier_map.get(sk) == "1")
        n_seeds = len(co)
        labels[lk] = {"name": le["name"],
                      "score": round(base * (1 + _lbr * (n_seeds - 1)) + (_c1b if cat1_seeds else 0), 2),
                      "n_seeds": n_seeds, "cat1_seeds": cat1_seeds,
                      "seeds": [seed_names.get(sk, sk) for sk in list(co)[:6]],
                      "in_watchlist": lk in watch, "in_base": lk in base_keys}
    return {"artists": dict(sorted(artists.items(), key=lambda kv: -kv[1]["score"])),
            "labels": dict(sorted(labels.items(), key=lambda kv: -kv[1]["score"]))}


def graph_rescore():
    return _memo("graph_rescore", _sig_graph(), _graph_rescore_impl)


def artist_signal():
    return _memo("artist_signal", (_sig_artist_signal(), _sig_graph()), _artist_signal_impl)


def _artist_signal_impl():
    """(corpus_counts, collection_counts, graph_scores, display) — clés canoniques
    (fusion des alias via artists_resolved.json)."""
    corpus_c, coll_c, disp = {}, {}, {}
    for r in st.session_state.get("corpus", []):
        a = (r.get("artist") or "").strip()
        if not a or normalize_label(a) in ARTIST_STOPWORDS:
            continue
        k = canonical_artist_key(a)
        corpus_c[k] = corpus_c.get(k, 0) + 1
        disp.setdefault(k, canonical_artist_name(a))
    for a, n in st.session_state.get("collection", {}).get("artist_counts", {}).items():
        if not a or normalize_label(a) in ARTIST_STOPWORDS:
            continue
        k = canonical_artist_key(a)
        coll_c[k] = coll_c.get(k, 0) + n
        disp.setdefault(k, canonical_artist_name(a))
    graph = {}
    for ck, v in graph_rescore().get("artists", {}).items():
        graph[ck] = v["score"]
        disp.setdefault(ck, v.get("name", ck))
    # noms d'affichage des artistes de la liste manuelle (peuvent être absents du reste)
    for cid, names in cfg().get("artist_categories", DEFAULT_ARTIST_CATEGORIES).items():
        for a in names:
            disp.setdefault(canonical_artist_key(a), canonical_artist_name(a))
    return corpus_c, coll_c, graph, disp


def artist_tier_map():
    """{clé canonique: '1'|'2'|'3'} d'après la liste manuelle en config."""
    cats = cfg().get("artist_categories", DEFAULT_ARTIST_CATEGORIES)
    m = {}
    for cid, names in cats.items():
        for n in names:
            m[canonical_artist_key(n)] = cid
    return m


def djset_cosign():
    return _memo("djset_cosign", _sig_artist_scores(), _djset_cosign_impl)


def _djset_cosign_impl():
    """{clé artiste canonique: nb de tracks extraites d'un set joué par un DJ de
    ma base}. Un morceau compte comme « co-sign » quand le DJ qui l'a joué fait
    lui-même partie de ma base (liste manuelle / artiste résolu / source DJ
    déclarée) — cf. demande étape 6b."""
    trusted = set(artist_tier_map())
    for e in st.session_state.get("artists_resolved", {}).values():
        if e.get("discogs_id") and e.get("status") in ("exact", "approx", "confirmed"):
            trusted.add(f"id:{e['discogs_id']}")
    for line in (cfg().get("djset_sources", "") or "").splitlines():
        if line.strip():
            trusted.add(canonical_artist_key(line.strip()))
    out = {}
    for r in st.session_state.get("corpus", []):
        if r.get("source") != "djset":
            continue
        a = (r.get("artist") or "").strip()
        if not a or normalize_label(a) in ARTIST_STOPWORDS:
            continue
        if canonical_artist_key((r.get("dj") or "").strip()) not in trusted:
            continue
        k = canonical_artist_key(a)
        out[k] = out.get(k, 0) + 1
    return out


def artist_scores():
    return _memo("artist_scores", _sig_artist_scores(), _artist_scores_impl)


def _artist_scores_impl():
    """{artiste canonique: (score 0–100, display, why)} — liste manuelle + corpus +
    collection + proximité graphe producteurs."""
    corpus_c, coll_c, graph, disp = artist_signal()
    djc = djset_cosign()
    tiers = artist_tier_map()
    aw = scoring()["artist_tiers"]
    sw = scoring()["artist_score"]
    max_corp = max(corpus_c.values(), default=1)
    max_coll = max(coll_c.values(), default=1)
    max_graph = max(graph.values(), default=1) or 1
    max_dj = max(djc.values(), default=1) or 1
    out = {}
    for k in set(tiers) | set(corpus_c) | set(coll_c) | set(graph) | set(djc):
        tier = tiers.get(k)
        manual = float(aw.get(tier, 0)) if tier else 0.0
        corp = corpus_c.get(k, 0) / max_corp
        coll = coll_c.get(k, 0) / max_coll
        grph = graph.get(k, 0) / max_graph
        djs = djc.get(k, 0) / max_dj
        score = round(100 * (sw.get("manual", 0.5) * manual
                             + sw.get("corpus", 0.18) * corp
                             + sw.get("collection", 0.1) * coll
                             + sw.get("graph", 0.14) * grph
                             + sw.get("djset", 0.08) * djs))
        why = []
        if tier:
            why.append(f"liste cat.{tier}")
        if coll_c.get(k):
            why.append(f"{coll_c[k]} en collection")
        if corpus_c.get(k):
            why.append(f"corpus ×{corpus_c[k]}")
        if djc.get(k):
            why.append(f"joué en set ×{djc[k]}")
        if graph.get(k):
            why.append("proche graphe")
        out[k] = (score, disp.get(k, k), ", ".join(why) or "—")
    return out


def artist_score(name):
    return artist_scores().get(canonical_artist_key(name), (0, name, "—"))[0]


def set_artist_categories(cats):
    """Écrit les 3 catégories d'artistes (dédup sur nom normalisé) en config.
    Les nouveaux noms sont mis en file d'enrichissement (résolution Discogs auto)."""
    prev = {normalize_label(n) for cid in ("1", "2", "3")
            for n in cfg().get("artist_categories", {}).get(cid, [])}
    seen = set()
    clean = {}
    for cid in ("1", "2", "3"):
        clean[cid] = []
        for n in cats.get(cid, []):
            n = (n or "").strip()
            k = normalize_label(n)
            if k and k not in seen:
                seen.add(k)
                clean[cid].append(n)
                if k not in prev:
                    enqueue_enrich("artists", n)
    cfg()["artist_categories"] = clean
    persist()


# ---------------------------------------------------------------- source : playlists YouTube

def yt_playlist_id(url_or_id):
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url_or_id or "")
    return m.group(1) if m else (url_or_id or "").strip() or None


def youtube_playlist_items(playlist_id, api_key, max_pages=40):
    """Titres + descriptions des vidéos d'une playlist via l'API YouTube Data v3."""
    items, token = [], None
    for _ in range(max_pages):
        params = {"part": "snippet", "playlistId": playlist_id, "maxResults": 50,
                  "key": api_key}
        if token:
            params["pageToken"] = token
        resp = requests.get(f"{YOUTUBE_API}/playlistItems", params=params, timeout=20)
        if resp.status_code in (403, 400):
            msg = resp.json().get("error", {}).get("message", resp.text[:200])
            raise RuntimeError(f"YouTube API {resp.status_code} : {msg}")
        if not resp.ok:
            raise RuntimeError(f"YouTube API {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            items.append({"title": sn.get("title", ""),
                          "channel": sn.get("videoOwnerChannelTitle") or sn.get("channelTitle", ""),
                          "description": sn.get("description", "")})
        token = data.get("nextPageToken")
        if not token:
            break
    return items


_YT_NOISE = re.compile(
    r"\((official|lyric|lyrics|audio|visuali[sz]er|music|hq|hd|4k|full)\b[^)]*\)"
    r"|\bofficial (video|audio|music video)\b|\bfree (dl|download)\b|\[[^\]]*\]", re.I)


_YT_SPLIT_RE = re.compile(r"\s+[-–—―─‐‑－•·]\s+")


def parse_yt_title(title, channel):
    t = _YT_NOISE.sub("", title or "").strip(" -–—―─|·•")
    parts = _YT_SPLIT_RE.split(t, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    ch = re.sub(r"\s*-\s*topic\s*$", "", channel or "", flags=re.I).strip()
    if ch and ch.lower() not in ("various artists", "va"):
        return ch, t
    return "", t


_YT_LABEL_RES = [
    re.compile(r"under exclusive licen[sc]e to (.+?)(?:\.|;|\n|$)", re.I),
    re.compile(r"℗\s*\d{4}\s+(.+?)(?:\n|$)"),
]


def parse_yt_label(description):
    for rx in _YT_LABEL_RES:
        m = rx.search(description or "")
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip(" .")
    return None


# ---------------------------------------------------------------- source : collection Bandcamp (API Subsonic)

SUBSONIC_BASE = "https://bandcamp.com/api/subsonic/rest"


def subsonic_get(method, user, password, **params):
    """Appel Subsonic (Bandcamp) — auth par token : t = md5(motdepasse + sel)."""
    salt = os.urandom(8).hex()
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    p = {"u": user, "t": token, "s": salt,
         "v": "1.16.1", "c": "CrateRadar", "f": "json", **params}
    resp = requests.get(f"{SUBSONIC_BASE}/{method}", params=p, timeout=25)
    if not resp.ok:
        raise RuntimeError(f"Subsonic {method} — HTTP {resp.status_code}")
    body = resp.json().get("subsonic-response", {})
    if body.get("status") != "ok":
        raise RuntimeError(f"Subsonic {method} : "
                           f"{body.get('error', {}).get('message', body.get('status'))}")
    return body


def bandcamp_subsonic_albums(user, password, progress=None, max_pages=60):
    """Énumère la collection Bandcamp (albums achetés) via getAlbumList2."""
    out, offset, size = [], 0, 500
    for _ in range(max_pages):
        body = subsonic_get("getAlbumList2", user, password,
                            type="alphabeticalByName", size=size, offset=offset)
        albums = body.get("albumList2", {}).get("album", [])
        for a in albums:
            out.append({"artist": (a.get("artist") or "").strip(),
                        "title": (a.get("name") or "").strip(),
                        "label": None, "year": a.get("year"),
                        "genre": a.get("genre"), "url": None})
        if progress:
            progress(f"{len(out)} albums lus…", None)
        if len(albums) < size:
            break
        offset += size
        time.sleep(0.5)
    return out


def label_artist_signal():
    """{label normalisé: (somme des artist_score/100 des artistes aimés qui y sont
    sortis, nombre d'artistes)}. Lien label→artiste via le corpus + le graphe."""
    ascore = artist_scores()  # {clé canonique: (score, nom, why)}
    lab_arts = {}
    for r in st.session_state.get("corpus", []):
        if r.get("label") and r.get("artist"):
            lab_arts.setdefault(normalize_label(r["label"]), set()).add(
                canonical_artist_key(r["artist"]))
    for lk, lv in graph_rescore().get("labels", {}).items():
        s = lab_arts.setdefault(lk, set())
        for n in lv.get("seeds", []):
            s.add(canonical_artist_key(n))
    return {lk: (sum(ascore.get(a, (0,))[0] for a in arts) / 100.0, len(arts))
            for lk, arts in lab_arts.items()}


def reco_rows():
    return _memo("reco_rows", _sig_reco(), _reco_rows_impl)


def _reco_rows_impl():
    """Labels recommandés : collection + wantlist + corpus + affinité de style
    + présence d'artistes que j'aime (via corpus & graphe de producteurs)."""
    cache = st.session_state.get("collection", {})
    lc, wc = cache.get("label_counts", {}), cache.get("want_label_counts", {})
    cs = corpus_label_scores()
    las = label_artist_signal()
    if not lc and not wc and not cs and not las:
        return []
    w = scoring()["reco"]
    wf = float(w.get("want_factor", 0.6))
    w_coll, w_aff, w_corp, w_art = (float(w.get("collection", 0.6)), float(w.get("affinity", 0.4)),
                                    float(w.get("corpus", 0.5)), float(w.get("artist", 0.4)))
    coll_raw = {k: lc.get(k, 0) + wf * wc.get(k, 0) for k in set(lc) | set(wc)}
    max_coll = max(coll_raw.values()) if coll_raw else 1.0
    max_corp = max(cs.values()) if cs else 1.0
    max_art = max((v[0] for v in las.values()), default=1.0) or 1.0
    prof, wmap = st.session_state.profile, taste_weight_map()
    floor = float(scoring()["label_affinity_floor"] or 0)
    watch_keys = {normalize_label(l) for l in cfg().get("watchlist", [])}
    base_keys = {normalize_label(l) for l in cfg().get("labels", [])}
    rows = []
    for k in set(coll_raw) | set(cs) | set(las):
        info = cache.get("label_ids", {}).get(k, {})
        e = prof.get(k)
        aff = affinity_score(e, wmap) if e else None
        if floor and (aff is None or aff < floor):
            continue          # seuil d'affinité global (onglet 🎯 Profilage)
        art_val, art_n = las.get(k, (0.0, 0))
        feat = {
            "collection": coll_raw.get(k, 0) / max_coll if max_coll else 0,
            "corpus": cs.get(k, 0) / max_corp if max_corp else 0,
            "artist": art_val / max_art if max_art else 0,
            "affinity": (aff / 100) if aff is not None else 0,
        }
        score = round(100 * (w_coll * feat["collection"] + w_corp * feat["corpus"]
                             + w_art * feat["artist"] + w_aff * feat["affinity"]))
        rows.append({
            "key": k, "name": info.get("name") or k, "score": score,
            "owned": lc.get(k, 0), "want": wc.get(k, 0),
            "corpus": round(cs.get(k, 0), 1), "aff": aff, "artists": art_n,
            "watched": k in watch_keys, "in_base": k in base_keys, "feat": feat,
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def profile_one_label(canonical_name):
    """Échantillonne jusqu'à 100 sorties d'un label (les plus 'wanted') et agrège
    les compteurs de styles / genres — 1 appel API."""
    data = discogs_search(label=canonical_name, per_page=100, page=1,
                          sort="want", sort_order="desc")
    res = data.get("results", [])
    sc, gc = {}, {}
    for x in res:
        for s in (x.get("style") or []):
            sc[s] = sc.get(s, 0) + 1
        for g in (x.get("genre") or []):
            gc[g] = gc.get(g, 0) + 1
    return {"sampled": len(res), "style_counts": sc, "genre_counts": gc,
            "total_items": data.get("pagination", {}).get("items", len(res)),
            "profiled_at": datetime.now().isoformat(timespec="seconds")}


def taste_weight_map():
    """{style_normalisé: poids} d'après les 3 catégories et leurs poids en config."""
    cats = cfg().get("taste_categories", DEFAULT_TASTE_CATEGORIES)
    weights = scoring()["taste_tiers"]
    m = {}
    for cid, styles in cats.items():
        w = float(weights.get(cid, 0))
        for s in styles:
            m[style_key(s)] = w
    return m


def style_category_keys():
    """{cid: set(style_keys)} pour ventiler un échantillon par catégorie."""
    cats = cfg().get("taste_categories", DEFAULT_TASTE_CATEGORIES)
    return {cid: {style_key(s) for s in styles} for cid, styles in cats.items()}


def affinity_score(entry, wmap=None):
    """Score pondéré (0–100) : moyenne des poids de catégorie sur tous les tags de
    style de l'échantillon (un tag hors catégories compte 0)."""
    wmap = taste_weight_map() if wmap is None else wmap
    sc = (entry or {}).get("style_counts") or {}
    total = sum(sc.values())
    if not total:
        return 0
    weighted = sum(n * wmap.get(style_key(s), 0.0) for s, n in sc.items())
    return round(100 * weighted / total)


def category_shares(entry):
    """{cid: part en % des tags de style de l'échantillon dans cette catégorie}."""
    sc = (entry or {}).get("style_counts") or {}
    total = sum(sc.values())
    cat_keys = style_category_keys()
    if not total:
        return {cid: 0 for cid in cat_keys}
    return {cid: round(100 * sum(n for s, n in sc.items() if style_key(s) in keys) / total)
            for cid, keys in cat_keys.items()}


def release_label_affinity(r):
    """(nom du label retenu, score d'affinité 0–100 ou None) pour une sortie de recherche."""
    prof = st.session_state.get("profile", {})
    wmap = taste_weight_map()
    names = ([r["_base_label"]] if r.get("_base_label") else []) + list(r.get("label", []))
    for name in names:
        e = prof.get(normalize_label(name))
        if e:
            return name, affinity_score(e, wmap)
    return (names[0] if names else None), None


# ---------------------------------------------------------------- score album (étape 4d)

_CREDIT_SPLIT = re.compile(r"\s*(?:,|&| feat\.? | ft\.? | vs\.? | and | x | with )\s*", re.I)


def split_credit_artists(s):
    out = []
    for p in _CREDIT_SPLIT.split(s or ""):
        p = re.sub(r"\s*\*+\s*$", "", p.split("=")[0].strip())
        if p and normalize_label(p) not in ARTIST_STOPWORDS and p not in out:
            out.append(p)
    return out


def style_affinity_of(styles):
    """0–100 : poids moyen des styles d'une sortie dans tes 3 catégories (None si aucun)."""
    styles = [s for s in (styles or []) if s]
    if not styles:
        return None
    wmap = taste_weight_map()
    return round(100 * sum(wmap.get(style_key(s), 0.0) for s in styles) / len(styles))


def reco_index():
    """{label normalisé: score reco 0–100}, mémoïsé."""
    return _memo("reco_index", _sig_reco(),
                 lambda: {r["key"]: r["score"] for r in reco_rows()})


def album_score(r, reco_idx, ascore_map):
    """(score 0–100, détail) pour une sortie de recherche. Combine score reco du label,
    score des artistes du disque, et affinité de style de la sortie. Zéro appel API.
    `ascore_map` = {clé canonique: score} pré-calculé (perf)."""
    w = scoring()["album"]
    # label
    lscore = None
    for lb in (list(r.get("label", [])) + ([r["_base_label"]] if r.get("_base_label") else [])):
        v = reco_idx.get(normalize_label(lb))
        if v is not None:
            lscore = max(lscore or 0, v)
    # artistes (partie avant « - » du titre)
    a_str, sep, _ = (r.get("title") or "").partition(" - ")
    arts = split_credit_artists(a_str) if sep else []
    ascores = [ascore_map.get(canonical_artist_key(a), 0) for a in arts]
    _bl = float(w.get("artist_max_vs_mean", 0.6))
    a_term = (round(_bl * max(ascores) + (1 - _bl) * (sum(ascores) / len(ascores)))
              if ascores else None)
    # style
    s_term = style_affinity_of(r.get("style"))
    terms = []
    if lscore is not None:
        terms.append((float(w.get("label", 0.4)), lscore))
    if a_term is not None:
        terms.append((float(w.get("artist", 0.4)), a_term))
    if s_term is not None:
        terms.append((float(w.get("style", 0.2)), s_term))
    tot = sum(t[0] for t in terms)
    if not tot:
        return None, {}
    return (round(sum(t[0] * t[1] for t in terms) / tot),
            {"label": lscore, "artist": a_term, "style": s_term})


# ---------------------------------------------------------------- tracklist & liens d'écoute

_SIDE_MARKER = re.compile(
    r'^\s*(this|logo|flip|reverse|other|blank|etched|runout)?\s*side\b'
    r'|^\s*side\s*[a-d]{1,2}\s*$|^\s*[a-d]{1,2}\s*$', re.I)


def real_tracks(tracklist):
    """Ne garde que les vraies pistes : retire les en-têtes de face
    (« This Side », « Logo Side », « Side A »…) et les entrées non-musicales."""
    out = []
    for t in tracklist:
        if t.get("type_", "track") != "track":
            continue
        title = (t.get("title") or "").strip()
        if not title or _SIDE_MARKER.match(title):
            continue
        out.append(t)
    return out


def yt_search_url(query):
    import urllib.parse
    return "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query.strip())


def discogs_buy_url(row):
    """Lien Discogs pour une track : page release si connue, sinon recherche
    (biaisée vinyle). `row` = ligne de corpus djset."""
    import urllib.parse
    rid = row.get("vinyl_release_id") or row.get("release_id")
    if rid:
        return f"https://www.discogs.com/release/{rid}"
    q = urllib.parse.quote_plus(f"{row.get('artist', '')} {row.get('title', '')}".strip())
    return f"https://www.discogs.com/search/?q={q}&type=release&format_exact=Vinyl"


def bandcamp_buy_url(row):
    """Recherche Bandcamp (achat digital) pour une track."""
    import urllib.parse
    q = urllib.parse.quote_plus(f"{row.get('artist', '')} {row.get('title', '')}".strip())
    return f"https://bandcamp.com/search?q={q}&item_type=t"


def yt_video_titles(ids, api_key):
    """{video_id: titre} via l'API YouTube Data (videos.list, 50 max/appel)."""
    out = {}
    if not api_key or not ids:
        return out
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 50):
        try:
            r = requests.get(f"{YOUTUBE_API}/videos",
                             params={"part": "snippet", "id": ",".join(ids[i:i + 50]),
                                     "key": api_key}, timeout=20)
            if r.ok:
                for it in r.json().get("items", []):
                    out[it["id"]] = it.get("snippet", {}).get("title", "")
        except Exception:
            pass
    return out


def get_release_detail(release_id):
    if release_id not in st.session_state.release_cache:
        data = discogs_get(f"/releases/{release_id}")
        st.session_state.release_cache[release_id] = data
    return st.session_state.release_cache[release_id]


def render_tracklist_toggle(release_id, key_prefix, fallback_artist=""):
    """Bouton qui charge la tracklist à la demande (1 appel API par sortie) et
    affiche, pour chaque piste, un lien de recherche YouTube généré."""
    if not release_id:
        return
    key = f"tl_open_{key_prefix}_{release_id}"
    st.session_state.setdefault(key, False)
    if st.button("🎵 Pistes", key=f"btn_{key}"):
        st.session_state[key] = not st.session_state[key]
    if not st.session_state[key]:
        return
    try:
        data = get_release_detail(release_id)
    except Exception as e:
        st.error(f"Pistes indisponibles : {e}")
        return
    rel_artist = ", ".join(a.get("name", "") for a in data.get("artists", [])) or fallback_artist
    tracks = real_tracks(data.get("tracklist", []))
    if not tracks:
        st.caption("Pas de tracklist sur Discogs.")
        return
    for t in tracks:
        pos = (t.get("position") or "").strip() or "•"
        title = (t.get("title") or "").strip()
        t_artist = ", ".join(a.get("name", "") for a in t.get("artists", [])) or rel_artist
        url = yt_search_url(f"{t_artist} {title}")
        st.markdown(f"`{pos}` {title} — [▶ YouTube]({url})")


def render_release_card(image_url, title, label_text, year_text, release_id, key_prefix,
                        discogs_url, extra_caption=None, artist=None, styles=None,
                        affinity=None, image_width=None, album=None, album_detail=None,
                        feedback=None):
    if image_url:
        if image_width:
            st.image(image_url, width=image_width)
        else:
            st.image(image_url, use_column_width=True)
    if album is not None:
        d = album_detail or {}
        tip = " · ".join(f"{k} {v}" for k, v in
                         (("label", d.get("label")), ("artistes", d.get("artist")),
                          ("style", d.get("style"))) if v is not None)
        st.markdown(f'<span class="album-badge">🎯 {album}</span> '
                    f'<span class="rc-style">{tip}</span>', unsafe_allow_html=True)
    st.markdown(f"**{title or 'Sans titre'}**")
    if artist:
        st.caption(f"👤 {artist}")
    if label_text:
        aff = f" · affinité {affinity}%" if affinity is not None else ""
        st.caption(f"🏷️ {label_text}{aff}")
    meta = ""
    if styles:
        meta += f'<span class="rc-style">🎚️ {styles}</span>&nbsp;&nbsp;'
    if year_text:
        meta += f'<span class="catno-tag">{year_text}</span>'
    if meta:
        st.markdown(f'<div class="rc-meta">{meta}</div>', unsafe_allow_html=True)
    if extra_caption:
        st.caption(extra_caption)
    render_tracklist_toggle(release_id, key_prefix, fallback_artist=artist or "")
    if discogs_url:
        st.markdown(f"[Voir sur Discogs]({discogs_url})")
    if feedback and feedback.get("score") is not None:
        _done = st.session_state.setdefault("_fb_done", {})
        _mark = _done.get(f"{feedback['kind']}:{feedback['key']}")
        if _mark:
            st.caption("👍 noté" if _mark == "up" else "👎 noté")
        else:
            fb1, fb2, _ = st.columns([1, 1, 4])
            if fb1.button("👍", key=f"fbup_{key_prefix}", help="Pertinent pour moi"):
                log_feedback(feedback["kind"], feedback["key"], feedback.get("name", ""),
                             "up", feedback["score"], feedback.get("feat", {}))
                _done[f"{feedback['kind']}:{feedback['key']}"] = "up"
                st.rerun()
            if fb2.button("👎", key=f"fbdn_{key_prefix}", help="Pas pour moi"):
                log_feedback(feedback["kind"], feedback["key"], feedback.get("name", ""),
                             "down", feedback["score"], feedback.get("feat", {}))
                _done[f"{feedback['kind']}:{feedback['key']}"] = "down"
                st.rerun()
    st.divider()


# ---------------------------------------------------------------- UI

st.set_page_config(page_title="Crate Radar", page_icon="🎛️", layout="wide")


def _require_login():
    """Porte d'accès quand l'appli est exposée (déploiement). Sans APP_PASSWORD
    défini (usage local), aucun mot de passe n'est demandé."""
    pw = os.environ.get("APP_PASSWORD", "")
    if not pw:
        return
    if st.session_state.get("_authed"):
        return
    st.markdown("### 🔒 Crate Radar")
    got = st.text_input("Mot de passe", type="password", key="_login_pw")
    if got and hashlib.sha256(got.encode()).digest() == hashlib.sha256(pw.encode()).digest():
        st.session_state["_authed"] = True
        st.rerun()
    elif got:
        st.error("Mot de passe incorrect.")
    st.stop()


_require_login()

if "cfg" not in st.session_state:
    st.session_state.cfg = load_config()
if "labels" not in st.session_state:
    st.session_state.labels = list(cfg().get("labels", []))
if "resolved" not in st.session_state:
    st.session_state.resolved = load_resolved()
if "profile" not in st.session_state:
    st.session_state.profile = load_profile()
if "history" not in st.session_state:
    st.session_state.history = load_history()
if "collection" not in st.session_state:
    st.session_state.collection = load_collection_cache()
if "corpus" not in st.session_state:
    st.session_state.corpus = load_json(CORPUS_PATH, [])
if "lookup_cache" not in st.session_state:
    st.session_state.lookup_cache = load_json(LOOKUP_CACHE_PATH, {})
if "producer_graph" not in st.session_state:
    st.session_state.producer_graph = load_json(PRODUCER_GRAPH_PATH, {})
if "artists_resolved" not in st.session_state:
    st.session_state.artists_resolved = load_json(ARTISTS_RESOLVED_PATH, {})
if "feedback" not in st.session_state:
    st.session_state.feedback = load_json(FEEDBACK_PATH, [])
if "results" not in st.session_state:
    st.session_state.results = []
if "wl_results" not in st.session_state:
    st.session_state.wl_results = []
if "seller_results" not in st.session_state:
    st.session_state.seller_results = []
if "release_cache" not in st.session_state:
    st.session_state.release_cache = {}
for _k, _v in {"resolve_running": False, "resolve_queue": [], "resolve_total": 0,
               "resolve_done": 0, "resolve_last": "", "cleanup_open": False,
               "search_running": False, "search_queue": [], "search_acc": [],
               "search_done": 0, "search_total": 0, "search_params": {},
               "search_errors": [], "search_last": "",
               "profile_running": False, "profile_queue": [], "profile_total": 0,
               "profile_done": 0, "profile_last": "", "profile_errors": [],
               "ingest_running": False, "ingest_queue": [], "ingest_acc": [],
               "ingest_total": 0, "ingest_done": 0, "ingest_source": "", "ingest_last": "",
               "ingest_deep": True}.items():
    st.session_state.setdefault(_k, _v)


def sync_job_outputs():
    """Recharge en session les fichiers que les jobs (crate_jobs.py) écrivent, dès
    qu'ils changent sur disque — sinon l'appli garde la version chargée au démarrage."""
    mt = st.session_state.setdefault("_file_mtimes", {})
    plain = {"producer_graph": (PRODUCER_GRAPH_PATH, {}), "corpus": (CORPUS_PATH, []),
             "lookup_cache": (LOOKUP_CACHE_PATH, {}), "profile": (PROFILE_PATH, {}),
             "collection": (COLLECTION_CACHE_PATH, {}), "resolved": (RESOLVED_PATH, {}),
             "artists_resolved": (ARTISTS_RESOLVED_PATH, {})}
    for key, (path, default) in plain.items():
        try:
            m = os.path.getmtime(path)
        except OSError:
            continue
        if mt.get(path) != m:
            mt[path] = m
            st.session_state[key] = load_json(path, default)
    try:
        cm = os.path.getmtime(CONFIG_PATH)
    except OSError:
        cm = None
    if cm is not None and mt.get(CONFIG_PATH) != cm:
        mt[CONFIG_PATH] = cm
        fresh = load_config()
        st.session_state.cfg = fresh
        st.session_state.labels = list(fresh.get("labels", []))


sync_job_outputs()


def _auto_background():
    """Tâches de fond automatiques : enrichissement des ajouts, consolidation du
    corpus. Rien à cliquer."""
    if not cfg().get("token"):
        return
    q = load_json(PENDING_ENRICH_PATH, {})
    if (q.get("labels") or q.get("artists")) and not job_running("enrich"):
        job_launch("enrich", {})
    n_corpus = len(st.session_state.get("corpus", []))
    if (n_corpus and n_corpus != st.session_state.get("_last_merge_corpus_n")
            and not job_running("merge_corpus") and not job_running("enrich")):
        st.session_state["_last_merge_corpus_n"] = n_corpus
        job_launch("merge_corpus", {})


_auto_background()

st.markdown("""
<style>
    .crate-title { font-family: Georgia, serif; font-weight: 700; font-size: 34px; margin-bottom: 0; }
    .crate-title span { color: #B7311E; }
    .crate-sub { font-family: monospace; text-transform: uppercase; letter-spacing: 0.05em;
                 color: #5B564A; font-size: 12px; margin-top: 0; }
    .catno-tag { font-family: monospace; background: #C98A2C; color: #2a1c02; padding: 2px 6px;
                 border-radius: 3px; font-size: 11px; white-space: nowrap; }
    .rc-meta { margin: -2px 0 2px; line-height: 1.6; }
    .rc-style { color: #5B564A; font-size: 12px; }
    .album-badge { background: #2E7D32; color: #fff; font-weight: 700; font-family: monospace;
                   padding: 1px 7px; border-radius: 3px; font-size: 13px; }
</style>
""", unsafe_allow_html=True)

st.markdown('<p class="crate-title">Crate <span>Radar</span></p>', unsafe_allow_html=True)
st.markdown('<p class="crate-sub">Discogs × ta base de labels — version locale</p>', unsafe_allow_html=True)

# --- stop global : les tâches longues (résolution/scan/profilage) se relancent seules
#     toutes les ~5 s et s'exécutent quel que soit l'onglet affiché ---
_RUN_FLAGS = ("resolve_running", "search_running", "profile_running", "ingest_running")
_running_now = [k for k in _RUN_FLAGS if st.session_state.get(k)]
if _running_now:
    _rc1, _rc2 = st.columns([4, 1])
    _rc1.warning("Tâche en cours (rafraîchissement auto continu) : "
                 + ", ".join(k.replace("_running", "") for k in _running_now))
    if _rc2.button("⏹ Tout arrêter", type="primary", key="global_stop"):
        # ne pas perdre un import en cours : on sauvegarde ce qui est déjà traité
        if st.session_state.get("ingest_running") and st.session_state.get("ingest_acc"):
            corpus_add_rows(st.session_state.ingest_acc, st.session_state.get("ingest_source", "?"))
            save_json(LOOKUP_CACHE_PATH, st.session_state.get("lookup_cache", {}))
        for k in _RUN_FLAGS:
            st.session_state[k] = False
        for k in ("resolve_queue", "search_queue", "profile_queue", "ingest_queue"):
            st.session_state[k] = []
        st.rerun()

with st.expander("⚙️ Configuration (token + base de labels)", expanded=not cfg().get("token")):
    col1, col2 = st.columns(2)
    with col1:
        token_input = st.text_input("Token d'accès personnel Discogs", value=cfg().get("token", ""), type="password")
        if token_input != cfg().get("token", ""):
            cfg()["token"] = token_input
            persist()
        st.caption("Génère-le sur [discogs.com/settings/developers](https://www.discogs.com/settings/developers)")
    with col2:
        st.markdown("**Base de labels**")
        st.caption(
            f"{len(st.session_state.labels)} label(s) en base, sauvegardés dans "
            "`crate_radar_config.json`. Import CSV, ajout et retrait se font dans "
            "l'onglet **🏷️ Ma base**."
        )

status = "✅ Token ok" if cfg().get("token") else "⚠️ Token manquant"
st.caption(f"{status} · {len(st.session_state.labels)} labels en base")

with st.expander("🧹 Noms canoniques Discogs (résolution & profilage)",
                 expanded=st.session_state.cleanup_open):
    st.write(
        "Depuis maintenant, **chaque label / artiste ajouté est résolu et profilé "
        "automatiquement en arrière-plan** (job `enrich`) — plus rien à lancer à la main. "
        "Le bouton ci-dessous fait la passe unique sur l'existant."
    )
    _pend = load_json(PENDING_ENRICH_PATH, {})
    _npend = len(_pend.get("labels", [])) + len(_pend.get("artists", []))
    if _npend:
        st.caption(f"⏳ {_npend} ajout(s) en cours d'enrichissement…")
    render_job("enrich", "Enrichissement auto")

    gnc1, gnc2 = st.columns([2, 3])
    if not job_running("canonicalize") and gnc1.button("🧹 Grand nettoyage Discogs",
                                                       disabled=not cfg().get("token")):
        job_launch("canonicalize", {"scope": "corpus"})
        st.rerun()
    gnc2.caption("Réécrit toute la base (labels + artistes) et les champs artiste/label du "
                 "corpus avec les noms Discogs exacts. Tâche de fond, reprenable — plusieurs "
                 "heures au 1ᵉʳ passage.")
    render_job("canonicalize", "Grand nettoyage")

    st.divider()
    st.caption("— 🛠 Outils d'import initial (usage ponctuel) —")
    total = len(st.session_state.labels)
    done = sum(1 for l in st.session_state.labels if normalize_label(l) in st.session_state.resolved)
    not_found = sum(1 for l in st.session_state.labels
                     if st.session_state.resolved.get(normalize_label(l), {}).get("status") == "not_found")
    st.progress(done / total if total else 0, text=f"{done}/{total} labels résolus ({not_found} introuvables sur Discogs)")

    st.caption("Avec la limite Discogs (~1 requête/seconde en pratique), résoudre toute la base prend plusieurs heures. "
               "La progression est sauvegardée à chaque label : tu peux arrêter puis reprendre quand tu veux.")

    # Traitement par petits lots avec rerun entre chaque, pour garder le bouton « Arrêter » réactif.
    RESOLVE_CHUNK = 5

    if st.session_state.resolve_running:
        st.session_state.cleanup_open = True  # garde l'expander ouvert pendant la résolution
        rdone, rtot = st.session_state.resolve_done, st.session_state.resolve_total
        st.progress(rdone / rtot if rtot else 0,
                    text=f"Résolution : {rdone}/{rtot} — {st.session_state.resolve_last or '…'}")
        stop = st.button("⏹ Arrêter la résolution")
        if stop:
            st.session_state.resolve_running = False
            st.session_state.resolve_queue = []
            st.success(f"Résolution arrêtée à {rdone}/{rtot}. Progression sauvegardée — "
                       "relance quand tu veux, elle reprendra là où elle en est.")
        else:
            chunk = st.session_state.resolve_queue[:RESOLVE_CHUNK]
            st.session_state.resolve_queue = st.session_state.resolve_queue[RESOLVE_CHUNK:]
            token = cfg().get("token", "")
            for j, name in enumerate(chunk):
                try:
                    dname, did, status_r, cands = resolve_one_label(name, token=token)
                except Exception as e:
                    dname, did, status_r, cands = None, None, f"error: {e}", []
                st.session_state.resolved[normalize_label(name)] = {
                    "original": name, "discogs_name": dname, "discogs_id": did,
                    "status": status_r, "candidates": cands,
                }
                save_resolved(st.session_state.resolved)
                st.session_state.resolve_done += 1
                st.session_state.resolve_last = f"{name} → {dname or '?'} ({status_r})"
                more_coming = st.session_state.resolve_queue or j < len(chunk) - 1
                if more_coming:
                    time.sleep(1.1)
            if not st.session_state.resolve_queue:
                st.session_state.resolve_running = False
                st.success(f"Terminé — {st.session_state.resolve_done} label(s) traité(s) sur ce lancement.")
            st.rerun()
    else:
        _prof = st.session_state.get("profile", {})
        _wmap = taste_weight_map()
        _floor = int(scoring()["label_affinity_floor"] or 0)
        rc1, rc2 = st.columns([3, 2])
        only_prof = rc1.checkbox(
            "Seulement les labels profilés au-dessus du seuil d'affinité", value=True,
            key="resolve_only_profiled",
            help="Recommandé : ne résous que les labels qui comptent (profilés + assez proches "
                 "de tes goûts), pas les milliers d'autres.")
        thr = rc1.slider("Affinité minimale", 0, 100,
                         (max(_floor, 30) if _floor == 0 else _floor), 5,
                         key="resolve_aff_min", disabled=not only_prof)
        batch_size = rc2.number_input("Nb max à résoudre maintenant",
                                      min_value=10, max_value=5000, value=200, step=10)
        _unres = [l for l in st.session_state.labels
                  if normalize_label(l) not in st.session_state.resolved]
        if only_prof:
            _pool = [l for l in _unres
                     if _prof.get(normalize_label(l)) is not None
                     and affinity_score(_prof[normalize_label(l)], _wmap) >= thr]
        else:
            _pool = _unres
        rc2.caption(f"**{len(_pool)}** label(s) à résoudre dans le périmètre"
                    + (f" · {len(_unres)} non résolus au total" if only_prof else ""))
        if st.button("Lancer la résolution"):
            if not cfg().get("token"):
                st.error("Renseigne d'abord ton token Discogs.")
            else:
                todo = _pool[:int(batch_size)]
                if not todo:
                    st.success("Aucun label à résoudre dans ce périmètre "
                               + ("(baisse le seuil, ou profile davantage la base)."
                                  if only_prof else "(tout est déjà résolu)."))
                else:
                    st.session_state.resolve_queue = todo
                    st.session_state.resolve_total = len(todo)
                    st.session_state.resolve_done = 0
                    st.session_state.resolve_last = ""
                    st.session_state.resolve_running = True
                    st.session_state.cleanup_open = True
                    st.rerun()

    approx_keys = [k for k, v in st.session_state.resolved.items() if v.get("status") == "approx"]
    confirmed_n = sum(1 for v in st.session_state.resolved.values() if v.get("status") == "confirmed")
    if approx_keys or confirmed_n:
        st.divider()
        st.markdown(f"### ⚠️ Correspondances approximatives — {len(approx_keys)} à vérifier"
                    + (f" · {confirmed_n} validée(s)" if confirmed_n else ""))
        if confirmed_n and st.button(f"↩️ Repasser les {confirmed_n} validées en « à vérifier »"):
            for v in st.session_state.resolved.values():
                if v.get("status") == "confirmed":
                    v["status"] = "approx"
            save_resolved(st.session_state.resolved)
            st.session_state.cleanup_open = True
            st.rerun()

    if approx_keys:
        show_approx = st.checkbox("Traiter les correspondances approximatives", key="show_approx")
        st.caption(f"{len(approx_keys)} restante(s).")
        if show_approx:
            st.session_state.cleanup_open = True
            # ----- traitement groupé par score de similarité (aucun appel API) -----
            st.markdown("**Traitement groupé**")
            st.caption(
                "Compare le nom d'origine à la proposition Discogs. Au-dessus du seuil, "
                "la proposition est probablement bonne → validation en un clic. Réversible "
                "via le bouton ci-dessus."
            )
            thr = st.slider("Seuil de similarité", 0.50, 1.00, 0.82, 0.01)
            scored = [(k, name_similarity(st.session_state.resolved[k].get("original", k),
                                          st.session_state.resolved[k].get("discogs_name") or ""))
                      for k in approx_keys]
            above = [k for k, s in scored if s >= thr]
            bc1, bc2 = st.columns(2)
            if bc1.button(f"✅ Valider les {len(above)} ≥ {thr:.2f}", disabled=not above):
                now = datetime.now().isoformat(timespec="seconds")
                for k in above:
                    st.session_state.resolved[k].update(
                        status="confirmed", reviewed_at=now, reviewed_by="bulk")
                save_resolved(st.session_state.resolved)
                st.rerun()
            if bc2.button(f"⚡ Tout valider ({len(approx_keys)}) sans filtre"):
                now = datetime.now().isoformat(timespec="seconds")
                for k in approx_keys:
                    st.session_state.resolved[k].update(
                        status="confirmed", reviewed_at=now, reviewed_by="bulk")
                save_resolved(st.session_state.resolved)
                st.rerun()
            st.caption(f"{len(above)} validé(s) au seuil actuel · "
                       f"{len(approx_keys) - len(above)} resteraient à vérifier à la main.")

            st.divider()
            st.markdown("**Revue une par une**")
            st.caption(
                "Pour chaque label : **Valider** la proposition choisie, ou **Introuvable** "
                "si rien ne colle. Le champ de recherche donne d'autres propositions. "
                "Les 15 premiers sont affichés — traite-les et les suivants apparaissent."
            )
            REVIEW_N = 15
            for k in approx_keys[:REVIEW_N]:
                entry = st.session_state.resolved[k]
                orig = entry.get("original", k)
                cands = entry.get("candidates") or []
                # garde la proposition actuelle dans la liste même si les candidats manquent
                if entry.get("discogs_name") and not any(
                        c.get("name") == entry["discogs_name"] for c in cands):
                    cands = [{"name": entry["discogs_name"], "id": entry.get("discogs_id")}] + cands

                st.markdown(f"**{orig}**")
                c1, c2, c3 = st.columns([3, 1, 1])
                if cands:
                    opt_labels = [f"{c['name']}  ·  id {c.get('id', '?')}" for c in cands]
                    sel = c1.selectbox("Proposition", options=list(range(len(cands))),
                                       format_func=lambda i: opt_labels[i],
                                       key=f"apx_sel_{k}", label_visibility="collapsed")
                else:
                    c1.caption("Aucune proposition en cache — lance une recherche ci-dessous.")
                    sel = None
                if c2.button("✅ Valider", key=f"apx_ok_{k}", disabled=sel is None):
                    chosen = cands[sel]
                    entry["discogs_name"] = chosen["name"]
                    entry["discogs_id"] = chosen.get("id")
                    entry["status"] = "confirmed"
                    entry["reviewed_at"] = datetime.now().isoformat(timespec="seconds")
                    save_resolved(st.session_state.resolved)
                    st.rerun()
                if c3.button("🚫 Introuvable", key=f"apx_no_{k}"):
                    entry.update(discogs_name=None, discogs_id=None, status="not_found",
                                 reviewed_at=datetime.now().isoformat(timespec="seconds"))
                    save_resolved(st.session_state.resolved)
                    st.rerun()

                if st.checkbox("🔎 Autre recherche Discogs", value=not cands, key=f"apx_more_{k}"):
                    sc1, sc2 = st.columns([4, 1])
                    q = sc1.text_input("Terme", value=orig, key=f"apx_q_{k}",
                                       label_visibility="collapsed")
                    if sc2.button("Chercher", key=f"apx_search_{k}"):
                        try:
                            entry["candidates"] = label_candidates(q, per_page=8)
                            save_resolved(st.session_state.resolved)
                            if not entry["candidates"]:
                                st.warning("Aucun résultat.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Erreur Discogs : {e}")
                st.divider()

# Navigation : un seul onglet exécuté par rerun (st.tabs exécute les 9 corps à
# chaque interaction — d'où la lenteur). Le radio conserve le choix en session.
_NAV = ["🔍 Recherche", "📻 Liste de veille", "🏪 Mes vendeurs", "🏷️ Ma base",
        "🎯 Profilage", "🎧 Sources & reco", "🎤 Mes artistes", "🎚️ Sets",
        "🎛️ Réglages", "📈 Apprentissage"]
_nav = st.radio("Navigation", _NAV, horizontal=True, label_visibility="collapsed", key="_nav")
st.divider()

# ---------------------------------------------------------------- Tab: Sets (DJ sets ingérés)

if _nav == "🎚️ Sets":
    st.write("Toutes les tracks extraites des DJ sets YouTube, par DJ puis par vidéo. "
             "Liens cliquables pour écouter (la track si YouTube l'a identifiée, sinon "
             "recherche YouTube ; + le set de provenance).")
    dj_rows = [r for r in st.session_state.corpus if r.get("source") == "djset"]
    if not dj_rows:
        st.info("Aucun set ingéré. Onglet **🎧 Sources & reco → DJ sets**.")
    else:
        # backfill des titres de set manquants (anciens imports) via l'API YouTube
        _tried = st.session_state.setdefault("_djtitle_tried", set())
        _miss = sorted({r["video"] for r in dj_rows
                        if r.get("video") and not r.get("set_title") and r["video"] not in _tried})
        if _miss and cfg().get("youtube_api_key"):
            _titles = yt_video_titles(_miss, cfg()["youtube_api_key"])
            if _titles:
                for r in st.session_state.corpus:
                    if (r.get("source") == "djset" and not r.get("set_title")
                            and _titles.get(r.get("video"))):
                        r["set_title"] = _titles[r["video"]]
                save_json(CORPUS_PATH, st.session_state.corpus)
            _tried.update(_miss)
            dj_rows = [r for r in st.session_state.corpus if r.get("source") == "djset"]
        # note de chaque track (même méthode que le score album) — mémoïsée : ne se
        # recalcule que si le corpus djset ou les poids/scores changent.
        def _build_djview():
            ridx = reco_index()
            amap = {k: v[0] for k, v in artist_scores().items()}
            bd = {}
            for r in dj_rows:
                pseudo = {"label": [r["label"]] if r.get("label") else [],
                          "title": f"{r.get('artist', '')} - {r.get('title', '')}",
                          "style": r.get("style") or []}
                r["_score"], r["_detail"] = album_score(pseudo, ridx, amap)
                bd.setdefault(r.get("dj", "?"), {}).setdefault(r.get("video", "?"), []).append(r)
            return bd, sorted(bd)

        _dv_sig = (len(dj_rows), _sig_reco(), _sig_artist_scores())
        by_dj, djs_all = _memo("djview_build", _dv_sig, _build_djview)
        c1, c2, c3 = st.columns([2, 1, 1])
        pick = c1.multiselect("DJs affichés", djs_all, default=djs_all, key="djview_pick")
        only_lab = c2.checkbox("Avec label seulement", value=False, key="djview_lab")
        min_sc = c3.slider("Note min", 0, 100, 0, 5, key="djview_minsc")
        tot_tracks = sum(len(v) for dj in pick for v in by_dj.get(dj, {}).values())
        st.caption(f"{tot_tracks} track(s) · {len(pick)} DJ(s) · "
                   f"{sum(len(by_dj.get(dj, {})) for dj in pick)} set(s)")
        for dj in pick:
            vids = by_dj[dj]
            n_tr = sum(len(v) for v in vids.values())
            with st.expander(f"🎧 {dj} — {n_tr} tracks · {len(vids)} set(s)"):
                dc1, dc2 = st.columns([3, 1])
                if dc2.button(f"🗑️ Retirer « {dj} »", key=f"djdel_{dj}"):
                    _vids_del = set(vids)
                    st.session_state.corpus = [
                        r for r in st.session_state.corpus
                        if not (r.get("source") == "djset" and r.get("dj") == dj)]
                    save_json(CORPUS_PATH, st.session_state.corpus)
                    _sp = os.path.join(_HERE, "djset_seen.json")
                    save_json(_sp, sorted(set(load_json(_sp, [])) - _vids_del))
                    st.success(f"« {dj} » retiré du corpus ({n_tr} tracks). "
                               "Ses vidéos pourront être re-scannées.")
                    st.rerun()
                # sets triés par meilleure note de track
                def _set_best(kv):
                    return max((x.get("_score") or 0 for x in kv[1]), default=0)
                for vid, tracks in sorted(vids.items(), key=_set_best, reverse=True):
                    stitle = tracks[0].get("set_title") or vid
                    scs = [x.get("_score") for x in tracks if x.get("_score") is not None]
                    savg = f" · note moy. {round(sum(scs) / len(scs))}" if scs else ""
                    sc1, sc2 = st.columns([5, 1])
                    sc1.markdown(f"**[{stitle}](https://www.youtube.com/watch?v={vid})** "
                                 f"· {len(tracks)} track(s){savg}")
                    if sc2.button("🗑️ ce set", key=f"setdel_{dj}_{vid}"):
                        st.session_state.corpus = [
                            r for r in st.session_state.corpus
                            if not (r.get("source") == "djset" and r.get("video") == vid)]
                        save_json(CORPUS_PATH, st.session_state.corpus)
                        _sp = os.path.join(_HERE, "djset_seen.json")
                        save_json(_sp, sorted(set(load_json(_sp, [])) - {vid}))
                        st.rerun()
                    _fbdone = st.session_state.setdefault("_fb_done", {})
                    for ti, t in enumerate(sorted(tracks, key=lambda x: -(x.get("_score") or -1))):
                        if only_lab and not t.get("label"):
                            continue
                        sv = t.get("_score")
                        if sv is not None and sv < min_sc:
                            continue
                        badge = (f"<span class='album-badge'>🎯 {sv}</span> " if sv is not None
                                 else "<span class='rc-style'>—</span> ")
                        lab = f" · _{t['label']}_" if t.get("label") else " · —"
                        listen = t.get("track_url") or yt_search_url(f"{t['artist']} {t['title']}")
                        buys = [f"[💿 Discogs]({discogs_buy_url(t)})",
                                f"[🛒 Bandcamp]({bandcamp_buy_url(t)})"]
                        if not (t.get("vinyl_release_id") or t.get("release_id")):
                            buys.reverse()          # pas de pressage repéré → digital d'abord
                        tc1, tc2, tc3 = st.columns([10, 1, 1])
                        tc1.markdown(
                            f"&nbsp;&nbsp;{badge}[{t['artist']} — {t['title']}]({listen}){lab} "
                            "· " + " · ".join(buys),
                            unsafe_allow_html=True)
                        _fk = normalize_label(f"{t.get('artist', '')} - {t.get('title', '')}")
                        _kp = f"djtrk_{dj}_{vid}_{ti}"
                        _mk = _fbdone.get(f"album:{_fk}")
                        if _mk:
                            tc2.caption("👍" if _mk == "up" else "👎")
                        elif sv is not None:
                            _dd = t.get("_detail") or {}
                            _ft = {k: (_dd.get(k) or 0) / 100 for k in ALBUM_FEAT_KEYS}
                            if tc2.button("👍", key=f"fbup_{_kp}"):
                                log_feedback("album", _fk, f"{t['artist']} — {t['title']}"[:90],
                                             "up", sv, _ft)
                                _fbdone[f"album:{_fk}"] = "up"
                                st.rerun()
                            if tc3.button("👎", key=f"fbdn_{_kp}"):
                                log_feedback("album", _fk, f"{t['artist']} — {t['title']}"[:90],
                                             "down", sv, _ft)
                                _fbdone[f"album:{_fk}"] = "down"
                                st.rerun()

# ---------------------------------------------------------------- Tab: Mes artistes

if _nav == "🎤 Mes artistes":
    st.write(
        "Ta liste d'artistes préférés, hiérarchisée en 3 catégories comme les styles. "
        "Elle donnera un **score d'artiste** qui entrera dans la reco (aujourd'hui : liste "
        "manuelle + présence dans ton corpus/collection ; bientôt : proximité dans le graphe "
        "de producteurs)."
    )

    a_cats = cfg().get("artist_categories", DEFAULT_ARTIST_CATEGORIES)
    st.session_state.setdefault("_seed_editor_n", 0)
    # les text_areas ne se resynchronisent pas seules : on force leur contenu quand
    # la config change (après un « Appliquer » du tableau).
    for cid in ("1", "2", "3"):
        want = "\n".join(a_cats.get(cid, []))
        if st.session_state.get(f"_artcat_shadow_{cid}") != want:
            st.session_state[f"artcat_{cid}"] = want
            st.session_state[f"_artcat_shadow_{cid}"] = want

    acol = st.columns(3)
    new_a_cats = {}
    for cid, col in zip(("1", "2", "3"), acol):
        with col:
            st.caption(f"Catégorie {cid} — {CAT_LABELS[cid]} · poids {scoring()['artist_tiers'][cid]}")
            raw = st.text_area(f"Artistes cat. {cid}", height=180,
                               key=f"artcat_{cid}", label_visibility="collapsed")
            new_a_cats[cid] = [s.strip() for s in raw.splitlines() if s.strip()]
    n_art = sum(len(v) for v in new_a_cats.values())
    sc1, sc2 = st.columns([1, 3])
    if sc1.button("💾 Enregistrer les listes"):
        set_artist_categories(new_a_cats)
        for cid in ("1", "2", "3"):
            st.session_state.pop(f"_artcat_shadow_{cid}", None)
        st.rerun()
    sc2.caption(f"{n_art} artiste(s) dans les listes. Les **poids** (rangs d'artistes, score "
                "d'artiste : manuel/corpus/collection/graphe/sets) sont dans l'onglet **🎛️ Réglages**.")

    st.divider()
    st.subheader("Alimenter depuis ce que j'écoute déjà")
    st.caption("Artistes de ton corpus (YouTube/Bandcamp) + ta collection Discogs. Colonne "
               "**« → Cat »** : choisis une catégorie, puis **Appliquer** — ça les ajoute aux "
               "listes ci-dessus.")

    corpus_c, coll_c, _graph, disp = artist_signal()
    tiers = artist_tier_map()
    order = sorted(set(corpus_c) | set(coll_c),
                   key=lambda k: (coll_c.get(k, 0) + corpus_c.get(k, 0)), reverse=True)
    flt_a = st.text_input("Filtrer", key="artist_seed_filter", placeholder="nom d'artiste…")
    if flt_a:
        order = [k for k in order if flt_a.lower() in disp.get(k, k).lower()]
    order = order[:120]
    seed_rows = [{"Artiste": disp.get(k, k), "Coll": coll_c.get(k, 0),
                  "Corp": corpus_c.get(k, 0),
                  "Actuel": tiers.get(k, "—"), "→ Cat": ""}
                 for k in order]
    edited = st.data_editor(
        seed_rows, hide_index=True, use_container_width=True, height=380,
        key=f"seed_editor_{st.session_state['_seed_editor_n']}",
        column_config={
            "Artiste": st.column_config.TextColumn(disabled=True),
            "Coll": st.column_config.NumberColumn(disabled=True, width="small"),
            "Corp": st.column_config.NumberColumn(disabled=True, width="small"),
            "Actuel": st.column_config.TextColumn(disabled=True, width="small"),
            "→ Cat": st.column_config.SelectboxColumn(options=["", "1", "2", "3"], width="small"),
        })
    picks = [(r["Artiste"], str(r.get("→ Cat") or "").strip())
             for r in edited if str(r.get("→ Cat") or "").strip() in ("1", "2", "3")]
    if st.button(f"Appliquer ({len(picks)})", type="primary", disabled=not picks):
        merged = {cid: list(cfg().get("artist_categories", {}).get(cid, [])) for cid in ("1", "2", "3")}
        for name, cid in picks:
            for lst in merged.values():
                if name in lst:
                    lst.remove(name)
            merged[cid].append(name)
        set_artist_categories(merged)
        for cid in ("1", "2", "3"):
            st.session_state.pop(f"_artcat_shadow_{cid}", None)
        st.session_state["_seed_editor_n"] += 1   # remet la colonne « → Cat » à zéro
        st.rerun()

    st.divider()
    st.subheader("Aperçu du score d'artiste")
    ascores = sorted(artist_scores().values(), reverse=True)
    top_rows = [{"Artiste": d, "Score": s, "Pourquoi": w} for s, d, w in ascores[:40]]
    st.dataframe(top_rows, hide_index=True, use_container_width=True)

    st.divider()
    st.subheader("🧹 Résolution des artistes vers Discogs")
    st.caption("Associe chaque artiste de ta liste à son identifiant Discogs exact (comme pour "
               "les labels). Le graphe utilise alors ces IDs — fini les collisions "
               "« Buck » ≠ « DJ Buck » ou les mauvais homonymes.")
    ar = st.session_state.get("artists_resolved", {})
    manual_all = [n for cid in ("1", "2", "3")
                  for n in cfg().get("artist_categories", {}).get(cid, [])]
    # noms bruts de toutes les sources -> clés normalize_label (index de artists_resolved.json)
    raw_names = set(manual_all)
    for _r in st.session_state.get("corpus", []):
        if _r.get("artist"):
            raw_names.add(_r["artist"])
    for _a in st.session_state.get("collection", {}).get("artist_counts", {}):
        raw_names.add(_a)
    raw_names = {n for n in raw_names if n and normalize_label(n) not in ARTIST_STOPWORDS}
    all_keys = {normalize_label(n) for n in raw_names}
    manual_keys = {normalize_label(n) for n in manual_all}
    n_res = sum(1 for k in all_keys if k in ar)
    n_approx = sum(1 for k in all_keys if ar.get(k, {}).get("status") == "approx")
    st.progress(n_res / len(all_keys) if all_keys else 0,
                text=f"{n_res}/{len(all_keys)} artistes résolus · {n_approx} approx à vérifier "
                     f"· {sum(1 for k in all_keys if ar.get(k, {}).get('status') == 'not_found')} introuvables")

    if not render_job("resolve_artists", "Résolution artistes") \
            and not job_running("resolve_artists"):
        scope = st.radio("Périmètre", [f"Ma liste ({len(manual_all)})",
                                       f"Liste + corpus + collection ({len(all_keys)})"],
                         horizontal=True, key="ar_scope")
        rc1, rc2 = st.columns([3, 1])
        reforce = rc1.checkbox("Re-résoudre aussi ceux déjà validés", key="ar_force")
        if rc2.button("Lancer la résolution", key="btn_resolve_artists",
                      disabled=not cfg().get("token")):
            job_launch("resolve_artists",
                       {"force": reforce, "scope": "all" if scope.startswith("Liste +") else "manual"})
            st.rerun()

    approx = sorted(((k, ar[k]) for k in all_keys if ar.get(k, {}).get("status") == "approx"),
                    key=lambda t: (t[0] not in manual_keys, t[1].get("original", "")))
    if approx:
        with st.expander(f"⚠️ {len(approx)} correspondance(s) approximative(s) à vérifier "
                         "(ta liste d'abord)"):
            for k, e in approx[:30]:
                st.markdown(f"**{e['original']}** → proposition : *{e.get('discogs_name')}*")
                cands = e.get("candidates") or []
                r1, r2, r3 = st.columns([4, 1, 1])
                if cands:
                    idx = r1.selectbox(
                        "Bon artiste", list(range(len(cands))),
                        format_func=lambda i, cc=cands: f"{cc[i]['name']} · id {cc[i]['id']}",
                        key=f"ares_sel_{k}", label_visibility="collapsed")
                else:
                    r1.caption("aucun candidat Discogs")
                    idx = None
                if r2.button("✅", key=f"ares_ok_{k}", disabled=idx is None):
                    c = cands[idx]
                    ar[k] = {**e, "discogs_name": c["name"], "discogs_id": c["id"],
                             "status": "confirmed"}
                    save_json(ARTISTS_RESOLVED_PATH, ar)
                    st.rerun()
                if r3.button("🚫", key=f"ares_no_{k}"):
                    ar[k] = {**e, "status": "not_found", "discogs_id": None, "discogs_name": None}
                    save_json(ARTISTS_RESOLVED_PATH, ar)
                    st.rerun()

    st.divider()
    st.subheader("🕸️ Graphe de producteurs")
    st.caption("Interroge la discographie de tes artistes-graines sur Discogs et remonte les "
               "**co-crédités** et **co-labels**. Le **graphe global** part de *tous* tes artistes "
               "résolus (liste + corpus + collection) : chaque artiste d'un résultat de recherche "
               "obtient alors un vrai score de proximité. Le score est **recalculé à la volée** "
               "quand tu recatégorises un artiste, sans reconstruire.")
    _gmeta = st.session_state.producer_graph
    _grr = graph_rescore()
    if _gmeta.get("built_at"):
        st.caption(f"Dernier calcul : {_gmeta['built_at'].replace('T', ' ')} · "
                   f"mode **{_gmeta.get('mode', '?')}** · "
                   f"{_gmeta.get('n_resolved_seeds', len(_gmeta.get('seeds', {})))} graines · "
                   f"{len(_grr.get('artists', {}))} artistes proches · "
                   f"{len(_grr.get('labels', {}))} labels candidats.")

    if render_job("build_graph", "Graphe de producteurs"):
        pass
    elif not job_running("build_graph"):
        _cc, _lc, _gg, _dp = artist_signal()          # clés canoniques (id:X après résolution)
        _tier = artist_tier_map()
        _manual = [n for cid in ("1", "2", "3")
                   for n in cfg().get("artist_categories", {}).get(cid, [])]
        _seed_meta = {}   # display -> {"w": présence, "orig": nom original}
        for k in set(_cc) | set(_lc) | set(_tier):
            nm = _dp.get(k, k)
            _seed_meta[nm] = {"w": _lc.get(k, 0) + _cc.get(k, 0), "orig": nm}
        for n in _manual:
            nm = canonical_artist_name(n)
            _seed_meta.setdefault(nm, {"w": 0, "orig": n})
        seed_choices = sorted(_seed_meta, key=lambda nm: -_seed_meta[nm]["w"])
        _n_resolved = sum(1 for v in st.session_state.get("artists_resolved", {}).values()
                          if v.get("discogs_id") and v.get("status") in
                          ("exact", "approx", "confirmed"))

        mode = st.radio("Graines du graphe",
                        [f"Graphe global ({_n_resolved} artistes résolus)",
                         "Top automatique par score", "Je choisis les artistes"],
                        key="graph_seed_mode")
        n_page = st.slider("Pages de discographie par graine", 1, 3, 2, key="graph_pages")
        params = {"pages": n_page}
        if mode.startswith("Graphe global"):
            params["mode"] = "global"
            _pg = st.session_state.producer_graph
            _existing = set(_pg.get("seeds", {})) if _pg.get("edges") is not None else set()
            _current = {f"id:{v['discogs_id']}" for v in st.session_state.get("artists_resolved", {}).values()
                        if v.get("discogs_id") and v.get("status") in ("exact", "approx", "confirmed")}
            _new = _current - _existing
            _full_min = max(1, round(_n_resolved * (n_page + 1) * 1.1 / 60))
            st.warning(f"⏳ **Construction complète** : ~{_n_resolved} artistes-graines, "
                       f"~{_n_resolved}–{_n_resolved * (n_page + 1)} appels Discogs, "
                       f"**≈ {_full_min} min**. À ne faire qu'une fois.")
            gb1, gb2 = st.columns(2)
            if gb1.button("Construire le graphe global (complet)", type="primary",
                          disabled=not cfg().get("token")):
                job_launch("build_graph", {"pages": n_page, "mode": "global"})
                st.rerun()
            if _existing:
                _upd_min = max(1, round(len(_new) * (n_page + 1) * 1.1 / 60))
                gb2.caption(f"**{len(_new)}** nouvel(s) artiste(s) résolu(s) depuis le dernier "
                            f"graphe (≈ {_upd_min} min).")
                if gb2.button(f"Mettre à jour (+{len(_new)} artistes)",
                              disabled=not cfg().get("token") or not _new):
                    job_launch("build_graph", {"pages": n_page, "mode": "global",
                                               "incremental": True})
                    st.rerun()
                st.caption("Un changement de **catégorie** ne nécessite AUCune reconstruction — "
                           "le score se recalcule tout seul. Seuls les artistes **nouvellement "
                           "ajoutés/résolus** exigent une mise à jour.")
            n = 2  # court-circuite le bouton générique plus bas
        elif mode == "Je choisis les artistes":

            def _cat_choices(cids):
                out = set()
                for cid in cids:
                    for n in cfg().get("artist_categories", {}).get(cid, []):
                        out.add(canonical_artist_name(n))
                return {n for n in out if n in _seed_meta}

            pc1, pc2, pc3 = st.columns(3)
            if pc1.button("＋ mes catégorie 1"):
                st.session_state["graph_seed_pick"] = sorted(
                    set(st.session_state.get("graph_seed_pick", [])) | _cat_choices(("1",)))
                st.rerun()
            if pc2.button("＋ cat. 1 + 2"):
                st.session_state["graph_seed_pick"] = sorted(
                    set(st.session_state.get("graph_seed_pick", [])) | _cat_choices(("1", "2")))
                st.rerun()
            if pc3.button("Vider la sélection"):
                st.session_state["graph_seed_pick"] = []
                st.rerun()
            picked_seeds = st.multiselect(
                f"Artistes-graines — choisis dans la liste ({len(seed_choices)} identifiés)",
                seed_choices, key="graph_seed_pick",
                placeholder="Tape un nom pour chercher…")
            # on envoie au job le nom "original" (ses lookups artists_resolved.json sont par nom)
            params["seed_names"] = [_seed_meta.get(d, {}).get("orig", d) for d in picked_seeds]
            n = len(picked_seeds)
            if 0 < n < 3:
                st.warning("Sélectionne au moins 3 graines pour un graphe utile.")
        else:
            n = st.slider("Nombre d'artistes-graines (les mieux notés — tes cat.1 en tête)",
                          5, 200, 40, 5, key="graph_seed_n")
            params["seeds"] = n
        if not mode.startswith("Graphe global"):
            est = max(1, round(n * (1 + n_page) * 1.1 / 60))
            st.caption(f"{n} graine(s) → ≈ {n}–{n * (n_page + 1)} appels API, ~{est} min.")
            if st.button("Construire le graphe", type="primary",
                         disabled=not cfg().get("token") or n < 2):
                job_launch("build_graph", params)
                st.rerun()

    if _grr.get("artists"):
        tiers_now = artist_tier_map()
        cand_all = [(k, v) for k, v in _grr["artists"].items() if k not in tiers_now]

        # --- ajout automatique en catégorie 1 au-dessus d'un seuil ---
        aa1, aa2 = st.columns([2, 3])
        g_thr = aa1.slider("Ajout auto en catégorie 1 si score ≥", 0.0, 20.0, 5.0, 0.5,
                           key="g_auto_thr")
        only_c1 = aa2.checkbox("… et ≥ 1 sortie avec un artiste catégorie 1", value=True)
        auto_c = [(k, v) for k, v in cand_all if v["score"] >= g_thr
                  and (not only_c1 or v.get("cat1_hits", 0) >= 1)]
        if aa2.button(f"➕ Ajouter {len(auto_c)} artiste(s) en catégorie 1",
                      type="primary", disabled=not auto_c):
            merged = {cid: list(cfg().get("artist_categories", {}).get(cid, []))
                      for cid in ("1", "2", "3")}
            for k, v in auto_c:
                merged["1"].append(v["name"])
            set_artist_categories(merged)
            for cid in ("1", "2", "3"):
                st.session_state.pop(f"_artcat_shadow_{cid}", None)
            st.success(f"{len(auto_c)} artiste(s) ajoutés en catégorie 1. "
                       "Reconstruis le graphe pour propager l'effet.")
            st.rerun()
        if auto_c:
            aa1.caption("Seront ajoutés : "
                        + ", ".join(v["name"] for _, v in auto_c[:15])
                        + (f" … +{len(auto_c) - 15}" if len(auto_c) > 15 else ""))

        st.markdown("**Artistes proches** — candidats à ajouter à ta liste")
        for k, v in cand_all[:40]:
            gc1, gc2, gc3, gc4 = st.columns([5, 1, 1, 1])
            star = "⭐ " if v.get("cat1_hits") else ""
            gc1.markdown(f"{star}**{v['name']}** · {v['score']}  \n"
                         f"<span class='rc-style'>{' · '.join(v.get('why', []))}</span>",
                         unsafe_allow_html=True)
            for lab, col, cid in (("→1", gc2, "1"), ("→2", gc3, "2"), ("→3", gc4, "3")):
                if col.button(lab, key=f"g_add_{cid}_{k}"):
                    cur = {c: list(cfg().get("artist_categories", {}).get(c, []))
                           for c in ("1", "2", "3")}
                    cur[cid].append(v["name"])
                    set_artist_categories(cur)
                    for c in ("1", "2", "3"):
                        st.session_state.pop(f"_artcat_shadow_{c}", None)
                    st.rerun()

        gl = sorted(_grr.get("labels", {}).values(),
                    key=lambda v: v.get("score", 0), reverse=True)
        base_keys_now = {normalize_label(x) for x in cfg().get("labels", [])}

        def _in_base(v):
            return v.get("in_base") or normalize_label(v["name"]) in base_keys_now

        n_new = sum(1 for v in gl if not _in_base(v))
        if gl:
            with st.expander(f"Labels candidats du graphe ({len(gl)} · {n_new} absents de ta base)"):
                st.markdown("**Ajout automatique à ma base**")
                st.caption("Critère : nombre de tes artistes-graines (liste manuelle + corpus + "
                           "collection) qui ont sorti un disque chez ce label.")
                la1, la2 = st.columns([2, 2])
                min_seeds = la1.slider("≥ N de mes artistes ont sorti chez ce label", 1, 6, 2,
                                       key="gl_auto_seeds")
                min_score = la1.slider("… et score du label ≥", 0.0, 25.0, 0.0, 0.5,
                                       key="gl_auto_score")
                need_cat1 = la2.checkbox("… dont au moins un en catégorie 1", value=False,
                                         key="gl_auto_cat1")
                targets = [v for v in gl if not _in_base(v)
                           and v.get("n_seeds", 0) >= min_seeds and v.get("score", 0) >= min_score
                           and (not need_cat1 or v.get("cat1_seeds", 0) >= 1)]
                if la2.button(f"➕ Ajouter {len(targets)} label(s) à ma base",
                              type="primary", disabled=not targets):
                    names = [v["name"] for v in targets]
                    set_labels(list(st.session_state.labels) + names)
                    res = st.session_state.resolved
                    for v in targets:
                        k = normalize_label(v["name"])
                        if k not in res or res[k].get("status") not in ("exact", "confirmed"):
                            res[k] = {"original": v["name"], "discogs_name": v["name"],
                                      "discogs_id": None, "status": "confirmed",
                                      "reviewed_by": "graph"}
                    save_resolved(res)
                    st.success(f"{len(names)} label(s) ajoutés à ta base "
                               "(marqués résolus). Pense à les profiler.")
                    st.rerun()
                if targets:
                    la2.caption("Ex. : " + ", ".join(v["name"] for v in targets[:12])
                                + (f" … +{len(targets) - 12}" if len(targets) > 12 else ""))

                st.divider()
                only_new = st.checkbox("Masquer ceux déjà en base", value=True, key="gl_only_new")
                for v in gl[:80]:
                    in_base = _in_base(v)
                    if only_new and in_base:
                        continue
                    lc1, lc2 = st.columns([5, 1])
                    tag = "✅ base" if in_base else "🆕"
                    star = "⭐" if v.get("cat1_seeds") else ""
                    lc1.write(f"{star}**{v['name']}** · score {v['score']} · "
                              f"{v.get('n_seeds', 0)} artiste(s) · {tag} — "
                              f"{', '.join(v.get('seeds', []))}")
                    if not v.get("in_watchlist") and lc2.button(
                            "+ veille", key=f"g_wl_{normalize_label(v['name'])}"):
                        add_watch(v["name"])
                        if not in_base:
                            set_labels(list(st.session_state.labels) + [v["name"]])
                        st.rerun()

# ---------------------------------------------------------------- Tab: Sources & reco

if _nav == "🎧 Sources & reco":
    st.write("Analyse ta **collection** et ta **wantlist** Discogs pour recommander des labels "
             "à suivre. Les IDs de labels viennent directement de Discogs — aucune résolution, "
             "coût ≈ `nb_disques / 100` appels.")

    ccache = st.session_state.collection
    if ccache.get("fetched_at"):
        st.caption(
            f"Dernier chargement : {ccache['fetched_at'].replace('T', ' ')} · "
            f"**{ccache.get('n_collection', 0)}** disques, **{ccache.get('n_wants', 0)}** wants · "
            f"{len(ccache.get('label_counts', {}))} labels distincts en collection."
        )
    else:
        st.info("Collection pas encore chargée.")

    add_to_base = st.checkbox("Ajouter les labels découverts à ma base", value=True)
    if not render_job("fetch_collection", "Collection Discogs") and not job_running("fetch_collection"):
        if st.button("Charger / rafraîchir ma collection + wantlist", type="primary",
                     disabled=not cfg().get("token")):
            job_launch("fetch_collection", {"merge_base": add_to_base})
            st.rerun()

    st.divider()
    st.subheader("🎼 Autres sources (YouTube, Bandcamp)")
    st.caption(f"Corpus de goût : **{len(st.session_state.corpus)}** entrée(s), "
               f"`{len(st.session_state.lookup_cache)}` lookups Discogs en cache. "
               "Chaque source produit des couples (artiste, titre) ; le label manquant est "
               "complété par un lookup Discogs.")
    deep_lookup = st.checkbox("Recherche Discogs approfondie (+ de labels trouvés, ~2× plus lent)",
                              value=True)

    yc1, yc2 = st.columns(2)
    with yc1:
        st.markdown("**YouTube** — playlists")
        yk = st.text_input("Clé API YouTube Data v3", value=cfg().get("youtube_api_key", ""),
                           type="password", key="yt_key_in")
        if yk != cfg().get("youtube_api_key", ""):
            cfg()["youtube_api_key"] = yk
            persist()
        pls = st.text_area("URLs de playlists (une par ligne)",
                           value=cfg().get("youtube_playlists", ""), height=90, key="yt_pls_in")
        if pls != cfg().get("youtube_playlists", ""):
            cfg()["youtube_playlists"] = pls
            persist()
        if not job_running("ingest_youtube") and st.button("Importer les playlists YouTube",
                                                           disabled=not yk):
            job_launch("ingest_youtube", {"deep": deep_lookup})
            st.rerun()

    with yc2:
        st.markdown("**Bandcamp** — collection (API Subsonic)")
        st.caption("Bandcamp → réglages → *Subsonic* : génère identifiant + mot de passe.")
        bsu = st.text_input("Identifiant Subsonic", value=cfg().get("bandcamp_sub_user", ""),
                            key="bc_su_in")
        if bsu != cfg().get("bandcamp_sub_user", ""):
            cfg()["bandcamp_sub_user"] = bsu
            persist()
        bsp = st.text_input("Mot de passe Subsonic", type="password",
                            value=cfg().get("bandcamp_sub_pass", ""), key="bc_sp_in")
        if bsp != cfg().get("bandcamp_sub_pass", ""):
            cfg()["bandcamp_sub_pass"] = bsp
            persist()
        if not job_running("ingest_bandcamp") and st.button("Importer ma collection Bandcamp",
                                                            disabled=not (bsu and bsp)):
            job_launch("ingest_bandcamp", {"deep": deep_lookup})
            st.rerun()

    render_job("ingest_youtube", "Import YouTube")
    render_job("ingest_bandcamp", "Import Bandcamp")

    st.divider()
    st.markdown("**🎧 DJ sets — tracks joués dans des podcasts / radios YouTube**")
    _have_dj = importlib.util.find_spec("yt_dlp") and importlib.util.find_spec("playwright")
    st.caption("Donne des DJs / émissions / chaînes. Le job liste leurs vidéos, ouvre chaque "
               "vidéo dans un navigateur headless et lit le **panneau « Musique »** (tracks "
               "identifiés par YouTube). Une ligne de corpus par couple track × DJ "
               "(`source=djset`, poids 0.4).")
    if not _have_dj:
        st.warning("Dépendances requises — dans un terminal :\n\n"
                   "`pip3 install yt-dlp playwright`\n\n`playwright install chromium`\n\n"
                   "puis recharge la page.")
    st.caption("Un **@handle** ou une **URL de chaîne** → toutes ses vidéos. Un **texte** → "
               "recherche YouTube (ajoute « set », « boiler room », « radio »… pour cibler "
               "des sets ; un nom d'artiste seul peut ne renvoyer que des clips).")
    djs = st.text_area("DJs / émissions / chaînes (une par ligne : nom, @handle ou URL de chaîne)",
                       value=cfg().get("djset_sources", ""), height=110, key="dj_src_in",
                       disabled=not _have_dj)
    if djs != cfg().get("djset_sources", ""):
        cfg()["djset_sources"] = djs
        persist()
    djc1, djc2 = st.columns(2)
    dj_max = djc1.slider("Vidéos max par source", 5, 60, 25, 5, key="dj_max",
                         disabled=not _have_dj)
    dj_minmin = djc2.slider("Durée minimum (min) — filtre les clips, garde les sets",
                            10, 90, 35, 5, key="dj_minmin", disabled=not _have_dj)
    dj_hint = st.checkbox("Exiger un mot-clé de set (set / mix / radio / boiler room / "
                          "podcast / session…) dans le **titre ou la description**",
                          value=True, key="dj_hint", disabled=not _have_dj)
    _dj_srcs = [s.strip() for s in djs.splitlines() if s.strip()]
    st.caption(f"≈ {len(_dj_srcs)} source(s) × {dj_max} vidéos × ~20 s (scraping + lookups "
               f"Discogs + pauses anti-blocage) → **≈ {max(1, round(len(_dj_srcs) * dj_max * 20 / 60))} min** "
               "au 1ᵉʳ passage. Reprenable (vidéos déjà traitées ignorées) et stoppable.")
    # raccourci : piocher dans MA liste d'artistes résolus (noms Discogs propres)
    _art_opts = sorted(
        {e["discogs_name"] for e in st.session_state.get("artists_resolved", {}).values()
         if e.get("discogs_name") and e.get("status") in ("exact", "approx", "confirmed")},
        key=lambda n: n.lower())
    dj_from_art = st.multiselect(
        f"Ou : chercher les sets d'artistes de ma liste ({len(_art_opts)} artistes résolus)",
        _art_opts, key="dj_from_art", disabled=not _have_dj)

    _seen_path = os.path.join(_HERE, "djset_seen.json")
    _n_seen = len(load_json(_seen_path, []))
    _dj_final = _dj_srcs + [a for a in dj_from_art if a not in _dj_srcs]

    def _launch_djsets():
        os.makedirs(JOBS_DIR, exist_ok=True)
        save_json(os.path.join(JOBS_DIR, "djsets.input.json"),
                  {"sources": _dj_final, "max_per_source": int(dj_max),
                   "min_minutes": int(dj_minmin), "require_hint": bool(dj_hint),
                   "deep": deep_lookup})
        job_launch("ingest_djsets", {})

    djb1, djb2 = st.columns([2, 2])
    if _have_dj and not job_running("ingest_djsets") and djb1.button(
            f"Importer les sets ({len(_dj_final)} source·s)", type="primary",
            disabled=not (_dj_final and cfg().get("token"))):
        _launch_djsets()
        st.rerun()
    if _n_seen and djb2.button(f"Réinitialiser l'historique ({_n_seen} vidéos vues)"):
        save_json(_seen_path, [])
        st.rerun()
    render_job("ingest_djsets", "Import DJ sets")

    st.divider()
    st.caption("La **consolidation** (labels du corpus → base) et l'**enrichissement** "
               "(résolution + profilage des ajouts) tournent désormais en arrière-plan, "
               "automatiquement. Profilage en masse de l'existant : onglet **🎯 Profilage**.")
    render_job("merge_corpus", "Consolidation corpus → base")
    render_job("enrich", "Enrichissement auto")

    with st.expander("Réglages"):
        st.caption("Les poids de la recommandation (collection / corpus / artiste / affinité / "
                   "want_factor) sont dans l'onglet **🎛️ Réglages**.")
        if st.session_state.corpus and st.button("🗑️ Vider le corpus de goût"):
            st.session_state.corpus = []
            save_json(CORPUS_PATH, [])
            st.rerun()

    st.divider()
    rows = reco_rows()
    if not rows:
        st.info("Charge ta collection pour voir des recommandations de labels.")
    else:
        hc1, hc2, hc3 = st.columns(3)
        hide_watched = hc1.checkbox("Masquer les labels déjà en veille", value=False)
        hide_base = hc2.checkbox("Masquer les labels déjà en base", value=True)
        topn = hc3.slider("Affichés", 10, 200, 40, 10)
        shown = [r for r in rows
                 if not (hide_watched and r["watched"]) and not (hide_base and r.get("in_base"))]
        n_prof = sum(1 for r in shown if r["aff"] is not None)
        st.caption(f"{len(shown)} label(s) — top {min(topn, len(shown))} affichés · "
                   f"{n_prof} profilé(s).")

        # --- ajout automatique au-dessus d'un seuil ---
        st.markdown("**Ajout automatique au-dessus d'un seuil**")
        auto_max = max((r["score"] for r in rows), default=100)
        ac1, ac2 = st.columns([3, 2])
        auto_thr = ac1.slider("Score minimum", 0, int(auto_max), min(50, int(auto_max)), 1,
                              key="reco_auto_thr")
        min_art = ac1.slider("… et ≥ N artistes que j'aime sortis là", 0, 6, 0, key="reco_auto_art")
        need_aff = ac1.checkbox("Exiger un label profilé (affinité connue)", value=True,
                                key="reco_auto_aff")
        dest = ac2.radio("Ajouter à", ["Ma base de labels", "La veille", "Les deux"],
                         key="reco_auto_dest")
        cand = [r for r in rows if r["score"] >= auto_thr and r.get("artists", 0) >= min_art
                and (not need_aff or r["aff"] is not None)]
        base_t = [r for r in cand if not r.get("in_base")]
        watch_t = [r for r in cand if not r["watched"]]
        summary = {"Ma base de labels": f"{len(base_t)} → base",
                   "La veille": f"{len(watch_t)} → veille",
                   "Les deux": f"{len(base_t)} → base · {len(watch_t)} → veille"}[dest]
        if ac2.button(f"➕ Ajouter ({summary})", type="primary",
                      disabled=not (base_t or watch_t)):
            nb = nw = 0
            if dest in ("Ma base de labels", "Les deux"):
                nb = sum(1 for r in base_t if add_label_to_base(r["name"]))
            if dest in ("La veille", "Les deux"):
                nw = sum(1 for r in watch_t if add_watch(r["name"]))
            st.success(f"+{nb} dans la base · +{nw} en veille.")
            st.rerun()
        if cand:
            st.caption("Concernés : " + ", ".join(r["name"] for r in cand[:20])
                       + (f" … +{len(cand) - 20}" if len(cand) > 20 else ""))

        wl_only = [r for r in rows if r["want"] and not r["owned"] and not r["watched"]]
        if wl_only:
            with st.expander(f"🎯 Raccourci : {len(wl_only)} label(s) de ta wantlist, pas en veille"):
                for r in wl_only[:60]:
                    q1, q2 = st.columns([5, 1])
                    q1.write(f"**{r['name']}** · {r['want']} en wantlist")
                    if q2.button("+ veille", key=f"reco_wlq_{r['key']}"):
                        add_watch(r["name"])
                        log_feedback("label", r["key"], r["name"], "up", r["score"], r["feat"])
                        st.rerun()

        _dismissed = st.session_state.setdefault("_reco_dismissed", set())
        for r in shown[:topn]:
            if r["key"] in _dismissed:
                continue
            cc1, cc2, cc3, cc4 = st.columns([6, 1, 1, 1])
            bits = []
            if r["owned"]:
                bits.append(f"{r['owned']} possédé(s)")
            if r["want"]:
                bits.append(f"{r['want']} en wantlist")
            if r.get("corpus"):
                bits.append(f"corpus {r['corpus']}")
            if r.get("artists"):
                bits.append(f"⭐ {r['artists']} artiste(s) aimé(s) ici")
            bits.append(f"affinité {r['aff']}%" if r["aff"] is not None else "non profilé")
            tag = " · ✅ base" if r.get("in_base") else " · 🆕"
            cc1.markdown(f"**{r['name']}** · score {r['score']}{tag}  \n"
                         f"<span class='rc-style'>{', '.join(bits)}</span>",
                         unsafe_allow_html=True)
            if r.get("in_base"):
                cc2.caption("en base")
            elif cc2.button("+ base", key=f"reco_base_{r['key']}"):
                add_label_to_base(r["name"])
                log_feedback("label", r["key"], r["name"], "up", r["score"], r["feat"])
                st.rerun()
            if r["watched"]:
                cc3.caption("en veille")
            elif cc3.button("+ veille", key=f"reco_wl_{r['key']}"):
                add_watch(r["name"])
                log_feedback("label", r["key"], r["name"], "up", r["score"], r["feat"])
                st.rerun()
            if cc4.button("👎", key=f"reco_no_{r['key']}", help="Pas pour moi — masque et sert à l'apprentissage"):
                log_feedback("label", r["key"], r["name"], "down", r["score"], r["feat"])
                _dismissed.add(r["key"])
                st.rerun()

# ---------------------------------------------------------------- Tab: Profilage des labels

if _nav == "🎯 Profilage":
    st.write(
        "Analyse les styles des sorties de chaque label de ta base pour lui donner un **score "
        "d'affinité** avec tes goûts. Ensuite, l'onglet Recherche peut ne cibler que les labels "
        "au-dessus d'un seuil, au lieu de balayer toute la base."
    )

    st.markdown("**Mes 3 catégories de styles** — le score d'affinité pondère chaque tag de style "
                "d'un label selon la catégorie où tu l'as rangé. Les **poids** des rangs et le "
                "**seuil d'affinité global** sont dans l'onglet **🎛️ Réglages**.")
    cats_cfg = cfg().get("taste_categories", DEFAULT_TASTE_CATEGORIES)
    _tw = scoring()["taste_tiers"]
    tc1, tc2, tc3 = st.columns(3)
    new_cats = {}
    for cid, col in (("1", tc1), ("2", tc2), ("3", tc3)):
        with col:
            st.caption(f"Catégorie {cid} — {CAT_LABELS[cid]} · poids {_tw[cid]}")
            raw = st.text_area(f"Styles cat. {cid}", value="\n".join(cats_cfg.get(cid, [])),
                               height=150, key=f"taste_cat_{cid}", label_visibility="collapsed")
            new_cats[cid] = [s.strip() for s in re.split(r"[\n,]", raw) if s.strip()]
    if new_cats != cats_cfg:
        cfg()["taste_categories"] = new_cats
        persist()
    wmap = taste_weight_map()
    n_styles = sum(len(v) for v in new_cats.values())
    st.caption(f"{n_styles} style(s) répartis. Score recalculé instantanément — pas besoin de "
               "re-profiler.")

    prof = st.session_state.profile
    labels_all = st.session_state.labels
    done_keys = {normalize_label(l) for l in labels_all if normalize_label(l) in prof}
    st.progress(len(done_keys) / len(labels_all) if labels_all else 0,
                text=f"{len(done_keys)}/{len(labels_all)} labels profilés")
    _floor_v = int(scoring()["label_affinity_floor"] or 0)
    if _floor_v:
        _below = sum(1 for k in done_keys if (affinity_score(prof.get(k), wmap) or 0) < _floor_v)
        st.caption(f"Seuil d'affinité global **{_floor_v}%** (→ 🎛️ Réglages) : "
                   f"**{len(done_keys) - _below}** label(s) actifs · {_below} sous le seuil · "
                   f"{len(labels_all) - len(done_keys)} non profilé(s) inactifs.")

    st.divider()
    n_missing = len(labels_all) - len(done_keys)
    st.caption("Les nouveaux labels sont profilés automatiquement à l'ajout. Ci-dessous : "
               "rattrapage en masse de l'existant (usage ponctuel).")
    _run_prof = render_job("profile_labels", "Profilage des labels")
    with st.expander(f"🛠 Profilage en masse — {n_missing} label(s) restant(s)"):
        st.markdown("1 appel API/label (~1,1 s). Priorité : collection / wantlist / corpus "
                    "d'abord, puis le reste. Tâche de fond, reprenable.")
        if not _run_prof and not job_running("profile_labels"):
            pc1, pc2 = st.columns([2, 1])
            chunk_n = pc1.number_input("Labels dans cette tranche", 50, 8000, 500, 50,
                                       key="prof_chunk_n")
            if pc2.button("Profiler la tranche suivante", type="primary",
                          disabled=not cfg().get("token") or n_missing == 0):
                job_launch("profile_labels", {"limit": int(chunk_n)})
                st.rerun()

    st.divider()
    st.markdown("**Résultats** — labels classés par affinité pondérée. "
                f"Colonnes C1/C2/C3 = part des styles dans chaque catégorie "
                f"({CAT_LABELS['1']} / {CAT_LABELS['2']} / {CAT_LABELS['3']}).")
    rows = []
    for lab in labels_all:
        e = prof.get(normalize_label(lab))
        if not e:
            continue
        sc = e.get("style_counts") or {}
        top = ", ".join(f"{s} ({n})" for s, n in
                        sorted(sc.items(), key=lambda kv: kv[1], reverse=True)[:4])
        shares = category_shares(e)
        rows.append({
            "Label": lab,
            "Affinité %": affinity_score(e, wmap),
            "C1 %": shares["1"],
            "C2 %": shares["2"],
            "C3 %": shares["3"],
            "Échantillon": e.get("sampled", 0),
            "Catalogue": e.get("total_items", ""),
            "Styles dominants": top,
        })
    if not rows:
        st.info("Aucun label profilé pour l'instant. Lance le profilage ci-dessus.")
    else:
        rows.sort(key=lambda r: r["Affinité %"], reverse=True)
        min_show = st.slider("N'afficher que les labels ≥ affinité", 0, 100, 0, 5)
        shown_rows = [r for r in rows if r["Affinité %"] >= min_show]
        st.caption(f"{len(shown_rows)} label(s) affiché(s) sur {len(rows)} profilé(s).")
        st.dataframe(shown_rows, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------- Tab: Ma base de labels

if _nav == "🏷️ Ma base":
    st.write(
        "Ta base de labels sert de filtre pour la recherche et la veille. Alimente-la par import "
        "CSV (fusion ou remplacement) et ajuste-la à la main ici — tout est sauvegardé dans "
        "`crate_radar_config.json`."
    )

    st.subheader("Importer un CSV")
    up = st.file_uploader("CSV — 1re colonne = nom du label, 1re ligne ignorée (en-tête)", type=["csv"])
    replace_all = st.checkbox("Remplacer entièrement la base (sinon : fusion avec l'existant)")
    if up is not None and st.button("Importer", type="primary"):
        content = up.read().decode("utf-8", errors="ignore")
        lines = [l for l in content.splitlines() if l.strip()]
        new_names = [line.split(",")[0].strip().strip('"') for line in lines[1:]]
        new_names = [n for n in new_names if n]
        before = len({normalize_label(l) for l in st.session_state.labels})
        if replace_all:
            set_labels(new_names)
            st.success(f"Base remplacée — {len(st.session_state.labels)} label(s).")
        else:
            set_labels(list(st.session_state.labels) + new_names)
            st.success(f"+{len(st.session_state.labels) - before} nouveau(x) — "
                       f"{len(st.session_state.labels)} au total.")

    st.divider()
    st.subheader("Ajouter un label")
    ac1, ac2 = st.columns([4, 1])
    new_label = ac1.text_input("Nom du label", key="new_label_input",
                               label_visibility="collapsed", placeholder="Nom du label")
    if ac2.button("Ajouter"):
        if not new_label.strip():
            st.warning("Saisis un nom.")
        elif normalize_label(new_label) in {normalize_label(l) for l in st.session_state.labels}:
            st.warning(f"« {new_label.strip()} » est déjà dans la base.")
        else:
            set_labels(list(st.session_state.labels) + [new_label])
            st.rerun()

    st.divider()
    st.subheader(f"Base actuelle ({len(st.session_state.labels)})")
    if not st.session_state.labels:
        st.info("Base vide — importe un CSV ou ajoute des labels ci-dessus.")
    else:
        flt = st.text_input("Filtrer", key="label_manage_filter", placeholder="Tape pour filtrer…")
        shown = ([l for l in st.session_state.labels if flt.lower() in l.lower()]
                 if flt else list(st.session_state.labels))
        st.caption(f"{len(shown)} affiché(s)" + ("" if not flt else f" sur {len(st.session_state.labels)}"))
        for lab in shown[:100]:
            rc1, rc2 = st.columns([5, 1])
            rc1.write(lab)
            if rc2.button("Retirer", key=f"rm_label_{lab}"):
                set_labels([l for l in st.session_state.labels if l != lab])
                st.rerun()
        if len(shown) > 100:
            st.caption(f"… {len(shown) - 100} autre(s) masqué(s), affine le filtre.")

# ---------------------------------------------------------------- Tab: Recherche

if _nav == "🔍 Recherche":
    st.write("Choisis un label pour une recherche ciblée, ou **laisse vide** : l'appli interroge "
             "alors Discogs une fois par label de ta base et remonte leurs sorties qui collent à "
             "tes filtres genre / style / année.")

    # --- reprise d'une recherche de l'historique : pré-remplit les widgets avant leur création ---
    _rl = st.session_state.pop("_relaunch", None)
    if _rl:
        st.session_state["f_labelfilter"] = _rl.get("picked_label") or _rl.get("label_filter", "")
        st.session_state["f_picked"] = _rl.get("picked_label", "")
        st.session_state["f_genre"] = _rl.get("genre", "")
        st.session_state["f_style"] = _rl.get("style", "")
        st.session_state["f_fmt"] = _rl.get("fmt", "Vinyl")
        st.session_state["f_year_from"] = _rl.get("year_from", "")
        st.session_state["f_year_to"] = _rl.get("year_to", "")
        if _rl.get("aff_min") is not None:
            st.session_state["f_aff_min"] = int(_rl["aff_min"])
        st.session_state["_autorun"] = True

    label_filter_text = st.text_input(
        "Filtrer les labels de ta base (restreint la liste ci-dessous, et — sans label choisi — "
        "le périmètre du scan)", key="f_labelfilter")
    scope_labels = ([l for l in st.session_state.labels if label_filter_text.lower() in l.lower()]
                    if label_filter_text else list(st.session_state.labels))
    label_options = [""] + scope_labels[:200]
    if st.session_state.get("f_picked") not in label_options:
        st.session_state["f_picked"] = ""
    picked_label = st.selectbox(
        f"Label ({len(st.session_state.labels)} en base, {len(label_options)-1} affichés)",
        label_options, key="f_picked",
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        genre = st.selectbox("Genre", GENRES, format_func=lambda g: g or "Tous", key="f_genre")
    with c2:
        style = st.selectbox("Style", STYLES, format_func=lambda s: s or "Tous", key="f_style")
    with c3:
        fmt = st.selectbox("Format", ["Vinyl", "", "CD"],
                           format_func=lambda f: {"Vinyl": "Vinyle", "": "Tous", "CD": "CD"}[f],
                           key="f_fmt")
    with c4:
        year_from = st.text_input("De l'année", key="f_year_from")
    with c5:
        year_to = st.text_input("À l'année", key="f_year_to")

    pages_per_label = 3
    if not picked_label:
        pages_per_label = st.slider(
            "Pages Discogs par label (100 sorties/page)", min_value=1, max_value=6, value=3,
            help="L'intervalle d'années est envoyé directement à l'API (year=2005-2014), donc "
                 "chaque label revient déjà filtré : 1 à 3 pages suffisent pour la quasi-totalité "
                 "des labels, même prolifiques."
        )

        # --- cibler les labels selon leur affinité (voir onglet 🎯 Profilage) ---
        _prof = st.session_state.profile
        _wmap = taste_weight_map()
        _perimeter = list(scope_labels)
        _profiled = [l for l in _perimeter if normalize_label(l) in _prof]
        _unprofiled = [l for l in _perimeter if normalize_label(l) not in _prof]
        if _profiled:
            _floor = int(scoring()["label_affinity_floor"] or 0)
            aff_min = st.slider("Affinité minimale des labels à scanner", 0, 100,
                                max(50, _floor), 5, key="f_aff_min",
                                help="Score calculé dans l'onglet 🎯 Profilage. Plancher = seuil "
                                     f"global ({_floor}%)." if _floor else
                                     "Score calculé dans l'onglet 🎯 Profilage.")
            keep_unprofiled = st.checkbox(
                f"Inclure aussi les {len(_unprofiled)} label(s) non profilé(s) du périmètre")
            ranked = sorted(((l, affinity_score(_prof.get(normalize_label(l)), _wmap))
                             for l in _profiled), key=lambda t: t[1], reverse=True)
            scope_labels = [l for l, s in ranked if s >= aff_min]
            if keep_unprofiled:
                scope_labels += _unprofiled
            st.caption(
                f"Périmètre : {len(_perimeter)} label(s), dont {len(_profiled)} profilé(s). "
                f"Retenus pour le scan : **{len(scope_labels)}** "
                f"(affinité ≥ {aff_min}%{', + non profilés' if keep_unprofiled else ''}), "
                "classés par affinité décroissante."
            )
        else:
            st.caption("Aucun label du périmètre n'est encore profilé — va dans l'onglet 🎯 Profilage "
                       "pour noter ta base et ne scanner que les labels proches de tes goûts.")

        _yr = year_param(year_from, year_to)
        est_min = max(1, round(len(scope_labels) * 1.3 * 1.1 / 60))
        st.caption(
            f"Le scan interrogera **{len(scope_labels)}** label(s)"
            + (f" (filtre « {label_filter_text} »)" if label_filter_text else " (toute la base)")
            + (f", années **{_yr}**" if _yr else ", **toutes années**")
            + f" — ~{len(scope_labels)} à {len(scope_labels) * pages_per_label} appels API, "
              f"≈ {est_min} min. Bouton « Arrêter » disponible pendant le scan."
        )

    col_a, col_b = st.columns([1, 3])
    with col_a:
        do_search = st.button("Rechercher", type="primary",
                              disabled=st.session_state.search_running)
    with col_b:
        if picked_label and st.button(f"+ Ajouter « {picked_label} » à la veille"):
            if add_watch(picked_label):
                st.success("Ajouté à la liste de veille.")
            else:
                st.caption("Déjà en veille.")

    do_search = do_search or st.session_state.pop("_autorun", False)

    # --- historique des recherches ---
    if st.session_state.history:
        with st.expander(f"🕓 Recherches précédentes ({len(st.session_state.history)})"):
            hc1, hc2 = st.columns([6, 1])
            hc2.button("Tout effacer", key="hist_clear",
                       on_click=lambda: (st.session_state.history.clear(), save_history([])))
            for hi, h in enumerate(st.session_state.history):
                r1, r2, r3 = st.columns([7, 1, 1])
                r1.write(f"`{h.get('at', '')[:16].replace('T', ' ')}` — {history_label(h)}")
                if r2.button("Relancer", key=f"hist_run_{hi}"):
                    st.session_state["_relaunch"] = h
                    st.rerun()
                if r3.button("✕", key=f"hist_del_{hi}"):
                    st.session_state.history.pop(hi)
                    save_history(st.session_state.history)
                    st.rerun()

    _hist_entry = {
        "picked_label": picked_label, "genre": genre, "style": style, "fmt": fmt,
        "year_from": year_from, "year_to": year_to, "label_filter": label_filter_text,
        "aff_min": (st.session_state.get("f_aff_min") if not picked_label else None),
    }

    if do_search:
        if not cfg().get("token"):
            st.error("Renseigne d'abord ton token Discogs.")
        elif picked_label:
            # --- Cas 1 : un label précis -> requête unique avec year=intervalle ---
            canonical = get_canonical(picked_label)
            yr = year_param(year_from, year_to)
            errors = []
            with st.spinner("Recherche en cours…"):
                try:
                    found = search_label_releases(canonical, genre, style, fmt, year=yr, max_pages=6)
                    if not found:
                        # Filet : le nom canonique ne matche rien -> recherche libre
                        # puis filtrage sur le nom normalisé.
                        d2 = discogs_search(q=canonical, genre=genre, style=style, format=fmt,
                                            year=yr, per_page=100, sort="year", sort_order="desc")
                        target = normalize_label(canonical)
                        found = [r for r in d2.get("results", [])
                                 if any(normalize_label(l) == target for l in r.get("label", []))]
                except Exception as e:
                    found, errors = [], [str(e)]
            found.sort(key=lambda r: int(r.get("year") or 0), reverse=True)
            st.session_state.results = found
            st.session_state.search_errors = []
            if not errors:
                add_history({**_hist_entry, "n_results": len(found)})
            for e in errors:
                st.error(e)
        elif not scope_labels:
            st.error("Ta base de labels est vide (ou le filtre ne correspond à aucun label).")
        else:
            # --- Cas 2 : pas de label -> job de scan en arrière-plan ---
            os.makedirs(JOBS_DIR, exist_ok=True)
            save_json(SEARCH_INPUT_PATH, {
                "scope": list(scope_labels), "genre": genre, "style": style, "fmt": fmt,
                "year": year_param(year_from, year_to),
                "year_from": year_from, "year_to": year_to, "pages": pages_per_label,
            })
            st.session_state["_search_hist"] = _hist_entry
            job_launch("search_base", {})
            st.rerun()

    # ---- scan « toute la base » en job d'arrière-plan ----
    _sb = job_status("search_base")
    if _sb and not _sb.get("running") and _sb.get("finished_at") and not _sb.get("error"):
        if st.session_state.get("_search_base_seen") != _sb["finished_at"]:
            st.session_state.results = load_json(SEARCH_RESULTS_PATH, [])
            st.session_state["_search_base_seen"] = _sb["finished_at"]
            _h = st.session_state.pop("_search_hist", None)
            if _h:
                add_history({**_h, "n_results": len(st.session_state.results)})
    render_job("search_base", "Scan de la base")

    st.subheader(f"Résultats ({len(st.session_state.results)})")
    if not st.session_state.results:
        st.info("Aucune recherche lancée, ou aucun résultat pour ces filtres.")
    else:
        def _score_results():
            r_idx = reco_index()
            _asc_map = {k: v[0] for k, v in artist_scores().items()}
            return [(r, *album_score(r, r_idx, _asc_map)) for r in st.session_state.results]

        _res = st.session_state.results
        _res_sig = (len(_res), _res[0].get("id") if _res else None,
                    _res[-1].get("id") if _res else None,
                    _sig_reco(), _sig_artist_scores())
        scored = _memo("search_scored", _res_sig, _score_results)

        sc1, sc2, sc3 = st.columns([2, 2, 3])
        sort_by = sc1.radio("Trier par", ["Score album", "Année"], horizontal=True,
                            key="res_sort")
        min_alb = sc2.slider("Score album minimum", 0, 100, 0, 5, key="res_min_alb")
        sc3.caption("Poids du score album → onglet **🎛️ Réglages**")

        view = [t for t in scored if (t[1] or 0) >= min_alb]
        if not view and scored:
            st.warning(f"{len(scored)} sortie(s) trouvée(s), mais toutes sous le seuil "
                       f"« Score album minimum » ({min_alb}) — je les affiche quand même. "
                       "Baisse le curseur pour retirer ce message.")
            view = list(scored)
        if sort_by == "Score album":
            view.sort(key=lambda t: (t[1] is None, -(t[1] or 0)))
        else:
            view.sort(key=lambda t: -int(t[0].get("year") or 0))
        cap = sc1.slider("Cartes affichées", 20, 400, 60, 20, key="res_cap")
        st.caption(f"{min(cap, len(view))} carte(s) affichée(s) sur {len(view)} retenues "
                   f"({len(scored)} sorties trouvées).")

        cols = st.columns(4)
        for i, (r, asc, adet) in enumerate(view[:cap]):
            with cols[i % 4]:
                catno = r.get("catno", "")
                year = r.get("year", "—")
                year_label = f"{catno} · {year}" if catno else str(year)
                raw_title = r.get("title") or ""
                artist, sep, album = raw_title.partition(" - ")
                lab_name, aff = release_label_affinity(r)
                _fbk = normalize_label(raw_title) or f"rid:{r.get('id')}"
                _fb = ({"kind": "album", "key": _fbk, "name": raw_title[:90], "score": asc,
                        "feat": {k: (adet.get(k) or 0) / 100 for k in ALBUM_FEAT_KEYS}}
                       if asc is not None else None)
                render_release_card(
                    image_url=r.get("thumb") or r.get("cover_image"),
                    title=(album if sep else raw_title),
                    artist=(artist if sep else None),
                    label_text=lab_name or ", ".join(r.get("label", [])),
                    affinity=aff,
                    styles=", ".join(r.get("style", [])),
                    year_text=year_label,
                    release_id=r.get("id"),
                    key_prefix=f"search_{i}",
                    discogs_url=f"https://www.discogs.com{r.get('uri')}" if r.get("uri") else None,
                    image_width=140, album=asc, album_detail=adet, feedback=_fb,
                )

# ---------------------------------------------------------------- Tab: Liste de veille

if _nav == "📻 Liste de veille":
    st.write("Les labels ajoutés depuis l'onglet Recherche apparaissent ici. Lance un scan groupé pour voir leurs sorties récentes en une fois.")

    # dédoublonnage silencieux d'un historique éventuellement pollué
    _wl_seen, _wl_clean = set(), []
    for w in cfg().get("watchlist", []):
        k = normalize_label(w)
        if k and k not in _wl_seen:
            _wl_seen.add(k)
            _wl_clean.append(w)
    if _wl_clean != cfg().get("watchlist", []):
        cfg()["watchlist"] = _wl_clean
        persist()

    if not cfg()["watchlist"]:
        st.info('Ta liste de veille est vide. Va dans "Recherche", choisis un label, et clique "+ Ajouter à la veille".')
    else:
        for i, w in enumerate(list(cfg()["watchlist"])):
            c1, c2 = st.columns([5, 1])
            c1.write(w)
            if c2.button("Retirer", key=f"rm_wl_{i}_{w}"):
                cfg()["watchlist"] = [x for x in cfg()["watchlist"] if x != w]
                persist()
                st.rerun()

    wl_year_from = st.text_input("Depuis l'année", str(CURRENT_YEAR - 1))
    if st.button("Lancer la veille", type="primary", disabled=not cfg()["watchlist"]):
        if not cfg().get("token"):
            st.error("Renseigne d'abord ton token Discogs.")
        else:
            progress = st.progress(0, text="Scan en cours...")
            combined = []
            errors = []
            wl = cfg()["watchlist"]
            for i, lab in enumerate(wl):
                try:
                    data = discogs_search(label=get_canonical(lab), format="Vinyl", year=wl_year_from, per_page=20)
                    for r in data.get("results", []):
                        r["_watched_label"] = lab
                        combined.append(r)
                except Exception as e:
                    errors.append(f'Erreur sur "{lab}": {e}')
                progress.progress(int((i + 1) / len(wl) * 100))
                if i < len(wl) - 1:
                    time.sleep(1.1)
            progress.empty()
            combined.sort(key=lambda r: int(r.get("year") or 0), reverse=True)
            st.session_state.wl_results = combined
            for e in errors:
                st.error(e)

    st.caption("Un scan interroge l'API une fois par label avec une pause d'1,1s entre chaque appel, pour rester dans la limite de Discogs (60 requêtes/min).")

    st.subheader(f"Nouveautés détectées ({len(st.session_state.wl_results)})")
    if not st.session_state.wl_results:
        st.info("Lance un scan pour voir apparaître les sorties récentes de tes labels suivis.")
    else:
        cols = st.columns(4)
        for i, r in enumerate(st.session_state.wl_results):
            with cols[i % 4]:
                render_release_card(
                    image_url=r.get("thumb") or r.get("cover_image"),
                    title=r.get("title"),
                    label_text=", ".join(r.get("label", [])),
                    year_text=r.get("year", "—"),
                    release_id=r.get("id"),
                    key_prefix=f"watch_{i}",
                    discogs_url=f"https://www.discogs.com{r.get('uri')}" if r.get("uri") else None,
                    extra_caption=f"via {r.get('_watched_label', '')}",
                )

# ---------------------------------------------------------------- Tab: Mes vendeurs

if _nav == "🏪 Mes vendeurs":
    st.write(
        "Suis des vendeurs Discogs et scanne leur inventaire en vente. "
        "Ajoute-les à la main, ou tente l'import depuis ton historique de commandes "
        "(⚠️ l'API `/marketplace/orders` ne renvoie que les commandes où **tu es vendeur** — "
        "si tu n'as jamais vendu, elle est vide ; dans ce cas, saisie manuelle)."
    )
    _nnew = len(load_json(SELLERS_NEW_PATH, []))
    if _nnew:
        st.success(f"🆕 **{_nnew}** nouvelle(s) annonce(s) en attente chez tes vendeurs "
                   "— section « Nouveautés » plus bas.")

    _sc1, _sc2 = st.columns([4, 1])
    _new_sellers = _sc1.text_input(
        "Ajouter des vendeurs (identifiants Discogs, séparés par virgule ou espace)",
        key="seller_add_in", placeholder="ex. hardwax, redeye_records, …")
    if _sc2.button("Ajouter", key="seller_add_btn") and _new_sellers.strip():
        cur = set(cfg().get("sellers", []))
        for name in re.split(r"[,\s]+", _new_sellers.strip()):
            name = name.strip().strip("@/")
            if name.startswith("http"):
                m = re.search(r"/seller/([^/]+)", name)
                name = m.group(1) if m else ""
            if name:
                cur.add(name)
        cfg()["sellers"] = sorted(cur)
        persist()
        st.rerun()

    with st.expander("Importer depuis mon historique de commandes Discogs"):
        if st.button("Charger mes vendeurs depuis mes commandes"):
            if not cfg().get("token"):
                st.error("Renseigne d'abord ton token Discogs.")
            else:
                found = {}
                page = 1
                pages = 1
                progress = st.progress(0, text="Chargement des commandes...")
                try:
                    while page <= pages and page <= 10:
                        data = discogs_get("/marketplace/orders",
                                            {"page": page, "per_page": 50, "sort": "created", "sort_order": "desc"})
                        for o in data.get("orders", []):
                            seller = o.get("seller", {})
                            uname = seller.get("username")
                            if uname and uname not in found:
                                found[uname] = uname
                        pages = data.get("pagination", {}).get("pages", 1)
                        progress.progress(min(int(page / max(pages, 1) * 100), 100))
                        page += 1
                        if page <= pages:
                            time.sleep(1.1)
                    progress.empty()
                    cfg()["sellers"] = sorted(set(cfg().get("sellers", [])) | set(found.values()))
                    persist()
                    if not found:
                        st.warning("Aucun vendeur trouvé. L'API ne renvoie que les commandes où "
                                   "tu es **vendeur** — utilise la saisie manuelle ci-dessus.")
                    else:
                        st.success(f"{len(found)} vendeur(s) importé(s).")
                except Exception as e:
                    progress.empty()
                    st.error(f"Impossible de charger tes commandes : {e}")

    if not cfg().get("sellers"):
        st.info("Ajoute au moins un vendeur ci-dessus pour activer le suivi des nouveautés.")
    else:
        with st.expander(f"Vendeurs suivis ({len(cfg()['sellers'])})"):
            _seen_meta = load_json(SELLERS_SEEN_PATH, {})
            for s in list(cfg()["sellers"]):
                c1, c2 = st.columns([5, 1])
                _ls = (_seen_meta.get(s) or {}).get("last_scan", "")
                c1.markdown(f"[{s}](https://www.discogs.com/seller/{s}/profile)"
                            + (f" · scanné {_ls.replace('T', ' ')}" if _ls else " · jamais scanné"))
                if c2.button("Retirer", key=f"rm_seller_{s}"):
                    cfg()["sellers"].remove(s)
                    persist()
                    st.rerun()

        st.markdown("**🆕 Nouveautés qui correspondent à tes goûts**")
        st.caption("Le scan compare l'inventaire « en vente » de chaque vendeur à son état au "
                   "dernier passage et note chaque **nouvelle annonce** avec le score album "
                   "(label × artistes × styles). Le 1ᵉʳ scan d'un vendeur sert de référence.")

        # auto-scan si le dernier passage remonte à > 24 h
        _seen_meta = load_json(SELLERS_SEEN_PATH, {})
        _last = max((v.get("last_scan", "") for v in _seen_meta.values()), default="")
        _stale = (not _last) or (datetime.now() - datetime.fromisoformat(_last)).total_seconds() > 86400
        if _stale and cfg().get("token") and not job_running("scan_sellers") \
                and not st.session_state.get("_sellers_autoscan"):
            st.session_state["_sellers_autoscan"] = True
            job_launch("scan_sellers", {})
            st.rerun()

        scb1, scb2 = st.columns([1, 3])
        if scb1.button("🔄 Scanner maintenant", type="primary",
                       disabled=not cfg().get("token") or job_running("scan_sellers")):
            job_launch("scan_sellers", {})
            st.rerun()
        scb2.caption(f"Dernier scan : {_last.replace('T', ' ') if _last else '—'}")
        render_job("scan_sellers", "Scan des vendeurs")

        new_items = load_json(SELLERS_NEW_PATH, [])
        if not new_items:
            st.info("Rien de nouveau pour l'instant.")
        else:
            _ridx = reco_index()
            _asc = {k: v[0] for k, v in artist_scores().items()}

            def _sell_score(it):
                pseudo = {"label": [it["label"]] if it.get("label") else [],
                          "title": f"{it.get('artist', '')} - {it.get('title', '')}",
                          "style": it.get("style") or []}
                return album_score(pseudo, _ridx, _asc)

            scored = sorted(((it, *_sell_score(it)) for it in new_items),
                            key=lambda t: (t[1] is None, -(t[1] or 0)))
            fc1, fc2, fc3 = st.columns(3)
            mins = fc1.slider("Score album minimum", 0, 100, 30, 5, key="sell_new_min")
            sf = fc2.multiselect("Vendeurs", sorted({it["seller"] for it in new_items}),
                                 key="sell_new_sf")
            show = [(it, sc, det) for it, sc, det in scored
                    if (sc or 0) >= mins and (not sf or it["seller"] in sf)]
            fc3.metric("À voir", len(show))
            bc1, bc2 = st.columns([1, 3])
            if bc1.button("✓ Tout marquer comme vu"):
                save_json(SELLERS_NEW_PATH, [])
                st.rerun()
            bc2.caption(f"{len(new_items)} nouvelle(s) annonce(s) en attente au total.")

            _fbdone = st.session_state.setdefault("_fb_done", {})
            for it, sc, det in show[:150]:
                lid = it.get("listing_id")
                cc1, cc2, cc3, cc4 = st.columns([8, 1, 1, 1])
                badge = (f"<span class='album-badge'>🎯 {sc}</span> " if sc is not None
                         else "<span class='rc-style'>—</span> ")
                price = (f" · {it['price']} {it.get('currency', '')}" if it.get("price") else "")
                cond = f" · {it['condition']}" if it.get("condition") else ""
                sty = f" · _{', '.join(it.get('style') or [])}_" if it.get("style") else ""
                url = it.get("uri") or (f"https://www.discogs.com/release/{it['release_id']}"
                                        if it.get("release_id") else "#")
                cc1.markdown(
                    f"{badge}[{it.get('artist', '')} — {it.get('title', '')}]({url})  "
                    f"<span class='rc-style'>chez **{it['seller']}**"
                    f"{' · ' + it['label'] if it.get('label') else ''}{price}{cond}{sty}</span>",
                    unsafe_allow_html=True)
                _fk = normalize_label(f"{it.get('artist', '')} - {it.get('title', '')}")
                _mk = _fbdone.get(f"album:{_fk}")
                if _mk:
                    cc2.caption("👍" if _mk == "up" else "👎")
                elif sc is not None:
                    _ft = {k: (det.get(k) or 0) / 100 for k in ALBUM_FEAT_KEYS}
                    if cc2.button("👍", key=f"sfbup_{lid}"):
                        log_feedback("album", _fk, f"{it.get('artist', '')} — {it.get('title', '')}"[:90],
                                     "up", sc, _ft)
                        _fbdone[f"album:{_fk}"] = "up"
                        st.rerun()
                    if cc3.button("👎", key=f"sfbdn_{lid}"):
                        log_feedback("album", _fk, f"{it.get('artist', '')} — {it.get('title', '')}"[:90],
                                     "down", sc, _ft)
                        _fbdone[f"album:{_fk}"] = "down"
                        st.rerun()
                if cc4.button("✕", key=f"sdismiss_{lid}", help="Retirer de la liste"):
                    save_json(SELLERS_NEW_PATH,
                              [x for x in load_json(SELLERS_NEW_PATH, [])
                               if x.get("listing_id") != lid])
                    st.rerun()

# ---------------------------------------------------------------- Tab: Réglages (board de scoring)

if _nav == "🎛️ Réglages":
    st.write("**Tous les paramètres de notation / classement au même endroit.** Lus partout via "
             "`scoring()` ; chaque modification recalcule les scores. Sauvegarde des jeux de "
             "réglages en **profils** pour comparer.")

    prof_store = load_json(SCORING_PROFILES_PATH, {})
    active_name = cfg().get("scoring_profile", "")

    st.subheader("Profils")
    names = sorted(prof_store)
    pc1, pc2, pc3 = st.columns([2, 1, 1])
    sel = pc1.selectbox("Profil enregistré", ["(config courante)"] + names,
                        index=(names.index(active_name) + 1) if active_name in names else 0,
                        key="scoring_profile_sel")
    if pc2.button("Charger", disabled=sel == "(config courante)"):
        cfg()["scoring"] = _deep_merge(DEFAULT_SCORING, prof_store[sel])
        cfg()["scoring_profile"] = sel
        persist()
        st.rerun()
    if pc3.button("Supprimer", disabled=sel == "(config courante)"):
        prof_store.pop(sel, None)
        save_json(SCORING_PROFILES_PATH, prof_store)
        if active_name == sel:
            cfg()["scoring_profile"] = ""
            persist()
        st.rerun()
    sv1, sv2 = st.columns([2, 1])
    newname = sv1.text_input("Nom", value=active_name or "", key="scoring_prof_name",
                             placeholder="ex. conservateur, découverte, test-09")
    if sv2.button("💾 Enregistrer sous ce nom", disabled=not newname.strip()):
        prof_store[newname.strip()] = scoring()
        save_json(SCORING_PROFILES_PATH, prof_store)
        cfg()["scoring_profile"] = newname.strip()
        persist()
        st.rerun()
    st.caption(f"Profil actif : **{active_name or '— (config courante non nommée)'}** · "
               f"{len(prof_store)} profil(s) enregistré(s).")

    st.divider()
    S = scoring()
    W = _deep_merge(DEFAULT_SCORING, {})   # copie de travail remplie par les curseurs

    def _row(section, key, label, lo, hi, step):
        W[section][key] = st.slider(label, float(lo), float(hi),
                                    float(S[section].get(key, DEFAULT_SCORING[section][key])),
                                    float(step), key=f"sc_{section}_{key}")

    st.subheader("Rangs de goût (styles) & rangs d'artistes")
    g1, g2 = st.columns(2)
    for cid in ("1", "2", "3"):
        W["taste_tiers"][cid] = g1.slider(f"Style — {CAT_LABELS[cid]}", 0.0, 1.0,
                                          float(S["taste_tiers"][cid]), 0.05, key=f"sc_tt_{cid}")
        W["artist_tiers"][cid] = g2.slider(f"Artiste — {CAT_LABELS[cid]}", 0.0, 1.0,
                                           float(S["artist_tiers"][cid]), 0.05, key=f"sc_at_{cid}")

    st.subheader("Score label (reco)")
    for k, lbl in [("collection", "Collection / wantlist"),
                   ("corpus", "Corpus (YouTube / Bandcamp / sets)"),
                   ("artist", "Artistes que j'aime sortis là"),
                   ("affinity", "Affinité de style (profilage)"),
                   ("want_factor", "Un item wantlist vaut … disque possédé")]:
        _row("reco", k, lbl, 0, 1, 0.05)
    W["label_affinity_floor"] = st.slider(
        "Seuil d'affinité global des labels (0 = désactivé)", 0, 100,
        int(S["label_affinity_floor"]), 5, key="sc_floor",
        help="Appliqué partout : reco, scan de recherche, score album. Sous le seuil (ou non "
             "profilé) = ignoré. Réversible, rien n'est supprimé.")

    st.subheader("Score album (résultats de recherche + tracks de sets + nouveautés vendeurs)")
    for k, lbl in [("label", "Label"), ("artist", "Artistes du disque"),
                   ("style", "Styles du disque"),
                   ("artist_max_vs_mean", "Artistes : part du max vs moyenne")]:
        _row("album", k, lbl, 0, 1, 0.05)

    st.subheader("Score artiste")
    for k, lbl in [("manual", "Liste manuelle"), ("corpus", "Corpus"),
                   ("collection", "Collection Discogs"), ("graph", "Proximité graphe"),
                   ("djset", "Joué par un DJ de ma base")]:
        _row("artist_score", k, lbl, 0, 1, 0.05)

    st.subheader("Graphe de producteurs")
    gg1, gg2 = st.columns(2)
    with gg1:
        for cid in ("1", "2", "3", "none"):
            W["graph"]["tier_w"][cid] = st.slider(
                f"Poids co-crédit — rang {cid}", 0.0, 6.0,
                float(S["graph"]["tier_w"][cid]), 0.1, key=f"sc_gtw_{cid}")
        W["graph"]["max_credits"] = st.slider(
            "Coupe compilation (> N crédits → ignoré)", 2, 20,
            int(S["graph"]["max_credits"]), 1, key="sc_gmc")
    with gg2:
        _row("graph", "artist_breadth", "Bonus d'ampleur artiste ×(1+b·(n−1))", 0, 1.5, 0.05)
        _row("graph", "label_breadth", "Bonus d'ampleur label ×(1+b·(n−1))", 0, 1.5, 0.05)
        _row("graph", "cat1_bonus", "Bonus si graine catégorie 1", 0, 6, 0.5)
        _row("graph", "role_main", "Poids rôle : Main", 0, 1, 0.05)
        _row("graph", "role_remix", "Poids rôle : Remix / Producer", 0, 1, 0.05)
        _row("graph", "role_other", "Poids rôle : autre", 0, 1, 0.05)
    st.caption("Poids de rang & bonus d'ampleur : appliqués à chaque rescoring (instantané). "
               "Poids de rôle & coupe compilation : uniquement à la **reconstruction** du graphe.")

    st.subheader("Poids des sources du corpus (une occurrence artiste/track vaut…)")
    for k, lbl in [("discogs_collection", "Collection Discogs"), ("discogs_want", "Wantlist"),
                   ("youtube", "YouTube"), ("bandcamp", "Bandcamp"), ("djset", "DJ sets")]:
        _row("sources", k, lbl, 0, 1.5, 0.05)

    st.subheader("Apprentissage")
    W["learn"]["l2"] = st.slider("Force de rappel par défaut (L2)", 0.5, 6.0,
                                 float(S["learn"]["l2"]), 0.5, key="sc_l2")
    W["learn"]["min_feedback"] = st.slider("Retours minimum avant proposition", 5, 60,
                                           int(S["learn"]["min_feedback"]), 1, key="sc_minfb")
    W["learn"]["min_per_class"] = st.slider("… dont minimum par classe (👍 / 👎)", 1, 20,
                                            int(S["learn"]["min_per_class"]), 1, key="sc_mincls")

    st.divider()
    ap1, ap2, ap3 = st.columns([1, 1, 2])
    if ap1.button("💾 Enregistrer les réglages", type="primary"):
        cfg()["scoring"] = W
        if active_name:
            cfg()["scoring_profile"] = ""      # config divergente d'un profil nommé
        persist()
        st.success("Réglages enregistrés — scores recalculés.")
        st.rerun()
    if ap2.button("↺ Tout réinitialiser"):
        cfg()["scoring"] = _deep_merge(DEFAULT_SCORING, {})
        cfg()["scoring_profile"] = ""
        persist()
        st.rerun()
    ap3.caption("« Enregistrer » applique tous les curseurs d'un coup.")

    with st.expander("Export / import JSON"):
        st.code(json.dumps(scoring(), indent=2, ensure_ascii=False), language="json")
        imp = st.text_area("Coller un bloc JSON à importer", height=160, key="sc_import")
        if st.button("Importer ce JSON", disabled=not imp.strip()):
            try:
                cfg()["scoring"] = _deep_merge(DEFAULT_SCORING, json.loads(imp))
                cfg()["scoring_profile"] = ""
                persist()
                st.success("Importé.")
                st.rerun()
            except Exception as e:
                st.error(f"JSON invalide : {e}")

# ---------------------------------------------------------------- Tab: Apprentissage (étape 7)

if _nav == "📈 Apprentissage":
    st.write(
        "Boucle de feedback. Chaque **👍 / 👎** posé sur une reco est journalisé avec les "
        "sous-signaux qui ont produit son score, au moment du clic :\n"
        "- **labels** recommandés (onglet 🎧) — + base / + veille = 👍, bouton 👎 ;\n"
        "- **résultats de recherche** (onglet 🔍 via 🎧) et **tracks de sets** (onglet 🎚️) — 👍 / 👎 sous chaque carte.\n\n"
        "Au-delà d'un minimum de retours, on ajuste les poids correspondants pour coller à tes "
        "décisions — régression logistique **régularisée vers les poids actuels** (un coup de "
        "pouce, pas un remplacement)."
    )
    MIN_FB = int(scoring()["learn"]["min_feedback"])
    MIN_CLS = int(scoring()["learn"]["min_per_class"])

    for kind, (title, sk, feat_keys) in FEEDBACK_KINDS.items():
        st.subheader(title)
        # dernier verdict par item (l'utilisateur peut changer d'avis)
        latest = {}
        for e in st.session_state.feedback:
            if e.get("kind") == kind and e.get("verdict") in ("up", "down"):
                latest[e["key"]] = e
        fb = list(latest.values())
        ups = [e for e in fb if e["verdict"] == "up"]
        downs = [e for e in fb if e["verdict"] == "down"]
        m1, m2, m3 = st.columns(3)
        m1.metric("Retours (items)", len(fb))
        m2.metric("👍", len(ups))
        m3.metric("👎", len(downs))

        if not fb:
            st.caption("Aucun retour de ce type pour l'instant.")
            st.divider()
            continue

        cal = []
        for lo, hi in [(0, 40), (40, 60), (60, 80), (80, 101)]:
            grp = [e for e in fb if lo <= (e.get("score_shown") or 0) < hi]
            n_up = sum(1 for e in grp if e["verdict"] == "up")
            cal.append({"tranche": f"{lo}–{hi - 1 if hi <= 100 else 100}", "retours": len(grp),
                        "% 👍": round(100 * n_up / len(grp)) if grp else None})
        feat_tbl = []
        for k in feat_keys:
            mu_up = sum(e["feat"].get(k, 0) for e in ups) / len(ups) if ups else 0
            mu_dn = sum(e["feat"].get(k, 0) for e in downs) / len(downs) if downs else 0
            feat_tbl.append({"signal": k, "moy. 👍": round(mu_up, 3),
                             "moy. 👎": round(mu_dn, 3), "écart": round(mu_up - mu_dn, 3)})
        cc1, cc2 = st.columns(2)
        cc1.caption("Calibration — le % 👍 doit monter avec le score")
        cc1.dataframe(cal, hide_index=True, use_container_width=True)
        cc2.caption("Signal moyen 👍 vs 👎 (« écart » positif = discriminant)")
        cc2.dataframe(feat_tbl, hide_index=True, use_container_width=True)

        cur = scoring()[sk]
        prior = {k: float(cur.get(k, DEFAULT_SCORING[sk].get(k, 0.3))) for k in feat_keys}
        if len(fb) < MIN_FB or len(ups) < MIN_CLS or len(downs) < MIN_CLS:
            st.info(f"Encore ~{max(MIN_FB - len(fb), 1)} retour(s) (≥ {MIN_CLS} de chaque type) "
                    "avant de proposer un ajustement fiable.")
        else:
            X = [e["feat"] for e in fb]
            y = [1 if e["verdict"] == "up" else 0 for e in fb]
            reg = st.slider("Force du rappel vers les poids actuels", 0.5, 6.0,
                            float(scoring()["learn"]["l2"]), 0.5, key=f"learn_l2_{kind}",
                            help="Élevé = on bouge peu. Bas = on suit les données.")
            prop = fit_logreg(X, y, prior, l2=reg)
            st.dataframe([{"signal": k, "actuel": round(prior[k], 3), "proposé": prop[k],
                           "Δ": round(prop[k] - prior[k], 3)} for k in feat_keys],
                         hide_index=True, use_container_width=True)
            if st.button("✅ Appliquer ces poids", type="primary", key=f"learn_apply_{kind}"):
                newsc = _deep_merge(DEFAULT_SCORING, cfg().get("scoring", {}))
                newsc[sk] = {**newsc[sk], **prop}
                cfg()["scoring"] = newsc
                persist()
                st.success("Poids mis à jour dans 🎛️ Réglages — scores recalculés.")
                st.rerun()
        st.divider()

    with st.expander(f"Journal complet ({len(st.session_state.feedback)} événements)"):
        recent = list(reversed(st.session_state.feedback))[:60]
        st.dataframe(
            [{"quand": e["ts"].replace("T", " "), "type": e.get("kind"),
              "item": e.get("name"), "verdict": e.get("verdict"),
              "score": e.get("score_shown")} for e in recent],
            hide_index=True, use_container_width=True)
        if st.checkbox("Confirmer la suppression", key="learn_wipe_ok") \
                and st.button("🗑️ Vider le journal de feedback"):
            st.session_state.feedback = []
            save_json(FEEDBACK_PATH, [])
            st.session_state.pop("_reco_dismissed", None)
            st.session_state.pop("_fb_done", None)
            st.rerun()
