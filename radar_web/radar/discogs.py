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
    out, page = [], 1
    while page <= max_pages:
        p = {"label": label, "per_page": 100, "page": page,
             "sort": "year", "sort_order": "desc"}
        if genre:
            p["genre"] = genre
        if style:
            p["style"] = style
        if fmt:
            p["format"] = fmt
        if year:
            p["year"] = year
        d = search(token=token, **p)
        res = d.get("results", [])
        out += res
        if page >= d.get("pagination", {}).get("pages", 1) or not res:
            break
        page += 1
        time.sleep(1.1)
    return out
