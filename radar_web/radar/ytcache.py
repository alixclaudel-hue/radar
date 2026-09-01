"""Cache partagé + cascade de clés pour l'API YouTube Data (Option A, étape 5a).

- Cache : /data/shared/youtube_cache.json (données publiques, neutres). Économise
  surtout les appels `search` (100 unités de quota chacun).
- Cascade : on essaie la clé perso de l'utilisateur puis la clé de l'appli
  (`YOUTUBE_API_KEY`) ; sur 403 quotaExceeded on passe à la suivante.
"""
import hashlib
import json
import os
import time

import requests

from . import paths

CACHE_PATH = os.path.join(paths.SHARED_DIR, "youtube_cache.json")
API = "https://www.googleapis.com/youtube/v3"
_QUOTA_REASONS = {"quotaexceeded", "dailylimitexceeded", "ratelimitexceeded",
                  "userratelimitexceeded"}


class QuotaExhausted(RuntimeError):
    pass


def youtube_keys(cfg):
    """Clés à essayer, dans l'ordre : perso (protège le pot commun) puis appli."""
    out = []
    for k in ((cfg or {}).get("youtube_api_key"), os.environ.get("YOUTUBE_API_KEY")):
        k = (k or "").strip()
        if k and k not in out:
            out.append(k)
    return out


def _load():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(d):
    os.makedirs(paths.SHARED_DIR, exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, CACHE_PATH)


def cache_get(key, max_age):
    e = _load().get(key)
    if e and (time.time() - e.get("ts", 0)) < max_age:
        return e.get("v")
    return None


def cache_put(key, value):
    d = _load()
    d[key] = {"ts": time.time(), "v": value}
    if len(d) > 20000:
        for k in sorted(d, key=lambda k: d[k].get("ts", 0))[:5000]:
            d.pop(k, None)
    _save(d)


def _reason(resp):
    try:
        errs = resp.json().get("error", {}).get("errors", [])
        return {e.get("reason", "").lower() for e in errs}
    except ValueError:
        return set()


def request(path, params, keys, timeout=15):
    """GET {API}{path} en essayant chaque clé. QuotaExhausted si toutes en 403 quota."""
    last = None
    for k in keys:
        r = requests.get(f"{API}{path}", params={**params, "key": k}, timeout=timeout)
        if r.ok:
            return r.json()
        last = r
        if r.status_code == 403 and _reason(r) & _QUOTA_REASONS:
            continue                      # clé épuisée -> suivante
        raise RuntimeError(f"YouTube {r.status_code}: {r.text[:200]}")
    if last is not None and last.status_code == 403:
        raise QuotaExhausted("Quota YouTube épuisé (toutes les clés). "
                             "Réessaie demain ou ajoute ta clé perso dans « Mieux connaître ton univers ».")
    raise RuntimeError("Aucune clé YouTube utilisable.")


def search_video(query, keys, ttl=7 * 86400):
    """videoId de la 1re vidéo pour `query` (mise en cache). None si rien / pas de clé."""
    q = " ".join((query or "").split())
    if not q or not keys:
        return None
    ckey = "search:" + hashlib.sha1(q.encode()).hexdigest()[:20]
    hit = cache_get(ckey, ttl)
    if hit is not None:
        return hit or None
    d = request("/search", {"part": "id", "type": "video", "maxResults": 1, "q": q}, keys)
    items = d.get("items", [])
    vid = (items[0].get("id", {}) or {}).get("videoId", "") if items else ""
    cache_put(ckey, vid)
    return vid or None
