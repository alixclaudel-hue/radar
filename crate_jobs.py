"""
crate_jobs.py — worker des tâches longues de Crate Radar.

N'est PAS lancé à la main : l'appli Streamlit l'invoque en sous-processus
    python crate_jobs.py <job> <params_json>
et suit la progression via jobs/<job>.status.json. Un fichier jobs/<job>.stop
demande l'arrêt propre.

Jobs : ingest_youtube · ingest_bandcamp · fetch_collection · profile_labels
       · merge_corpus · build_graph
"""

import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
# Répertoire des données : partagé avec radar_web via CRATE_DATA_DIR (volume
# persistant en conteneur ; à côté du script en local).
DATA = os.environ.get("CRATE_DATA_DIR") or HERE
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

DISCOGS_UA = "CrateRadar/1.0 +personal-use"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"
SUBSONIC_BASE = "https://bandcamp.com/api/subsonic/rest"
SOURCE_WEIGHTS = {"discogs_collection": 1.0, "discogs_want": 0.6, "youtube": 0.5,
                  "spotify": 0.5, "bandcamp": 0.9, "djset": 0.4}
ARTIST_STOPWORDS = {"various artists", "various", "va", "unknown artist", "unknown",
                    "release", "progressive classics", "no artist", "traxsource"}


# ============================================================= util

def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def cfg_load():
    """Config + secrets d'environnement (déploiement)."""
    d = load_json(CONFIG_PATH, {})
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
                   "last": "", "message": "", "error": None,
                   "started_at": datetime.now().isoformat(timespec="seconds"),
                   "finished_at": None}
        self.flush()

    def flush(self):
        save_json(self.status_path, self.st)

    def stopped(self):
        return os.path.exists(self.stop_path)

    def tick(self, last="", inc=1, total=None):
        self.st["done"] += inc
        if last:
            self.st["last"] = last
        if total is not None:
            self.st["total"] = total
        self.flush()

    def msg(self, m):
        self.st["message"] = m
        self.flush()

    def finish(self, message="", error=None):
        self.st.update(running=False, message=message or self.st["message"],
                       error=error, finished_at=datetime.now().isoformat(timespec="seconds"))
        if self.stopped():
            os.remove(self.stop_path)
        self.flush()


# ============================================================= Discogs

def discogs_get(token, path, params=None):
    p = dict(params or {})
    p["token"] = token
    for attempt in range(5):
        try:
            r = requests.get(f"https://api.discogs.com{path}", params=p,
                             headers={"User-Agent": DISCOGS_UA}, timeout=25)
        except requests.RequestException:
            time.sleep(3)
            continue
        if r.status_code == 429:
            time.sleep(12 + 6 * attempt)
            continue
        if not r.ok:
            return {}
        return r.json()
    return {}


def discogs_search(token, **params):
    params["type"] = params.get("type", "release")
    return discogs_get(token, "/database/search", params)


def lookup_key(artist, title):
    return f"{style_key(artist)}||{style_key(title)}"


def discogs_lookup(token, artist, title, cache, kind="track", deep=True):
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
    for i, ap in enumerate(attempts):
        if i:
            time.sleep(0.3)
        calls += 1
        res = discogs_search(token, per_page=5, **ap).get("results", [])
        if res:
            r0 = res[0]
            labels = r0.get("label") or []
            vinyl_id = next((x.get("id") for x in res
                             if "vinyl" in " ".join(x.get("format") or []).lower()), None)
            hit = {"label": labels[0] if labels else None, "release_id": r0.get("id"),
                   "year": r0.get("year"), "style": r0.get("style") or [],
                   "format": r0.get("format") or [], "vinyl_release_id": vinyl_id}
            break
    cache[k] = hit
    return hit, calls


# ============================================================= YouTube parsing

_YT_NOISE = re.compile(
    r"\((official|lyric|lyrics|audio|visuali[sz]er|music|hq|hd|4k|full)\b[^)]*\)"
    r"|\bofficial (video|audio|music video)\b|\bfree (dl|download)\b|\[[^\]]*\]", re.I)
_YT_SPLIT = re.compile(r"\s+[-–—―─‐‑－•·]\s+")
_YT_LABEL_RES = [
    re.compile(r"under exclusive licen[sc]e to (.+?)(?:\.|;|\n|$)", re.I),
    re.compile(r"℗\s*\d{4}\s+(.+?)(?:\n|$)"),
]


