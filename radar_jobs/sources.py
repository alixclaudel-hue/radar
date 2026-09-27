"""Clients des sources externes : Discogs (API + cache de recherche), parsing
des titres YouTube, Spotify, Bandcamp (Subsonic).
"""

import hashlib
import os
import re
import time

import requests

from radar_web.radar import ytcache
from radar_jobs.common import style_key


DISCOGS_UA = "CrateRadar/1.0 +personal-use"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"
SUBSONIC_BASE = "https://bandcamp.com/api/subsonic/rest"


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


def youtube_items(pid, keys, max_pages=40):
    items, token = [], None
    for _ in range(max_pages):
        params = {"part": "snippet", "playlistId": pid, "maxResults": 50}
        if token:
            params["pageToken"] = token
        d = ytcache.request("/playlistItems", params, keys)
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
