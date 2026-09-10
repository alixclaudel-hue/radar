"""Recherche Volumo (boutique DJ électro) via l'API JSON qu'utilise leur propre
site (`/api/v1/{tracks,albums}/search`) — endpoint public, pas de clé, pas
documenté officiellement mais stable (utilisé par leur frontend Next.js).

Contrairement à Bandcamp, pas de repli "page de recherche" : si aucun résultat
suffisamment confiant, on ne propose pas de lien du tout — c'est la demande
explicite du retour utilisateur (pas de lien vers une boutique qui ne vend pas
le disque).

Cache partagé : un couple (artiste, titre) -> résultat change rarement.
"""
import os
import time

import requests

from . import paths, store
from .textmatch import overlap, toks

CACHE_PATH = os.path.join(paths.SHARED_DIR, "volumo_cache.json")
API = "https://volumo.com/api/v1"
UA = "Mozilla/5.0 (Radar; +personal-use)"
TTL = 30 * 86400
MIN_SCORE = 0.62


def _api(kind, query):
    endpoint = "tracks/search" if kind == "t" else "albums/search"
    r = requests.get(f"{API}/{endpoint}", timeout=12,
                      headers={"User-Agent": UA, "Accept": "application/json"},
                      params={"query": query, "limit": 10})
    r.raise_for_status()
    return r.json().get("items") or []


def search(artist, title, kind="a"):
    """kind : 't' (track) ou 'a' (album). -> {"url", "name", "band"} ou None."""
    artist, title = (artist or "").strip(), (title or "").strip()
    kind = "t" if kind == "t" else "a"
    if not title:
        return None

    ck = f"{kind}|{artist.lower()}|{title.lower()}"
    cache = store.load(CACHE_PATH, {})
    hit = cache.get(ck)
    if hit is not None and time.time() - hit.get("ts", 0) < TTL:
        return hit.get("v")

    want = toks(f"{artist} {title}")
    try:
        results = _api(kind, f"{artist} {title}".strip())
    except (requests.RequestException, ValueError):
        return None                      # échec réseau : ne pas mettre en cache

    best, best_sc = None, 0.0
    for r in results:
        band_toks = set()
        for a in r.get("artists") or []:
            band_toks |= toks(a.get("name"))
        cand = band_toks | toks(r.get("title"))
        sc = overlap(want, cand)
        if sc > best_sc:
            best, best_sc = r, sc

    out = None
    if best and best_sc >= MIN_SCORE:
        if kind == "t":
            url = f"https://volumo.com/track/{best['id']}"
            band = ", ".join(a.get("name", "") for a in best.get("artists") or [])
        else:
            url = f"https://volumo.com/album/{best['icpn']}"
            band = ", ".join(a.get("name", "") for a in best.get("artists") or [])
        out = {"url": url, "name": best.get("title"), "band": band}
    if len(cache) > 5000:
        cache = {}
    cache[ck] = {"ts": time.time(), "v": out}
    store.save(CACHE_PATH, cache, indent=None)
    return out