def parse_yt_title(title, channel):
    t = _YT_NOISE.sub("", title or "").strip(" -–—―─|·•")
    parts = _YT_SPLIT.split(t, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    ch = re.sub(r"\s*-\s*topic\s*$", "", channel or "", flags=re.I).strip()
    if ch and ch.lower() not in ("various artists", "va"):
        return ch, t
    return "", t


def parse_yt_label(desc):
    for rx in _YT_LABEL_RES:
        m = rx.search(desc or "")
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip(" .")
    return None


def yt_playlist_id(u):
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", u or "")
    return m.group(1) if m else (u or "").strip() or None


def youtube_items(pid, key, max_pages=40):
    items, token = [], None
    for _ in range(max_pages):
        params = {"part": "snippet", "playlistId": pid, "maxResults": 50, "key": key}
        if token:
            params["pageToken"] = token
        r = requests.get(f"{YOUTUBE_API}/playlistItems", params=params, timeout=25)
        if not r.ok:
            raise RuntimeError(f"YouTube API {r.status_code}: {r.text[:200]}")
        d = r.json()
        for it in d.get("items", []):
            sn = it.get("snippet", {})
            items.append({"title": sn.get("title", ""),
                          "channel": sn.get("videoOwnerChannelTitle") or sn.get("channelTitle", ""),
                          "description": sn.get("description", "")})
        token = d.get("nextPageToken")
        if not token:
            break
    return items


# ============================================================= Spotify

def spotify_playlist_id(u):
    m = re.search(r"playlist[/:]([A-Za-z0-9]+)", u or "")
    return m.group(1) if m else ((u or "").strip() or None)


def spotify_token(client_id, client_secret):
    r = requests.post(SPOTIFY_TOKEN_URL, data={"grant_type": "client_credentials"},
                      auth=(client_id, client_secret), timeout=20)
    if not r.ok:
        raise RuntimeError(f"Spotify auth {r.status_code}: {r.text[:200]}")
    return r.json().get("access_token", "")


def spotify_playlist_meta(pid, tok):
    r = requests.get(f"{SPOTIFY_API}/playlists/{pid}",
                     headers={"Authorization": f"Bearer {tok}"},
                     params={"fields": "name,owner(display_name),tracks(total)"}, timeout=20)
    if not r.ok:
        raise RuntimeError(f"Spotify playlist {r.status_code}: {r.text[:200]}")
    d = r.json()
    return {"title": d.get("name") or pid,
            "channel": (d.get("owner") or {}).get("display_name") or "",
            "n_items": (d.get("tracks") or {}).get("total") or 0}


def spotify_items(pid, tok, max_pages=40):
    items = []
    url = f"{SPOTIFY_API}/playlists/{pid}/tracks"
    params = {"limit": 100,
              "fields": "next,items(track(name,artists(name),album(name)))"}
    for _ in range(max_pages):
        r = requests.get(url, headers={"Authorization": f"Bearer {tok}"},
                         params=params, timeout=25)
        if not r.ok:
            raise RuntimeError(f"Spotify tracks {r.status_code}: {r.text[:200]}")
        d = r.json()
        for it in d.get("items", []):
            tr = it.get("track") or {}
            if not tr.get("name"):
                continue
            arts = [a.get("name") for a in (tr.get("artists") or []) if a.get("name")]
            items.append({"artist": ", ".join(arts), "title": tr.get("name") or "",
                          "album": (tr.get("album") or {}).get("name") or ""})
        url = d.get("next")
        params = None
        if not url:
            break
    return items


# ============================================================= Bandcamp / Subsonic

def subsonic_get(method, user, password, **params):
    salt = os.urandom(8).hex()
    tok = hashlib.md5((password + salt).encode()).hexdigest()
    p = {"u": user, "t": tok, "s": salt, "v": "1.16.1", "c": "CrateRadar", "f": "json", **params}
    r = requests.get(f"{SUBSONIC_BASE}/{method}", params=p, timeout=25)
    if not r.ok:
        raise RuntimeError(f"Subsonic {method} HTTP {r.status_code}")
    body = r.json().get("subsonic-response", {})
    if body.get("status") != "ok":
        raise RuntimeError(f"Subsonic {method}: {body.get('error', {}).get('message', body)}")
    return body


def bandcamp_albums(user, password, max_pages=60):
    out, offset, size = [], 0, 500
    for _ in range(max_pages):
        body = subsonic_get("getAlbumList2", user, password,
                            type="alphabeticalByName", size=size, offset=offset)
        al = body.get("albumList2", {}).get("album", [])
        for a in al:
            out.append({"artist": (a.get("artist") or "").strip(),
                        "title": (a.get("name") or "").strip(), "genre": a.get("genre")})
        if len(al) < size:
            break
        offset += size
        time.sleep(0.4)
    return out


# ============================================================= corpus helpers

def corpus_merge(new_rows, source):
    corpus = load_json(CORPUS_PATH, [])
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
    return added, len(corpus)


# ============================================================= JOBS

def job_ingest_youtube(job, params):
    cfg = cfg_load()
    token = cfg.get("token", "")
    key = params.get("api_key") or cfg.get("youtube_api_key", "")
    urls = params.get("urls") or [u for u in cfg.get("youtube_playlists", "").splitlines() if u.strip()]
    deep = params.get("deep", True)
    pairs = [(u, yt_playlist_id(u)) for u in urls]
    pids = [p for _, p in pairs if p]
    if not pids:
        return job.finish(error="Aucune playlist.")
    job.msg("Lecture des playlists…")
    meta = load_json(YOUTUBE_META_PATH, {})
    raw = []
    for url, pid in [(u, p) for u, p in pairs if p]:
        try:
            r = requests.get(f"{YOUTUBE_API}/playlists",
                             params={"part": "snippet", "id": pid, "key": key}, timeout=20)
            sn = (r.json().get("items") or [{}])[0].get("snippet", {})
        except Exception:
            sn = {}
        items = youtube_items(pid, key)
        for it in items:
            a, t = parse_yt_title(it["title"], it["channel"])
            if a or t:
                raw.append({"artist": a, "title": t, "label": parse_yt_label(it["description"]),
                            "url": None})
        meta[pid] = {"url": url, "title": sn.get("title") or pid,
                     "channel": sn.get("channelTitle") or "",
                     "n_items": len(items),
                     "imported_at": datetime.now().isoformat(timespec="seconds")}
    save_json(YOUTUBE_META_PATH, meta)
    cache = load_json(LOOKUP_CACHE_PATH, {})
    corpus = load_json(CORPUS_PATH, [])
    seen = {("youtube", style_key(r.get("artist", "")), style_key(r.get("title", ""))) for r in corpus}
    todo = [r for r in raw if ("youtube", style_key(r["artist"]), style_key(r["title"])) not in seen]
    job.st["total"] = len(todo)
    job.msg(f"{len(raw)} titres, {len(todo)} à traiter.")
    acc = []
    for i, r in enumerate(todo):
        if job.stopped():
            break
        label = r["label"]
        rid, style, calls = None, [], 0
        if not label and (r["artist"] or r["title"]):
            hit, calls = discogs_lookup(token, r["artist"], r["title"], cache, deep=deep)
            if hit:
                label, rid, style = hit["label"], hit["release_id"], hit["style"]
        acc.append({"artist": r["artist"], "title": r["title"], "label": label,
                    "release_id": rid, "style": style, "genre": None, "url": None})
        job.tick(f"{r['artist']} — {r['title']}" + (f"  → {label}" if label else "  → —"))
        if calls:
            time.sleep(max(0.2, 1.1 * calls - 0.3 * (calls - 1)))
        if (i + 1) % 15 == 0:
            corpus_merge(acc, "youtube")
            acc = []
            save_json(LOOKUP_CACHE_PATH, cache)
    add, tot = corpus_merge(acc, "youtube")
    save_json(LOOKUP_CACHE_PATH, cache)
    job.finish(f"+{job.st['done']} traités. Corpus : {tot}.")


def job_ingest_spotify(job, params):
    cfg = cfg_load()
    token = cfg.get("token", "")
    cid = params.get("client_id") or cfg.get("spotify_client_id", "")
    csec = params.get("client_secret") or cfg.get("spotify_client_secret", "")
    urls = params.get("urls") or [u for u in cfg.get("spotify_playlists", "").splitlines() if u.strip()]
    deep = params.get("deep", True)
    if not (cid and csec):
        return job.finish(error="Identifiants API Spotify manquants (client ID + secret).")
    pids = [(u, p) for u, p in ((u, spotify_playlist_id(u)) for u in urls) if p]
    if not pids:
        return job.finish(error="Aucune playlist Spotify.")
    job.msg("Connexion à l'API Spotify…")
    try:
        tok = spotify_token(cid, csec)
    except Exception as e:
        return job.finish(error=str(e))
    if not tok:
        return job.finish(error="Spotify : jeton d'accès vide (identifiants ?).")
    meta = load_json(SPOTIFY_META_PATH, {})
    raw = []
    for url, pid in pids:
        try:
            m = spotify_playlist_meta(pid, tok)
        except Exception:
            m = {"title": pid, "channel": "", "n_items": 0}
        try:
            items = spotify_items(pid, tok)
        except Exception as e:
            return job.finish(error=str(e))
        for it in items:
            if it["artist"] or it["title"]:
                raw.append({"artist": it["artist"], "title": it["title"], "label": None, "url": None})
        meta[pid] = {"url": url, "title": m["title"], "channel": m["channel"],
                     "n_items": len(items) or m["n_items"],
                     "imported_at": datetime.now().isoformat(timespec="seconds")}
    save_json(SPOTIFY_META_PATH, meta)
    cache = load_json(LOOKUP_CACHE_PATH, {})
    corpus = load_json(CORPUS_PATH, [])
    seen = {("spotify", style_key(r.get("artist", "")), style_key(r.get("title", ""))) for r in corpus}
    todo = [r for r in raw if ("spotify", style_key(r["artist"]), style_key(r["title"])) not in seen]
    job.st["total"] = len(todo)
    job.msg(f"{len(raw)} titres, {len(todo)} à traiter.")
    acc = []
    for i, r in enumerate(todo):
        if job.stopped():
            break
        label, rid, style, calls = None, None, [], 0
        if r["artist"] or r["title"]:
            hit, calls = discogs_lookup(token, r["artist"], r["title"], cache, deep=deep)
            if hit:
                label, rid, style = hit["label"], hit["release_id"], hit["style"]
        acc.append({"artist": r["artist"], "title": r["title"], "label": label,
                    "release_id": rid, "style": style, "genre": None, "url": None})
        job.tick(f"{r['artist']} — {r['title']}" + (f"  → {label}" if label else "  → —"))
        if calls:
            time.sleep(max(0.2, 1.1 * calls - 0.3 * (calls - 1)))
        if (i + 1) % 15 == 0:
            corpus_merge(acc, "spotify")
            acc = []
            save_json(LOOKUP_CACHE_PATH, cache)
    add, tot = corpus_merge(acc, "spotify")
    save_json(LOOKUP_CACHE_PATH, cache)
    job.finish(f"+{job.st['done']} traités. Corpus : {tot}.")


def job_ingest_bandcamp(job, params):
    cfg = cfg_load()
    token = cfg.get("token", "")
    u = params.get("user") or cfg.get("bandcamp_sub_user", "")
    pw = params.get("password") or cfg.get("bandcamp_sub_pass", "")
    deep = params.get("deep", True)
    if not (u and pw):
        return job.finish(error="Identifiants Subsonic manquants.")
    job.msg("Lecture de la collection Bandcamp…")
    albums = bandcamp_albums(u, pw)
    cache = load_json(LOOKUP_CACHE_PATH, {})
    corpus = load_json(CORPUS_PATH, [])
    seen = {("bandcamp", style_key(r.get("artist", "")), style_key(r.get("title", ""))) for r in corpus}
    todo = [a for a in albums if ("bandcamp", style_key(a["artist"]), style_key(a["title"])) not in seen]
    job.st["total"] = len(todo)
    job.msg(f"{len(albums)} albums, {len(todo)} à traiter.")
    acc = []
    for i, a in enumerate(todo):
        if job.stopped():
            break
        label, rid, style, calls = None, None, [], 0
        if a["artist"] or a["title"]:
            hit, calls = discogs_lookup(token, a["artist"], a["title"], cache, kind="release", deep=deep)
            if hit:
                label, rid, style = hit["label"], hit["release_id"], hit["style"]
        acc.append({"artist": a["artist"], "title": a["title"], "label": label,
                    "release_id": rid, "style": style, "genre": a.get("genre"), "url": None})
        job.tick(f"{a['artist']} — {a['title']}" + (f"  → {label}" if label else "  → —"))
        if calls:
            time.sleep(max(0.2, 1.1 * calls - 0.3 * (calls - 1)))
        if (i + 1) % 15 == 0:
            corpus_merge(acc, "bandcamp")
            acc = []
            save_json(LOOKUP_CACHE_PATH, cache)
    add, tot = corpus_merge(acc, "bandcamp")
    save_json(LOOKUP_CACHE_PATH, cache)
    job.finish(f"+{job.st['done']} traités. Corpus : {tot}.")


def job_fetch_collection(job, params):
    cfg = cfg_load()
    token = cfg.get("token", "")
    ident = discogs_get(token, "/oauth/identity")
    user = ident.get("username")
    if not user:
        return job.finish(error="Identité Discogs illisible (token ?).")
    lc, wc, lids, ac = {}, {}, {}, {}
    n_coll = n_want = 0
    for kind, path, rk, tgt in (
        ("collection", f"/users/{user}/collection/folders/0/releases", "releases", lc),
        ("wantlist", f"/users/{user}/wants", "wants", wc),
    ):
        page, pages = 1, 1
        while page <= pages and page <= 200:
            if job.stopped():
                break
            d = discogs_get(token, path, {"page": page, "per_page": 100})
            items = d.get(rk, [])
            for it in items:
                bi = it.get("basic_information", {})
                for lb in bi.get("labels", []):
                    nm = (lb.get("name") or "").strip()
                    if not nm:
                        continue
                    k = normalize_label(nm)
                    tgt[k] = tgt.get(k, 0) + 1
                    if lb.get("id") and k not in lids:
                        lids[k] = {"name": nm, "id": lb.get("id")}
                for ar in bi.get("artists", []):
                    nm = (ar.get("name") or "").strip()
                    if nm and nm.lower() != "various":
                        ac[nm] = ac.get(nm, 0) + 1
            if kind == "collection":
                n_coll += len(items)
            else:
                n_want += len(items)
            pages = d.get("pagination", {}).get("pages", 1)
            job.tick(f"{kind} page {page}/{pages}", total=None)
            page += 1
            if page <= pages:
                time.sleep(1.1)
    cache = {"username": user, "fetched_at": datetime.now().isoformat(timespec="seconds"),
             "n_collection": n_coll, "n_wants": n_want, "label_counts": lc,
             "want_label_counts": wc, "label_ids": lids, "artist_counts": ac}
    save_json(COLLECTION_CACHE_PATH, cache)
    # merge labels into base + seed resolved
    if params.get("merge_base", True):
        cfg = cfg_load()
        base = cfg.get("labels", [])
        exist = {normalize_label(x) for x in base}
        newn = [v["name"] for k, v in lids.items() if k not in exist]
        cfg["labels"] = base + newn
        save_json(CONFIG_PATH, cfg)
        res = load_json(RESOLVED_PATH, {})
        for k, v in lids.items():
            cur = res.get(k)
            if cur and cur.get("status") in ("exact", "confirmed"):
                continue
            res[k] = {"original": v["name"], "discogs_name": v["name"], "discogs_id": v["id"],
                      "status": "confirmed", "reviewed_by": "collection"}
        save_json(RESOLVED_PATH, res)
        job.finish(f"{n_coll} disques, {n_want} wants. +{len(newn)} labels ajoutés à la base.")
    else:
        job.finish(f"{n_coll} disques, {n_want} wants.")


def job_merge_corpus(job, params):
    """Fusionne les labels du corpus dans la base + nettoyage."""
    cfg = cfg_load()
    corpus = load_json(CORPUS_PATH, [])
    resolved = load_json(RESOLVED_PATH, {})
    base = cfg.get("labels", [])
    base_keys = {normalize_label(x) for x in base}

    junk_sub = ["(bmi)", "(ascap)", "(sesac)", "(prs)", "(gema)", "(sacem)", "distrokid",
                "cd baby", "cdbaby", "tunecore", "the orchard", "believe digital", "ingrooves",
                "routenote", "amuse", "ditto music", "label engine"]

    def clean(name):
        s = re.sub(r"^\s*discogs\s*:\s*", "", (name or "").strip(), flags=re.I)
        m = re.search(r"(?:licen[sc]e\s+(?:to|from)|distributed by|marketed by)\s+(.+)$", s, re.I)
        if m:
            s = m.group(1).strip()
        s = s.rstrip(" :;.-")
        if "http" in s.lower() or "_" in s or len(s) < 2:
            return ""
        return s

    def is_junk(n):
        low = n.lower()
        return (not low or "not on label" in low or low in ("none", "n/a", "unknown")
                or any(x in low for x in junk_sub))

    counts, disp = Counter(), {}
    dropped = []
    for r in corpus:
        lab = clean(r.get("label"))
        if not lab:
            if r.get("label"):
                dropped.append(r["label"])
            continue
        k = normalize_label(lab)
        counts[k] += 1
        disp.setdefault(k, lab)
    good = [k for k in disp if not is_junk(disp[k])]
    new = sorted(disp[k] for k in good if k not in base_keys)
    job.st["total"] = len(new)

    cfg["labels"] = base + new
    save_json(CONFIG_PATH, cfg)
    seeded = 0
    for k in good:
        cur = resolved.get(k)
        if cur and cur.get("status") in ("exact", "confirmed"):
            continue
        resolved[k] = {"original": disp[k], "discogs_name": disp[k],
                       "discogs_id": (cur or {}).get("discogs_id"),
                       "status": "confirmed", "reviewed_by": "corpus"}
        seeded += 1
    save_json(RESOLVED_PATH, resolved)
    job.st["done"] = len(new)
    job.finish(f"+{len(new)} labels ajoutés (base {len(base)} → {len(cfg['labels'])}), "
               f"{seeded} résolus, {len(set(dropped))} écartés.")


def _reco_label_priority():
    coll = load_json(COLLECTION_CACHE_PATH, {})
    corpus = load_json(CORPUS_PATH, [])
    score = Counter()
    disp = {}
    for k, n in coll.get("label_counts", {}).items():
        score[k] += n
        disp.setdefault(k, coll.get("label_ids", {}).get(k, {}).get("name") or k)
    for k, n in coll.get("want_label_counts", {}).items():
        score[k] += 0.6 * n
        disp.setdefault(k, coll.get("label_ids", {}).get(k, {}).get("name") or k)
    for r in corpus:
        if r.get("label"):
            k = normalize_label(r["label"])
            score[k] += 0.5
            disp.setdefault(k, r["label"])
    return score, disp


def job_profile_labels(job, params):
    cfg = cfg_load()
    token = cfg.get("token", "")
    limit = int(params.get("limit", 150))
    profile = load_json(PROFILE_PATH, {})
    resolved = load_json(RESOLVED_PATH, {})
    score, disp = _reco_label_priority()
    # 1) labels prioritaires (collection / wantlist / corpus), les plus fréquents d'abord
    ordered = [k for k, _ in score.most_common()]
    seen = set(ordered)
    # 2) puis tout le reste de la base de labels (non couvert par le signal ci-dessus)
    for name in cfg.get("labels", []):
        k = normalize_label(name)
        if k and k not in seen:
            seen.add(k)
            ordered.append(k)
            disp.setdefault(k, name)
    todo = [k for k in ordered if k not in profile][:limit]
    job.st["total"] = len(todo)

    def canon(k):
        e = resolved.get(k)
        if e and e.get("status") in ("exact", "approx", "confirmed") and e.get("discogs_name"):
            return e["discogs_name"]
        return disp.get(k, k)

    for i, k in enumerate(todo):
        if job.stopped():
            break
        d = discogs_search(token, label=canon(k), per_page=100, page=1,
                           sort="want", sort_order="desc")
        res = d.get("results", [])
        sc, gc = Counter(), Counter()
        for x in res:
            sc.update(x.get("style") or [])
            gc.update(x.get("genre") or [])
        profile[k] = {"original": disp.get(k, k), "sampled": len(res),
                      "style_counts": dict(sc), "genre_counts": dict(gc),
                      "total_items": d.get("pagination", {}).get("items", len(res)),
                      "profiled_at": datetime.now().isoformat(timespec="seconds")}
        job.tick(f"{canon(k)} — {len(res)} sorties")
        if (i + 1) % 15 == 0:
            save_json(PROFILE_PATH, profile)
        time.sleep(1.1)
    save_json(PROFILE_PATH, profile)
    job.finish(f"+{job.st['done']} labels profilés. Total : {len(profile)}.")


# ---- build_graph (étape 4b) -------------------------------------------------

_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|&| feat\.? | ft\.? | vs\.? | and | x | with | pres\.? )\s*", re.I)
_LABEL_STOP = {"not on label", "self-released", "self released", "white label", "no label",
               "none", "unknown", "resident advisor", "n/a", "promo only", "dmc",
               "tm century", "cd pool", "ministry of sound", "universal", "columbia",
               "emi", "sony music", "warner music", "polystar", "zyx music",
               "wagram music", "more music and media"}
