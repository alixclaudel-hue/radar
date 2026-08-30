"""
ingest_youtube.py — importe une (ou plusieurs) playlist YouTube dans le corpus de goût
de Crate Radar, sans passer par Streamlit.

Lance-le simplement :
    python3 ingest_youtube.py
ou avec des URLs en argument :
    python3 ingest_youtube.py "https://youtube.com/playlist?list=XXXX" "https://..."

Sans argument, il prend les playlists enregistrées dans crate_radar_config.json
(clé "youtube_playlists", une URL par ligne).

Produit / met à jour, à côté du script :
  - taste_corpus.json   : lignes {artist, title, label, release_id, style, source, added_at}
  - lookup_cache.json   : cache permanent des recherches Discogs (artiste||titre -> résultat)

Reprenable : relancer ignore les titres déjà dans le corpus et réutilise le cache.
Interruptible : Ctrl+C — tout ce qui est traité est déjà sur le disque (sauvegarde
tous les 20 titres).
"""

import json
import os
import re
import sys
import time
from datetime import datetime

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "crate_radar_config.json")
CORPUS_PATH = os.path.join(HERE, "taste_corpus.json")
LOOKUP_CACHE_PATH = os.path.join(HERE, "lookup_cache.json")
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
DISCOGS_UA = "CrateRadar/1.0 +personal-use"
SAVE_EVERY = 20
DEEP = True  # 2e passe Discogs en texte libre si la 1re échoue


# ----------------------------------------------------------------- utilitaires fichiers

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


# ----------------------------------------------------------------- parsing YouTube

_NOISE = re.compile(
    r"\((official|lyric|lyrics|audio|visuali[sz]er|music|hq|hd|4k|full)\b[^)]*\)"
    r"|\bofficial (video|audio|music video)\b|\bfree (dl|download)\b|\[[^\]]*\]", re.I)

_LABEL_RES = [
    re.compile(r"under exclusive licen[sc]e to (.+?)(?:\.|;|\n|$)", re.I),
    re.compile(r"℗\s*\d{4}\s+(.+?)(?:\n|$)"),
]


_SPLIT_RE = re.compile(r"\s+[-–—―─‐‑－•·]\s+")


def parse_title(title, channel):
    t = _NOISE.sub("", title or "").strip(" -–—―─|·•")
    parts = _SPLIT_RE.split(t, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    ch = re.sub(r"\s*-\s*topic\s*$", "", channel or "", flags=re.I).strip()
    if ch and ch.lower() not in ("various artists", "va"):
        return ch, t
    return "", t


def parse_label(description):
    for rx in _LABEL_RES:
        m = rx.search(description or "")
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip(" .")
    return None


def playlist_id(url_or_id):
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url_or_id or "")
    return m.group(1) if m else (url_or_id or "").strip() or None


def fetch_playlist(pid, api_key):
    items, token = [], None
    while True:
        params = {"part": "snippet", "playlistId": pid, "maxResults": 50, "key": api_key}
        if token:
            params["pageToken"] = token
        r = requests.get(f"{YOUTUBE_API}/playlistItems", params=params, timeout=20)
        if not r.ok:
            raise SystemExit(f"YouTube API {r.status_code} : {r.text[:300]}")
        data = r.json()
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            items.append({
                "title": sn.get("title", ""),
                "channel": sn.get("videoOwnerChannelTitle") or sn.get("channelTitle", ""),
                "description": sn.get("description", ""),
            })
        token = data.get("nextPageToken")
        if not token:
            break
    return items


# ----------------------------------------------------------------- Discogs

def discogs_search(token, **params):
    params["type"] = "release"
    params["token"] = token
    r = requests.get("https://api.discogs.com/database/search", params=params,
                     headers={"User-Agent": DISCOGS_UA}, timeout=20)
    if r.status_code == 429:
        print("  … 429 Discogs, pause 60 s")
        time.sleep(60)
        return discogs_search(token, **params)
    if not r.ok:
        return {"results": []}
    return r.json()


def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower().replace("-", " ")).strip()


