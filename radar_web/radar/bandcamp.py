"""Recherche Bandcamp via l'API d'autocomplétion publique du site.

Bandcamp n'a plus d'API publique depuis 2022, mais l'endpoint qui alimente sa
propre barre de recherche (`bcsearch_public_api`) répond en JSON, sans auth ni
Cloudflare. On l'utilise comme pour YouTube : trouver le meilleur résultat et
pointer dessus, sinon retomber sur la page de recherche.

Cache partagé : un couple (artiste, titre) -> URL Bandcamp change rarement.
"""
import json
import os
import re
import time
from urllib.parse import quote_plus, urlparse

import requests

from . import paths

CACHE_PATH = os.path.join(paths.SHARED_DIR, "bandcamp_cache.json")
API = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"
UA = "Mozilla/5.0 (Radar; +personal-use)"
TTL = 30 * 86400
MIN_SCORE = 0.62

_NOISE = re.compile(
    r"[\(\[][^\)\]]*[\)\]]"                       # (Original Mix), [2020 Remaster]…
    r"|\b(original|extended|radio|club|vocal|instrumental|dub)\s+(mix|edit|version)\b"
    r"|\bremaster(ed)?\b|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bvip\b", re.I)

# mots qui trahissent une autre version que l'originale (remix, bootleg, edit…)
_ALT_VERSION = {"remix", "rework", "bootleg", "edit", "flip", "refix", "vip",
                "reprise", "mashup", "mix", "version", "rmx", "dub"}
# jetons courts / de numérotation à ignorer (mouvements, parties)
_FILLER = {"pt", "part", "i", "ii", "iii", "iv", "v", "vi", "l", "ll", "lll", "a", "b"}


def _toks(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _slug(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _host_matches(url, *names):
    """Le sous-domaine <x>.bandcamp.com contient-il un fragment ≥4 car. d'un des
    noms ? Les comptes officiels sont `nomartiste.bandcamp.com` / `nomlabel...`."""
    host = _slug(urlparse(url or "").hostname or "")
    for n in names:
        sl = _slug(n)
        if len(sl) >= 4 and (sl in host or host in sl):
            return True
        for w in re.findall(r"[a-z0-9]{4,}", (n or "").lower()):
            if w in host:
                return True
    return False


def _clean(s):
    return re.sub(r"\s+", " ", _NOISE.sub(" ", s or "")).strip()


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


def _api(text, kind):
    r = requests.post(API, timeout=12,
                      headers={"User-Agent": UA, "Content-Type": "application/json"},
                      json={"search_text": text, "search_filter": kind,
                            "full_page": False, "fan_id": None})
    r.raise_for_status()
    return (r.json().get("auto") or {}).get("results") or []


def search_url(artist, title, kind="t"):
    q = f"{artist or ''} {title or ''}".strip()
    return f"https://bandcamp.com/search?q={quote_plus(q)}&item_type={'t' if kind == 't' else 'a'}"


def search(artist, title, kind="t", label=""):
    """kind : 't' (track) ou 'a' (album). -> {url,name,band,album,img} ou None."""
    artist, title = (artist or "").strip(), (title or "").strip()
    kind = "t" if kind == "t" else "a"
    if not title:
        return None

    ck = f"{kind}|{artist.lower()}|{title.lower()}"
    cache = _load()
    hit = cache.get(ck)
    if hit is not None and time.time() - hit.get("ts", 0) < TTL:
        return hit.get("v")

    q = _clean(f"{artist} {title}") or f"{artist} {title}".strip()
    want = _toks(f"{artist} {title}")
    a_toks = _toks(artist)
    l_toks = _toks(label) - {"records", "record", "recordings", "music", "ltd"}
    t_toks = _toks(_clean(title)) - _FILLER
    try:
        results = _api(q, kind)
    except (requests.RequestException, ValueError):
        return None                      # échec réseau : ne pas mettre en cache

    best, best_sc = None, 0.0
    for r in results:
        if r.get("type") != kind:
            continue
        band_toks = _toks(r.get("band_name", ""))
        name_toks = _toks(r.get("name", ""))
        album_toks = _toks(r.get("album_name", ""))
        cand = band_toks | name_toks
        if not cand or not want:
            continue
        sc = len(want & cand) / len(want)
        url = r.get("item_url_path") or r.get("item_url_root") or ""
        # Compte de confiance : celui de l'artiste ou de son label, ET dont le
        # sous-domaine bandcamp.com le confirme. Sinon (compte tiers, ou compte
        # juste nommé « Burial » avec une URL au hasard) c'est presque toujours un
        # bootleg / rip / edit, dont les métadonnées imitent pourtant « Artiste -
        # Titre » à la perfection.
        official = _host_matches(url, artist) or (bool(l_toks) and _host_matches(url, label))
        if not official:
            sc *= 0.35
        # titre truffé de mots absents de la demande -> autre version
        foreign = name_toks - want - _FILLER
        if foreign & _ALT_VERSION or len(foreign) >= 2:
            sc *= 0.5
        # le cœur du titre doit être présent
        if t_toks and len(t_toks & name_toks) / len(t_toks) < 0.5:
            sc *= 0.5
        if l_toks and (l_toks & album_toks):
            sc += 0.1
        if sc > best_sc:
            best, best_sc = r, sc

    out = None
    if best and best_sc >= MIN_SCORE:
        out = {"url": best.get("item_url_path") or best.get("item_url_root"),
               "name": best.get("name"), "band": best.get("band_name"),
               "album": best.get("album_name"), "img": best.get("img")}
    if len(cache) > 5000:
        cache = {}
    cache[ck] = {"ts": time.time(), "v": out}
    _save(cache)
    return out