_LABEL_STOP_SUB = ["magazine", "podcast", "mixtape", "not on label", "self-released",
                   "promo only", "dj center", "bootleg"]


def _clean_graph_label(name):
    """Nom de label propre pour le graphe, ou "" si à écarter."""
    s = (name or "").split(",")[0].strip()                 # "GR Records, GR Records" -> "GR Records"
    s = re.sub(r"\s*\(\d+\)\s*$", "", s).strip()           # "(2)" désambiguïsation
    low = s.lower()
    if len(s) < 2 or low in _LABEL_STOP or any(x in low for x in _LABEL_STOP_SUB):
        return ""
    return s


def _split_artists(s):
    """Découpe une chaîne de crédit Discogs en noms individuels, tels que Discogs les
    écrit (on garde « (2) » qui désambiguïse un homonyme ; on retire juste le « * »
    de variation)."""
    out = []
    for p in _ARTIST_SPLIT.split(s or ""):
        p = p.split("=")[0]                        # "A = 寺田創一" -> "A"
        p = re.sub(r"\s*\*+\s*$", "", p.strip())   # marqueur de variation Discogs
        if p and normalize_label(p) not in ARTIST_STOPWORDS and p not in out:
            out.append(p)
    return out


def _seed_artists(cfg):
    """Retourne (scored, tier, disp) :
    scored = [(clé, nom, score)] classé du plus fort au plus faible,
    tier = {clé: '1'|'2'|'3'}, disp = {clé: nom d'affichage}."""
    cats = cfg.get("artist_categories", {"1": [], "2": [], "3": []})
    aw = cfg.get("artist_weights", {"1": 1.0, "2": 0.6, "3": 0.3})
    sw = cfg.get("artist_score_weights", {"manual": 0.6, "corpus": 0.25, "collection": 0.15})
    corpus = load_json(CORPUS_PATH, [])
    coll = load_json(COLLECTION_CACHE_PATH, {})
    cc, lc, disp = Counter(), Counter(), {}
    for r in corpus:
        a = (r.get("artist") or "").strip()
        k = normalize_label(a)
        if k and k not in ARTIST_STOPWORDS:
            cc[k] += 1
            disp.setdefault(k, a)
    for a, n in coll.get("artist_counts", {}).items():
        k = normalize_label(a)
        if k and k not in ARTIST_STOPWORDS:
            lc[k] += n
            disp.setdefault(k, a)
    tier = {}
    for cid, names in cats.items():
        for n in names:
            tier[normalize_label(n)] = cid
            disp.setdefault(normalize_label(n), n)
    mc = max(cc.values(), default=1)
    ml = max(lc.values(), default=1)
    out = {}
    for k in set(tier) | set(cc) | set(lc):
        s = (sw.get("manual", .6) * (float(aw.get(tier.get(k), 0)))
             + sw.get("corpus", .25) * (cc.get(k, 0) / mc)
             + sw.get("collection", .15) * (lc.get(k, 0) / ml))
        out[k] = (s, disp.get(k, k))
    ranked = sorted(out.items(), key=lambda kv: kv[1][0], reverse=True)
    scored = [(k, v[1], v[0]) for k, v in ranked]
    return scored, tier, disp


