"""Client API Discogs — porté de crate_radar.py."""
import time

import requests

UA = "Radar/1.0 +personal-use"
BASE = "https://api.discogs.com"


class DiscogsError(RuntimeError):
    pass


def get(path, params=None, token=""):
    params = dict(params or {})
    if token:
        params["token"] = token
    r = requests.get(f"{BASE}{path}", params=params,
                     headers={"User-Agent": UA}, timeout=20)
    if r.status_code == 401:
        raise DiscogsError("Token Discogs invalide ou manquant (401).")
    if r.status_code == 429:
        raise DiscogsError("Limite Discogs atteinte (429) — patiente une minute.")
    if not r.ok:
        raise DiscogsError(f"Erreur Discogs {r.status_code} : {r.text[:200]}")
    return r.json()


def search(token="", **params):
    params.setdefault("type", "release")
    return get("/database/search", params, token=token)


def release(release_id, token=""):
    return get(f"/releases/{release_id}", token=token)


def search_label_releases(token, label, genre="", style="", fmt="", year="", max_pages=3):
    """Sorties d'un label filtrées (pagination). Retourne la liste brute des résultats."""
    return _paged_release_search(token, "label", label, genre, style, fmt, year, max_pages)


def search_artist_releases(token, artist, genre="", style="", fmt="", year="", max_pages=3):
    """Sorties créditées à un artiste (pagination). Liste brute des résultats."""
    return _paged_release_search(token, "artist", artist, genre, style, fmt, year, max_pages)


def _paged_release_search(token, field, value, genre="", style="", fmt="", year="", max_pages=3):
    out, page = [], 1
    while page <= max_pages:
        p = {field: value, "per_page": 100, "page": page,
             "sort": "year", "sort_order": "desc"}
        for k, v in (("genre", genre), ("style", style), ("format", fmt), ("year", year)):
            if v:
                p[k] = v
        d = search(token=token, **p)
        res = d.get("results", [])
        out += res
        if page >= d.get("pagination", {}).get("pages", 1) or not res:
            break
        page += 1
        time.sleep(1.1)
    return out