def discogs_lookup(artist, title, token, cache):
    """Retourne (hit, nb_appels). hit = {label, release_id, year, style} ou None."""
    k = f"{norm(artist)}||{norm(title)}"
    if k in cache:
        return cache[k], 0
    artist, title = (artist or "").strip(), (title or "").strip()
    if artist and title:
        attempts = [{"artist": artist, "track": title}]
        if DEEP:
            attempts.append({"q": f"{artist} {title}"})
    else:
        attempts = [{"q": f"{artist} {title}".strip()}]
    hit, calls = None, 0
    for i, p in enumerate(attempts):
        if i:
            time.sleep(0.3)
        calls += 1
        res = discogs_search(token, per_page=5, **p).get("results", [])
        if res:
            r0 = res[0]
            labels = r0.get("label") or []
            hit = {"label": labels[0] if labels else None, "release_id": r0.get("id"),
                   "year": r0.get("year"), "style": r0.get("style") or []}
            break
    cache[k] = hit
    return hit, calls


# ----------------------------------------------------------------- main

def main():
    cfg = load_json(CONFIG_PATH, {})
    token = cfg.get("token", "")
    api_key = cfg.get("youtube_api_key", "")
    if not token:
        raise SystemExit("Pas de token Discogs dans crate_radar_config.json")
    if not api_key:
        raise SystemExit("Pas de youtube_api_key dans crate_radar_config.json")

    urls = sys.argv[1:] or [u for u in cfg.get("youtube_playlists", "").splitlines() if u.strip()]
    pids = [p for p in (playlist_id(u) for u in urls) if p]
    if not pids:
        raise SystemExit("Aucune playlist. Passe une URL en argument ou renseigne "
                         "youtube_playlists dans la config.")

    print(f"Playlists : {pids}")
    raw = []
    for pid in pids:
        got = fetch_playlist(pid, api_key)
        print(f"  {pid} : {len(got)} vidéos")
        for it in got:
            a, t = parse_title(it["title"], it["channel"])
            if a or t:
                raw.append({"artist": a, "title": t, "label": parse_label(it["description"])})

    corpus = load_json(CORPUS_PATH, [])
    cache = load_json(LOOKUP_CACHE_PATH, {})
    seen = {(r.get("source"), norm(r.get("artist", "")), norm(r.get("title", ""))) for r in corpus}

    todo = [r for r in raw
            if ("youtube", norm(r["artist"]), norm(r["title"])) not in seen]
    print(f"\n{len(raw)} titres récupérés, {len(raw) - len(todo)} déjà dans le corpus, "
          f"{len(todo)} à traiter.\n")

    now = datetime.now().isoformat(timespec="seconds")
    added = with_label = 0
    try:
        for i, r in enumerate(todo, 1):
            label = r["label"]
            rid, style, calls = None, [], 0
            if not label and (r["artist"] or r["title"]):
                hit, calls = discogs_lookup(r["artist"], r["title"], token, cache)
                if hit:
                    label, rid, style = hit["label"], hit["release_id"], hit["style"]
            sig = ("youtube", norm(r["artist"]), norm(r["title"]))
            if sig not in seen:
                seen.add(sig)
                corpus.append({"artist": r["artist"], "title": r["title"], "label": label,
                               "release_id": rid, "style": style, "genre": None,
                               "url": None, "source": "youtube", "added_at": now})
                added += 1
                with_label += bool(label)
            tag = f"-> {label}" if label else "-> (pas de label)"
            print(f"[{i:>4}/{len(todo)}] {r['artist']} — {r['title']}  {tag}")
            if calls:
                time.sleep(max(0.2, 1.1 * calls - 0.3 * (calls - 1)))
            if i % SAVE_EVERY == 0:
                save_json(CORPUS_PATH, corpus)
                save_json(LOOKUP_CACHE_PATH, cache)
    except KeyboardInterrupt:
        print("\nInterrompu — sauvegarde de ce qui est fait…")
    finally:
        save_json(CORPUS_PATH, corpus)
        save_json(LOOKUP_CACHE_PATH, cache)

    print(f"\nTerminé. +{added} entrées ({with_label} avec label). "
          f"Corpus total : {len(corpus)}. Cache lookups : {len(cache)}.")
    print("Recharge l'onglet 🎧 de l'appli — les recommandations utilisent maintenant ce corpus.")


if __name__ == "__main__":
    main()