def _canon_key(name, ares):
    e = ares.get(normalize_label(name))
    if e and e.get("discogs_id") and e.get("status") in ("exact", "approx", "confirmed"):
        return f"id:{e['discogs_id']}"
    return normalize_label(name)


def job_build_graph(job, params):
    """Construit le graphe de producteurs et stocke les ARÊTES BRUTES (comptes de
    co-crédits par graine, poids de rôle, labels). Le score final est recalculé
    côté appli à partir des catégories courantes (pas besoin de re-fetch)."""
    cfg = cfg_load()
    token = cfg.get("token", "")
    pages = int(params.get("pages", 2))
    mode = params.get("mode", "top")
    incremental = bool(params.get("incremental"))
    gsc = (cfg.get("scoring") or {}).get("graph", {})   # cf. DEFAULT_SCORING["graph"] côté appli
    max_credits = int(params.get("max_credits", gsc.get("max_credits", 6)))  # > N = compilation
    role_main = float(gsc.get("role_main", 1.0))
    role_remix = float(gsc.get("role_remix", 0.7))
    role_other = float(gsc.get("role_other", 0.4))
    ares = load_json(ARTISTS_RESOLVED_PATH, {})

    prev = load_json(PRODUCER_GRAPH_PATH, {}) if incremental else {}
    prev_seeds = prev.get("seeds", {}) if prev.get("edges") is not None else {}

    # --- liste des graines : (seed_key canonique, nom, rid ou None) ---
    seeds, seen = [], set()
    if mode == "global":
        for nk, e in ares.items():
            rid = e.get("discogs_id")
            if rid and e.get("status") in ("exact", "approx", "confirmed"):
                sk = f"id:{rid}"
                if sk in seen or (incremental and sk in prev_seeds):
                    continue
                seen.add(sk)
                seeds.append((sk, e.get("discogs_name") or e.get("original") or nk, rid))
    else:
        scored, _tier, _disp = _seed_artists(cfg)
        explicit = params.get("seed_names")
        names = ([n for n in explicit if n and n.strip()] if explicit
                 else [d for _k, d, _s in scored[:int(params.get("seeds", 40))]])
        for nm in names:
            sk = _canon_key(nm, ares)
            if sk in seen:
                continue
            seen.add(sk)
            e = ares.get(normalize_label(nm)) or {}
            seeds.append((sk, e.get("discogs_name") or nm, e.get("discogs_id") or e.get("id")))

    if incremental and prev_seeds and not seeds:
        return job.finish("Graphe déjà à jour — aucun nouvel artiste résolu depuis le dernier build.")
    if len(seeds) < 2 and not (incremental and prev_seeds):
        return job.finish(error=f"Trop peu de graines ({len(seeds)}).")

    # en incrémental : on repart des arêtes existantes et on ajoute celles des nouvelles graines
    art_edges = prev.get("edges", {}) if incremental else {}
    lab_edges = prev.get("label_edges", {}) if incremental else {}
    seed_names = dict(prev_seeds) if incremental else {}
    seed_names.update({sk: nm for sk, nm, _ in seeds})
    seed_set = set(seed_names)
    job.st["total"] = len(seeds)
    job.msg(f"{len(seeds)} graine(s) à traiter"
            + (" (mise à jour)" if incremental and prev_seeds else " — graphe global"))
    n_resolved = 0

    for i, (sk, name, rid) in enumerate(seeds):
        if job.stopped():
            break
        if not rid:
            r = discogs_search(token, type="artist", q=name, per_page=3).get("results", [])
            if r:
                rid = r[0].get("id")
                ares[normalize_label(name)] = {"original": name, "discogs_name": r[0].get("title"),
                                               "discogs_id": rid, "status": "approx",
                                               "candidates": []}
                save_json(ARTISTS_RESOLVED_PATH, ares)
            time.sleep(1.1)
        if not rid:
            job.tick(f"{name} — introuvable")
            continue
        n_resolved += 1
        rels = []
        for pg in range(1, pages + 1):
            d = discogs_get(token, f"/artists/{rid}/releases",
                            {"per_page": 100, "page": pg, "sort": "year", "sort_order": "desc"})
            rels += d.get("releases", [])
            if pg >= d.get("pagination", {}).get("pages", 1):
                break
            time.sleep(1.1)
        for rel in rels:
            co_names = _split_artists(rel.get("artist", ""))
            if len(co_names) > max_credits:        # compilation -> on saute tout (artistes + label)
                continue
            role = rel.get("role") or "Main"
            rw = role_main if role == "Main" else (
                role_remix if role in ("Remix", "Producer") else role_other)
            for co in co_names:
                ck = _canon_key(co, ares)
                if not ck or ck in seed_set or ck in ARTIST_STOPWORDS:
                    continue
                ce = art_edges.setdefault(ck, {"name": co, "id": None, "co": {}})
                slot = ce["co"].setdefault(sk, {"n": 0, "rw": rw})
                slot["n"] += 1
                slot["rw"] = max(slot["rw"], rw)
                re_ = ares.get(normalize_label(co))
                if re_ and re_.get("discogs_id"):
                    ce["id"] = re_["discogs_id"]
                    ce["name"] = re_.get("discogs_name") or co
            lab = _clean_graph_label(rel.get("label"))
            if lab:
                lk = normalize_label(lab)
                le = lab_edges.setdefault(lk, {"name": lab, "co": {}})
                le["co"][sk] = le["co"].get(sk, 0) + 1
        job.tick(f"{name} — {len(rels)} sorties")
        time.sleep(1.1)

    if not art_edges or (n_resolved == 0 and not (incremental and prev_seeds)):
        return job.finish(error=f"Graphe vide : {n_resolved}/{len(seeds)} graines résolues.")
    # un artiste devenu graine ne doit plus figurer comme candidat
    for sk in list(art_edges):
        if sk in seed_set:
            del art_edges[sk]
    total_seeds = len(seed_names)
    save_json(PRODUCER_GRAPH_PATH, {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "global" if mode == "global" or incremental else mode,
        "n_resolved_seeds": total_seeds,
        "seeds": seed_names, "edges": art_edges, "label_edges": lab_edges,
    })
    tag = "mise à jour" if incremental and prev_seeds else mode
    job.finish(f"{tag} · +{n_resolved} graine(s) traitée(s), {total_seeds} au total → "
               f"{len(art_edges)} artistes liés, {len(lab_edges)} labels.")


def job_resolve_artists(job, params):
    """Associe chaque artiste (liste manuelle par défaut) à son id Discogs.
    Écrit artists_resolved.json : {clé: {original, discogs_name, discogs_id, status,
    candidates}} avec status exact / approx / not_found."""
    cfg = cfg_load()
    token = cfg.get("token", "")
    ares = load_json(ARTISTS_RESOLVED_PATH, {})
    names = params.get("names")
    if not names:
        cats = cfg.get("artist_categories", {})
        names = [n for cid in ("1", "2", "3") for n in cats.get(cid, [])]
        if params.get("scope") == "all":
            for r in load_json(CORPUS_PATH, []):
                if r.get("artist"):
                    names.append(r["artist"])
            for a in load_json(COLLECTION_CACHE_PATH, {}).get("artist_counts", {}):
                names.append(a)
    names = [n for n in names if normalize_label(n) not in ARTIST_STOPWORDS]
    force = params.get("force", False)
    seen, todo = set(), []
    for n in names:
        k = normalize_label(n)
        if not k or k in seen:
            continue
        seen.add(k)
        cur = ares.get(k)
        if force or not cur or cur.get("status") not in ("exact", "confirmed"):
            todo.append(n)
    job.st["total"] = len(todo)
    for i, name in enumerate(todo):
        if job.stopped():
            break
        d = discogs_search(token, type="artist", q=name, per_page=8)
        cands = [{"name": c.get("title"), "id": c.get("id")}
                 for c in d.get("results", []) if c.get("title")]
        k = normalize_label(name)
        if not cands:
            ares[k] = {"original": name, "discogs_name": None, "discogs_id": None,
                       "status": "not_found", "candidates": []}
        else:
            tgt = normalize_label(name)
            exact = next((c for c in cands if normalize_label(c["name"]) == tgt), None)
            top = exact or cands[0]
            ares[k] = {"original": name, "discogs_name": top["name"], "discogs_id": top["id"],
                       "status": "exact" if exact else "approx", "candidates": cands}
        job.tick(f"{name} → {ares[k]['discogs_name'] or '?'} ({ares[k]['status']})")
        if (i + 1) % 10 == 0:
            save_json(ARTISTS_RESOLVED_PATH, ares)
        time.sleep(1.1)
    save_json(ARTISTS_RESOLVED_PATH, ares)
    ex = sum(1 for v in ares.values() if v.get("status") == "exact")
    ap = sum(1 for v in ares.values() if v.get("status") == "approx")
    nf = sum(1 for v in ares.values() if v.get("status") == "not_found")
    job.finish(f"{len(ares)} artistes ({ex} exacts, {ap} approx à vérifier, {nf} introuvables).")


def _search_label_releases(token, label_name, genre, style, fmt, year, max_pages):
    out, page = [], 1
    while page <= max_pages:
        d = discogs_search(token, label=label_name, genre=genre, style=style, format=fmt,
                           year=year, per_page=100, page=page, sort="year", sort_order="desc")
        out.extend(d.get("results", []))
        if page >= d.get("pagination", {}).get("pages", 1):
            break
        page += 1
        time.sleep(1.1)
    return out


def job_search_base(job, params):
    """Scan « sans label » : une requête par label du périmètre, filtrée genre/style/
    année. Périmètre + filtres lus dans jobs/search_base.input.json. Résultat écrit
    dans search_results.json."""
    cfg = cfg_load()
    token = cfg.get("token", "")
    inp = load_json(SEARCH_INPUT_PATH, {})
    scope = inp.get("scope") or []
    genre, style, fmt = inp.get("genre", ""), inp.get("style", ""), inp.get("fmt", "")
    yr = inp.get("year", "")
    pages = int(inp.get("pages", 3))
    yf = int(inp["year_from"]) if str(inp.get("year_from", "")).strip().isdigit() else None
    yt = int(inp["year_to"]) if str(inp.get("year_to", "")).strip().isdigit() else None
    resolved = load_json(RESOLVED_PATH, {})

    def canon(name):
        e = resolved.get(normalize_label(name))
        if e and e.get("status") in ("exact", "approx", "confirmed") and e.get("discogs_name"):
            return e["discogs_name"]
        return name

    job.st["total"] = len(scope)
    acc, errors = [], []
    for lab in scope:
        if job.stopped():
            break
        c = canon(lab)
        try:
            rels = _search_label_releases(token, c, genre, style, fmt, yr, pages)
            if not rels:
                d2 = discogs_search(token, q=c, genre=genre, style=style, format=fmt,
                                    year=yr, per_page=100, sort="year", sort_order="desc")
                tgt = normalize_label(c)
                rels = [r for r in d2.get("results", [])
                        if any(normalize_label(x) == tgt for x in r.get("label", []))]
            for r in rels:
                r["_base_label"] = lab
            acc.extend(rels)
        except Exception as e:
            errors.append(f"{lab}: {e}")
        job.tick(f"{lab} → {len(acc)} cumulées")
        time.sleep(1.1)

    seen, out = set(), []
    for r in acc:
        rid = r.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        y = int(r.get("year") or 0)
        if y and yf and y < yf:
            continue
        if y and yt and y > yt:
            continue
        out.append(r)
    out.sort(key=lambda r: int(r.get("year") or 0), reverse=True)
    save_json(SEARCH_RESULTS_PATH, out)
    job.finish(f"{len(scope)} labels scannés → {len(out)} sortie(s)."
               + (f" {len(errors)} erreur(s)." if errors else ""))


_DJSET_HINT_RE = re.compile(
    r"\b(dj[- ]?set|\bset\b|\bmix\b|mixtape|b2b|boiler ?room|essential mix|"
    r"live (?:set|@|at)|podcast|\bradio\b|session|warm[- ]?up|closing set|opening set|"
    r"in the mix|guest mix|resident)\b", re.I)


def _yt_descriptions(ids, api_key):
    """{video_id: description} via l'API YouTube Data (videos.list, 50 max/appel)."""
    out = {}
    if not api_key or not ids:
        return out
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            r = requests.get(f"{YOUTUBE_API}/videos",
                             params={"part": "snippet", "id": ",".join(chunk), "key": api_key},
                             timeout=20)
            if r.ok:
                for it in r.json().get("items", []):
                    out[it["id"]] = it.get("snippet", {}).get("description", "")
        except Exception:
            pass
    return out


def _yt_video_ids(source, max_n, min_seconds=2100, api_key="", require_hint=True):
    """[(video_id, source_label)] pour une source. Filtres : durée ≥ min_seconds ;
    pour une recherche texte, le nom doit figurer dans le titre ; si `require_hint`,
    un mot-clé de set (set / mix / radio / boiler room / podcast / session…) doit
    apparaître dans le titre OU la description."""
    import yt_dlp
    s = source.strip()
    is_text = not (s.startswith("@") or "youtube.com/" in s or s.startswith("http"))
    if s.startswith("@"):
        target = f"https://www.youtube.com/{s}/videos"
    elif "youtube.com/" in s:
        target = s if "/videos" in s or "list=" in s else s.rstrip("/") + "/videos"
    elif s.startswith("http"):
        target = s
    else:
        target = f"ytsearch{max(max_n * 6, 30)}:{s}"      # marge pour filtrer

    # on sépare le NOM (à exiger dans le titre) des mots-indices de recherche
    _hints = {"set", "sets", "dj", "mix", "mixe", "mixes", "radio", "boiler", "room",
              "essential", "live", "podcast", "b2b", "closing", "opening", "warmup",
              "warm", "up", "at", "the", "show", "session", "sessions"}
    name_toks = [t for t in re.split(r"[^\w]+", s.lower()) if len(t) > 1 and t not in _hints]
    name_norm = " ".join(name_toks)

    opts = {"extract_flat": True, "quiet": True, "no_warnings": True, "skip_download": True}
    cookies = os.path.join(HERE, "www.youtube.com_cookies.txt")
    if os.path.exists(cookies):
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)

    # 1) candidats : durée OK + (recherche texte) nom dans le titre
    cand, seen, stack = [], set(), [info]
    while stack and len(cand) < max_n * 3:
        node = stack.pop(0)
        if not node:
            continue
        if node.get("entries"):
            stack = list(node["entries"]) + stack
            continue
        vid = node.get("id")
        if not vid or len(vid) != 11 or vid in seen:
            continue
        seen.add(vid)
        dur = node.get("duration")
        if dur is not None and dur < min_seconds:
            continue
        title = node.get("title") or ""
        if is_text and title and name_toks:
            tl = title.lower()
            ok = (name_norm and name_norm in normalize_label(title)) or (
                sum(t in tl for t in name_toks) / len(name_toks) >= 0.6)
            if not ok:
                continue
        cand.append((vid, title))

    # 2) filtre "c'est bien un set" : mot-clé dans le titre OU la description
    #    (pour une recherche texte seulement ; une chaîne/@handle est déjà de confiance)
    if require_hint and is_text and cand:
        descs = _yt_descriptions([v for v, _ in cand], api_key)
        cand = [(v, t) for v, t in cand
                if _DJSET_HINT_RE.search(f"{t}\n{descs.get(v, '')}")]

    return [(v, s, t) for v, t in cand[:max_n]]


def _scrape_music_panel(page, video_id):
    """Cartes du panneau « Musique » d'une vidéo -> [(title, artist, album)]."""
    page.goto(f"https://www.youtube.com/watch?v={video_id}", timeout=30000)
    page.wait_for_timeout(2500)
    for t in ("text=Tout accepter", "text=Accept all", "button:has-text('Accept all')"):
        try:
            page.click(t, timeout=1500)
        except Exception:
            pass
    for sel in ("tp-yt-paper-button#expand", "#expand", "ytd-text-inline-expander #expand"):
        try:
            page.click(sel, timeout=2000)
            break
        except Exception:
            pass
    page.wait_for_timeout(1500)
    out = []
    for card in page.query_selector_all("yt-video-attribute-view-model"):
        def _txt(sel):
            el = card.query_selector(sel)
            return el.inner_text().strip() if el else None
        title = _txt("h1.ytVideoAttributeViewModelTitle")
        artist = _txt("h4.ytVideoAttributeViewModelSubtitle")
        album = _txt("span.ytVideoAttributeViewModelSecondarySubtitle")
        link_el = card.query_selector("a.ytVideoAttributeViewModelContentContainer")
        href = link_el.get_attribute("href") if link_el else None
        url = f"https://www.youtube.com{href}" if href and href.startswith("/") else href
        if title or artist:
            out.append((title, artist, album, url))
    # dédoublonnage interne (le panneau apparaît parfois 2× dans le DOM)
    seen, uniq = set(), []
    for row in out:
        if row not in seen:
            seen.add(row)
            uniq.append(row)
    return uniq


def job_ingest_djsets(job, params):
    """Extrait les tracks des sets/podcasts YouTube de DJs choisis (panneau « Musique »
    via Playwright) et les verse dans le corpus (source=djset, une ligne par couple
    track×DJ)."""
    try:
        import yt_dlp  # noqa
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        return job.finish(error=f"Dépendance manquante ({e.name}). Installe : "
                                "pip3 install yt-dlp playwright && playwright install chromium")

    cfg = cfg_load()
    token = cfg.get("token", "")
    inp = load_json(DJSET_INPUT_PATH, {})
    sources = [s.strip() for s in inp.get("sources", []) if s.strip()]
    max_per = int(inp.get("max_per_source", 25))
    min_seconds = int(inp.get("min_minutes", 35)) * 60
    require_hint = bool(inp.get("require_hint", True))
    api_key = cfg.get("youtube_api_key", "")
    deep = bool(inp.get("deep", True))
    if not sources:
        return job.finish(error="Aucune source (DJ / émission / chaîne).")

    job.msg("Listing des vidéos…")
    vids = []
    for src in sources:
        try:
            got = _yt_video_ids(src, max_per, min_seconds, api_key, require_hint)
            vids += got
            job.msg(f"{src} : {len(got)} vidéo(s)")
        except Exception as e:
            job.msg(f"{src} : erreur listing ({e})")
        if job.stopped():
            break

    seen_vids = set(load_json(DJSET_SEEN_PATH, []))
    todo = [(v, dj, vt) for v, dj, vt in vids if v not in seen_vids]
    job.st["total"] = len(todo)
    job.msg(f"{len(vids)} vidéos trouvées, {len(todo)} à traiter.")
    if not vids:
        return job.finish(error="Aucune vidéo trouvée pour ces sources. Essaie un @handle, "
                                "une URL de chaîne, ou ajoute « set »/« boiler room » au nom.")
    if not todo:
        return job.finish(f"Les {len(vids)} vidéo(s) trouvées ont déjà été traitées "
                          "(voir « Réinitialiser l'historique » pour tout re-scanner).")

    corpus = load_json(CORPUS_PATH, [])
    cache = load_json(LOOKUP_CACHE_PATH, {})
    dj_seen = {(r.get("source"), style_key(r.get("artist", "")), style_key(r.get("title", "")),
               style_key(r.get("dj", ""))) for r in corpus if r.get("source") == "djset"}
    added, now = 0, datetime.now().isoformat(timespec="seconds")

    import random
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        pg = browser.new_page()
        for i, (vid, dj, vtitle) in enumerate(todo):
            if job.stopped():
                break
            try:
                tracks = _scrape_music_panel(pg, vid)
            except Exception as e:
                tracks = []
                job.msg(f"{vid} : {e}")
            for title, artist, album, track_url in tracks:
                a = (artist or "").strip()
                t = (title or "").strip()
                if not (a or t):
                    continue
                sig = ("djset", style_key(a), style_key(t), style_key(dj))
                if sig in dj_seen:
                    continue
                dj_seen.add(sig)
                label, rid, style, vinyl_id = None, None, [], None
                hit, _ = discogs_lookup(token, a, t, cache, deep=deep)
                if hit:
                    label, rid, style = hit["label"], hit["release_id"], hit["style"]
                    vinyl_id = hit.get("vinyl_release_id")
                corpus.append({"artist": a, "title": t, "label": label, "release_id": rid,
                               "vinyl_release_id": vinyl_id, "style": style, "genre": None,
                               "url": None, "source": "djset", "dj": dj, "video": vid,
                               "set_title": vtitle, "track_url": track_url, "added_at": now})
                added += 1
            seen_vids.add(vid)
            job.tick(f"{dj} · {vid} → {len(tracks)} track(s) · +{added} au corpus")
            if (i + 1) % 8 == 0:
                save_json(CORPUS_PATH, corpus)
                save_json(LOOKUP_CACHE_PATH, cache)
                save_json(DJSET_SEEN_PATH, sorted(seen_vids))
            time.sleep(random.uniform(4, 8))
        browser.close()

    save_json(CORPUS_PATH, corpus)
    save_json(LOOKUP_CACHE_PATH, cache)
    save_json(DJSET_SEEN_PATH, sorted(seen_vids))
    job.finish(f"{job.st['done']} vidéo(s) traitées → +{added} entrées djset au corpus "
               f"(total corpus {len(corpus)}).")


def job_scan_sellers(job, params):
    """Scanne l'inventaire « For Sale » de chaque vendeur suivi, repère les annonces
    apparues depuis le dernier passage (diff sur l'id d'annonce) et les empile dans
    seller_new.json. Le 1er scan d'un vendeur sert de référence (aucune nouveauté)."""
    cfg = cfg_load()
    token = cfg.get("token", "")
    sellers = [s.strip() for s in cfg.get("sellers", []) if s and s.strip()]
    if not token:
        return job.finish(error="Pas de token Discogs.")
    if not sellers:
        return job.finish(error="Aucun vendeur suivi.")
    max_pages = int(params.get("max_pages", 5))
    seen = load_json(SELLERS_SEEN_PATH, {})
    queue = load_json(SELLERS_NEW_PATH, [])
    known = {e.get("listing_id") for e in queue}
    job.st["total"] = len(sellers)
    now = datetime.now().isoformat(timespec="seconds")
    total_new = 0
    for s in sellers:
        if job.stopped():
            break
        entry = seen.get(s) or {}
        seen_ids = set(entry.get("ids", []))
        bootstrapped = bool(entry.get("bootstrapped"))
        listings = []
        for page in range(1, max_pages + 1):
            d = discogs_get(token, f"/users/{s}/inventory",
                            {"status": "For Sale", "per_page": 100, "page": page,
                             "sort": "listed", "sort_order": "desc"})
            got = d.get("listings", [])
            listings += got
            if page >= d.get("pagination", {}).get("pages", 1) or not got:
                break
            time.sleep(1.1)
        cur_ids = [x.get("id") for x in listings if x.get("id")]
        fresh = [x for x in listings if x.get("id")
                 and x["id"] not in seen_ids and x["id"] not in known]
        if bootstrapped:
            for x in fresh:
                rel = x.get("release", {}) or {}
                desc = rel.get("description") or ""
                artist, _, title = desc.partition(" - ")
                lab = rel.get("label")
                if isinstance(lab, list):
                    lab = lab[0] if lab else None
                queue.append({
                    "listing_id": x.get("id"), "seller": s, "release_id": rel.get("id"),
                    "artist": artist.strip(), "title": (title or desc).strip(), "label": lab,
                    "style": rel.get("style") or [], "genre": rel.get("genre") or [],
                    "year": rel.get("year"), "thumb": rel.get("thumbnail"),
                    "format": rel.get("format"),
                    "price": (x.get("price") or {}).get("value"),
                    "currency": (x.get("price") or {}).get("currency"),
                    "condition": x.get("condition"),
                    "uri": x.get("uri") or (f"https://www.discogs.com/sell/item/{x.get('id')}"
                                            if x.get("id") else None),
                    "first_seen": now,
                })
                known.add(x.get("id"))
            total_new += len(fresh)
            job.msg(f"{s} : +{len(fresh)} nouvelle(s)")
        else:
            job.msg(f"{s} : {len(listings)} annonces (référence initiale)")
        merged = cur_ids + [i for i in entry.get("ids", []) if i not in set(cur_ids)]
        seen[s] = {"ids": merged[:3000], "bootstrapped": True, "last_scan": now}
        job.tick(s)
        save_json(SELLERS_SEEN_PATH, seen)
        save_json(SELLERS_NEW_PATH, queue)
    save_json(SELLERS_SEEN_PATH, seen)
    save_json(SELLERS_NEW_PATH, queue)
    job.finish(f"+{total_new} nouvelle(s) annonce(s) chez {len(sellers)} vendeur(s) · "
               f"file d'attente : {len(queue)}.")


# =================================================== enrichissement auto + canonique

PENDING_ENRICH_PATH = os.path.join(USER_DIR, "pending_enrich.json")
CANON_STATE_PATH = os.path.join(USER_DIR, "canonicalize.state.json")
VEILLE_SEEN_PATH = os.path.join(USER_DIR, "veille_seen.json")
VEILLE_NEW_PATH = os.path.join(USER_DIR, "veille_new.json")


def _resolve_entity(token, name, kind):
    """kind='label'|'artist' -> (discogs_name, id, status, candidates)."""
    d = discogs_search(token, type=kind, q=name, per_page=8)
    cands = [{"name": c.get("title"), "id": c.get("id")}
             for c in d.get("results", []) if c.get("title")]
    if not cands:
        return None, None, "not_found", []
    tgt = normalize_label(name)
    exact = next((c for c in cands if normalize_label(c["name"]) == tgt), None)
    top = exact or cands[0]
    return top["name"], top["id"], ("exact" if exact else "approx"), cands


def _profile_label(token, name):
    d = discogs_search(token, label=name, per_page=100, page=1, sort="want", sort_order="desc")
    res = d.get("results", [])
    sc, gc = Counter(), Counter()
    for x in res:
        sc.update(x.get("style") or [])
        gc.update(x.get("genre") or [])
    return {"original": name, "sampled": len(res), "style_counts": dict(sc),
            "genre_counts": dict(gc),
            "total_items": d.get("pagination", {}).get("items", len(res)),
            "profiled_at": datetime.now().isoformat(timespec="seconds")}


def _rename_in_list(lst, old, new):
    """Remplace old -> new dans lst (comparaison normalisée), dédup. True si changé."""
    if not new or normalize_label(old) == normalize_label(new):
        return False
    out, changed, seen = [], False, set()
    for x in lst:
        y = new if normalize_label(x) == normalize_label(old) else x
        changed = changed or (y != x)
        if normalize_label(y) not in seen:
            seen.add(normalize_label(y))
            out.append(y)
    lst[:] = out
    return changed


def job_enrich(job, params):
    """Draine pending_enrich.json : pour chaque label/artiste fraîchement ajouté,
    résolution vers le nom Discogs canonique (+ id) puis profilage (labels).
    Renomme au passage la base / la veille / les catégories."""
    cfg = cfg_load()
    token = cfg.get("token", "")
    pend = load_json(PENDING_ENRICH_PATH, {})
    labels = list(dict.fromkeys(pend.get("labels", [])))
    artists = list(dict.fromkeys(pend.get("artists", [])))
    if not token:
        return job.finish(error="Pas de token Discogs.")
    job.st["total"] = len(labels) + len(artists)
    if not job.st["total"]:
        return job.finish("Rien à enrichir.")

    res = load_json(RESOLVED_PATH, {})
    prof = load_json(PROFILE_PATH, {})
    ares = load_json(ARTISTS_RESOLVED_PATH, {})
    cfg_changed = False

    for name in labels:
        if job.stopped():
            break
        dn, did, status, cands = _resolve_entity(token, name, "label")
        canon = dn if (dn and status == "exact") else name
        res[normalize_label(name)] = {"original": name, "discogs_name": dn or name,
                                      "discogs_id": did, "status": status,
                                      "candidates": cands, "reviewed_by": "auto"}
        if canon != name:
            cfg_changed |= _rename_in_list(cfg.setdefault("labels", []), name, canon)
            cfg_changed |= _rename_in_list(cfg.setdefault("watchlist", []), name, canon)
            res[normalize_label(canon)] = res[normalize_label(name)]
        time.sleep(1.1)
        pk = normalize_label(canon)
        if pk not in prof:
            prof[pk] = _profile_label(token, canon)
            time.sleep(1.1)
        save_json(RESOLVED_PATH, res)
        save_json(PROFILE_PATH, prof)
        job.tick(f"label {name} → {dn or '?'} ({status})")

    for name in artists:
        if job.stopped():
            break
        dn, did, status, cands = _resolve_entity(token, name, "artist")
        ares[normalize_label(name)] = {"original": name, "discogs_name": dn or name,
                                       "discogs_id": did, "status": status,
                                       "candidates": cands}
        if dn and status == "exact" and normalize_label(dn) != normalize_label(name):
            for cid in ("1", "2", "3"):
                cfg_changed |= _rename_in_list(
                    cfg.setdefault("artist_categories", {}).setdefault(cid, []), name, dn)
        save_json(ARTISTS_RESOLVED_PATH, ares)
        job.tick(f"artiste {name} → {dn or '?'} ({status})")
        time.sleep(1.1)

    if cfg_changed:
        save_json(CONFIG_PATH, cfg)
    save_json(PENDING_ENRICH_PATH, {"labels": [], "artists": []})
    job.finish(f"+{len(labels)} label(s), +{len(artists)} artiste(s) enrichis.")


def job_canonicalize(job, params):
    """Passe unique : réécrit toute la base (labels + catégories d'artistes) avec les
    noms Discogs canoniques + ids, puis les champs artiste/label du corpus. Reprenable
    via canonicalize.state.json. `params['scope']` = 'names' | 'corpus' (défaut 'corpus')."""
    cfg = cfg_load()
    token = cfg.get("token", "")
    if not token:
        return job.finish(error="Pas de token Discogs.")
    scope = params.get("scope", "corpus")
    state = load_json(CANON_STATE_PATH, {"labels": [], "artists": [], "corpus_ids": []})
    done_l = set(state["labels"])
    done_a = set(state["artists"])
    done_c = set(state["corpus_ids"])

    res = load_json(RESOLVED_PATH, {})
    ares = load_json(ARTISTS_RESOLVED_PATH, {})
    base = list(cfg.get("labels", []))
    cats = cfg.setdefault("artist_categories", {})
    art_names = [n for cid in ("1", "2", "3") for n in cats.get(cid, [])]
    corpus = load_json(CORPUS_PATH, [])
    corpus_todo = ([r for r in corpus if r.get("release_id") and str(r["release_id"]) not in done_c]
                   if scope == "corpus" else [])
    job.st["total"] = (len(base) - len(done_l)) + (len(art_names) - len(done_a)) + len(corpus_todo)

    changed = 0
    for name in base:
        if job.stopped():
            break
        k = normalize_label(name)
        if k in done_l:
            continue
        cur = res.get(k)
        if cur and cur.get("discogs_id") and cur.get("status") in ("exact", "confirmed"):
            canon = cur.get("discogs_name") or name
        else:
            dn, did, status, cands = _resolve_entity(token, name, "label")
            res[k] = {"original": name, "discogs_name": dn or name, "discogs_id": did,
                      "status": status, "candidates": cands, "reviewed_by": "auto"}
            canon = dn if (dn and status == "exact") else name
            time.sleep(1.1)
        if _rename_in_list(base, name, canon):
            changed += 1
            res[normalize_label(canon)] = res.get(k, {})
        done_l.add(k)
        if len(done_l) % 10 == 0:
            cfg["labels"] = base
            save_json(CONFIG_PATH, cfg)
            save_json(RESOLVED_PATH, res)
            state["labels"] = sorted(done_l)
            save_json(CANON_STATE_PATH, state)
        job.tick(f"label {name} → {canon}")
    cfg["labels"] = base
    save_json(CONFIG_PATH, cfg)
    save_json(RESOLVED_PATH, res)

    for name in list(art_names):
        if job.stopped():
            break
        k = normalize_label(name)
        if k in done_a:
            continue
        cur = ares.get(k)
        if cur and cur.get("discogs_id") and cur.get("status") in ("exact", "confirmed"):
            canon = cur.get("discogs_name") or name
        else:
            dn, did, status, cands = _resolve_entity(token, name, "artist")
            ares[k] = {"original": name, "discogs_name": dn or name, "discogs_id": did,
                       "status": status, "candidates": cands}
            canon = dn if (dn and status == "exact") else name
            time.sleep(1.1)
        for cid in ("1", "2", "3"):
            _rename_in_list(cats.setdefault(cid, []), name, canon)
        done_a.add(k)
        save_json(CONFIG_PATH, cfg)
        save_json(ARTISTS_RESOLVED_PATH, ares)
        state["artists"] = sorted(done_a)
        save_json(CANON_STATE_PATH, state)
        job.tick(f"artiste {name} → {canon}")

    for i, r in enumerate(corpus_todo):
        if job.stopped():
            break
        rid = str(r["release_id"])
        d = discogs_get(token, f"/releases/{r['release_id']}")
        if d:
            arts = d.get("artists") or []
            if arts:
                r["artist"] = ", ".join(a.get("name", "").strip() for a in arts if a.get("name"))
            labs = d.get("labels") or []
            if labs:
                r["label"] = labs[0].get("name") or r.get("label")
        done_c.add(rid)
        if (i + 1) % 8 == 0:
            save_json(CORPUS_PATH, corpus)
            state["corpus_ids"] = sorted(done_c)
            save_json(CANON_STATE_PATH, state)
        job.tick(f"corpus {r.get('artist', '')[:30]}")
        time.sleep(1.1)
    save_json(CORPUS_PATH, corpus)
    state.update(labels=sorted(done_l), artists=sorted(done_a), corpus_ids=sorted(done_c))
    save_json(CANON_STATE_PATH, state)
    job.finish(f"Nettoyage : {changed} label(s) renommé(s), {len(done_a)} artiste(s), "
               f"{len(done_c)} ligne(s) de corpus canonisées.")


def _veille_search(token, rule):
    """Releases Discogs correspondant à une règle de veille (dédoublonnées par id)."""
    styles = (rule.get("styles") or [])[:3]
    genre = (rule.get("genres") or [None])[0]
    yf, yt = rule.get("year_from"), rule.get("year_to")
    yr = f"{yf}-{yt}" if (yf and yt) else (str(yf) if yf else (str(yt) if yt else None))
    fmt = "Vinyl" if rule.get("vinyl_only") else None
    labels = rule.get("labels") or []
    base = {"sort": "year", "sort_order": "desc", "per_page": 100}
    if genre:
        base["genre"] = genre
    if yr:
        base["year"] = yr
    if fmt:
        base["format"] = fmt
    out, seen = [], set()

    def go(extra, pages):
        for pg in range(1, pages + 1):
            d = discogs_search(token, page=pg, **base, **extra)
            res = d.get("results", [])
            for r in res:
                if r.get("id") and r["id"] not in seen:
                    seen.add(r["id"])
                    out.append(r)
            if pg >= d.get("pagination", {}).get("pages", 1) or not res:
                break
            time.sleep(1.1)

    if labels:
        for lab in labels:
            go({"label": lab}, 1)
            time.sleep(1.1)
    elif styles:
        for stl in styles:
            go({"style": stl}, 2)
            time.sleep(1.1)
    else:
        go({}, 3)
    return out


def job_scan_veille(job, params):
    """Scanne chaque règle de veille active (+ la règle implicite « labels suivis »),
    repère les sorties Discogs nouvelles depuis le dernier passage et les empile dans
    veille_new.json. 1er passage d'une règle = référence."""
    cfg = cfg_load()
    token = cfg.get("token", "")
    if not token:
        return job.finish(error="Pas de token Discogs.")
    rules = [dict(r) for r in cfg.get("veille_rules", []) if r.get("active", True)]
    wl = [w for w in cfg.get("watchlist", []) if w and w.strip()]
    wl_cap = int(params.get("watchlist_cap", 150))
    if wl:
        rules.insert(0, {"id": "__watchlist__",
                         "name": f"Labels suivis ({min(len(wl), wl_cap)}/{len(wl)})",
                         "labels": wl[:wl_cap], "vinyl_only": True})
    if not rules:
        return job.finish("Aucune règle de veille active.")
    seen = load_json(VEILLE_SEEN_PATH, {})
    queue = load_json(VEILLE_NEW_PATH, [])
    known = {str(e.get("release_id")) for e in queue}
    job.st["total"] = len(rules)
    now = datetime.now().isoformat(timespec="seconds")
    total_new = 0
    for rule in rules:
        if job.stopped():
            break
        rid = rule.get("id") or normalize_label(rule.get("name", "")) or "r"
        ent = seen.get(rid) or {}
        seen_ids = set(ent.get("ids", []))
        boot = bool(ent.get("bootstrapped"))
        try:
            found = _veille_search(token, rule)
        except Exception as e:
            job.msg(f"{rule.get('name')} : erreur ({e})")
            found = []
        cur_ids = [str(r.get("id")) for r in found if r.get("id")]
        fresh = [r for r in found if r.get("id") and str(r["id"]) not in seen_ids
                 and str(r["id"]) not in known]
        if boot:
            for r in fresh:
                labs = r.get("label") or []
                t = r.get("title", "")
                art, _, ti = t.partition(" - ")
                queue.append({
                    "release_id": r.get("id"), "rule": rule.get("name"),
                    "artist": art.strip() if ti else "", "title": (ti or t).strip(),
                    "label": labs[0] if labs else None,
                    "style": r.get("style") or [], "year": r.get("year"),
                    "thumb": r.get("thumb") or r.get("cover_image"),
                    "uri": f"https://www.discogs.com{r.get('uri')}" if r.get("uri") else None,
                    "first_seen": now,
                })
                known.add(str(r.get("id")))
            total_new += len(fresh)
            job.msg(f"{rule.get('name')} : +{len(fresh)} nouveauté(s)")
        else:
            job.msg(f"{rule.get('name')} : {len(found)} sorties (référence initiale)")
        merged = cur_ids + [i for i in ent.get("ids", []) if i not in set(cur_ids)]
        seen[rid] = {"ids": merged[:4000], "bootstrapped": True, "last_scan": now}
        job.tick(rule.get("name", ""))
        save_json(VEILLE_SEEN_PATH, seen)
        save_json(VEILLE_NEW_PATH, queue)
    save_json(VEILLE_SEEN_PATH, seen)
    save_json(VEILLE_NEW_PATH, queue)
    job.finish(f"+{total_new} nouveauté(s) sur {len(rules)} règle(s) · file d'attente {len(queue)}.")


JOBS = {
    "ingest_youtube": job_ingest_youtube,
    "ingest_spotify": job_ingest_spotify,
    "ingest_bandcamp": job_ingest_bandcamp,
    "fetch_collection": job_fetch_collection,
    "merge_corpus": job_merge_corpus,
    "profile_labels": job_profile_labels,
    "resolve_artists": job_resolve_artists,
    "build_graph": job_build_graph,
    "search_base": job_search_base,
    "ingest_djsets": job_ingest_djsets,
    "scan_sellers": job_scan_sellers,
    "enrich": job_enrich,
    "canonicalize": job_canonicalize,
    "scan_veille": job_scan_veille,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in JOBS:
        raise SystemExit(f"usage: python crate_jobs.py <{'|'.join(JOBS)}> [params_json]")
    name = sys.argv[1]
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else {}
    job = Job(name, total=int(params.get("_total", 0)))
    try:
        JOBS[name](job, params)
    except Exception as e:
        job.finish(error=f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
