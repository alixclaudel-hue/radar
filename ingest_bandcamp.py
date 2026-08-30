"""
ingest_bandcamp.py — importe ta collection Bandcamp (via l'API Subsonic) dans le
corpus de goût de Crate Radar, sans passer par Streamlit.

Pré-requis : dans crate_radar_config.json, les clés
    "bandcamp_sub_user" et "bandcamp_sub_pass"
(générées sur Bandcamp → réglages → Subsonic).

Lance :
    python3 ingest_bandcamp.py

Produit / met à jour, à côté du script :
  - taste_corpus.json   (source "bandcamp")
  - lookup_cache.json   (cache Discogs partagé avec ingest_youtube.py)

Reprenable et interruptible (Ctrl+C) — sauvegarde tous les 20 albums.
"""

import hashlib
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
SUBSONIC_BASE = "https://bandcamp.com/api/subsonic/rest"
DISCOGS_UA = "CrateRadar/1.0 +personal-use"
SAVE_EVERY = 20
DEEP = True


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


def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower().replace("-", " ")).strip()


# ----------------------------------------------------------------- Bandcamp / Subsonic

def subsonic(method, user, password, **params):
    salt = os.urandom(8).hex()
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    p = {"u": user, "t": token, "s": salt, "v": "1.16.1", "c": "CrateRadar",
         "f": "json", **params}
    r = requests.get(f"{SUBSONIC_BASE}/{method}", params=p, timeout=25)
    if not r.ok:
        raise SystemExit(f"Subsonic {method} — HTTP {r.status_code} : {r.text[:200]}")
    body = r.json().get("subsonic-response", {})
    if body.get("status") != "ok":
        raise SystemExit(f"Subsonic {method} : {body.get('error', {}).get('message', body)}")
    return body


def bandcamp_albums(user, password):
    out, offset, size = [], 0, 500
    while True:
        body = subsonic("getAlbumList2", user, password,
                        type="alphabeticalByName", size=size, offset=offset)
        al = body.get("albumList2", {}).get("album", [])
        for a in al:
            out.append({"artist": (a.get("artist") or "").strip(),
                        "title": (a.get("name") or "").strip(),
                        "genre": a.get("genre")})
        if len(al) < size:
            break
        offset += size
        time.sleep(0.5)
    return out


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


def discogs_lookup(artist, title, token, cache):
    """kind=release (album). Retourne (hit, nb_appels)."""
    k = f"{norm(artist)}||{norm(title)}"
    if k in cache:
        return cache[k], 0
    artist, title = (artist or "").strip(), (title or "").strip()
    if artist and title:
        attempts = [{"artist": artist, "release_title": title}]
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
    u = cfg.get("bandcamp_sub_user", "")
    pw = cfg.get("bandcamp_sub_pass", "")
    if not token:
        raise SystemExit("Pas de token Discogs dans crate_radar_config.json")
    if not (u and pw):
        raise SystemExit("Renseigne bandcamp_sub_user / bandcamp_sub_pass dans la config "
                         "(Bandcamp → réglages → Subsonic).")

    print("Lecture de la collection Bandcamp…")
    albums = bandcamp_albums(u, pw)
    print(f"{len(albums)} albums dans la collection.\n")

    corpus = load_json(CORPUS_PATH, [])
    cache = load_json(LOOKUP_CACHE_PATH, {})
    seen = {(r.get("source"), norm(r.get("artist", "")), norm(r.get("title", ""))) for r in corpus}
    todo = [a for a in albums
            if ("bandcamp", norm(a["artist"]), norm(a["title"])) not in seen]
    print(f"{len(albums) - len(todo)} déjà dans le corpus, {len(todo)} à traiter.\n")

    now = datetime.now().isoformat(timespec="seconds")
    added = with_label = 0
    try:
        for i, a in enumerate(todo, 1):
            label, rid, style, calls = None, None, [], 0
            if a["artist"] or a["title"]:
                hit, calls = discogs_lookup(a["artist"], a["title"], token, cache)
                if hit:
                    label, rid, style = hit["label"], hit["release_id"], hit["style"]
            sig = ("bandcamp", norm(a["artist"]), norm(a["title"]))
            if sig not in seen:
                seen.add(sig)
                corpus.append({"artist": a["artist"], "title": a["title"], "label": label,
                               "release_id": rid, "style": style, "genre": a.get("genre"),
                               "url": None, "source": "bandcamp", "added_at": now})
                added += 1
                with_label += bool(label)
            print(f"[{i:>3}/{len(todo)}] {a['artist']} — {a['title']}  "
                  f"{'-> ' + label if label else '-> (pas de label)'}")
            if calls:
                time.sleep(max(0.2, 1.1 * calls - 0.3 * (calls - 1)))
            if i % SAVE_EVERY == 0:
                save_json(CORPUS_PATH, corpus)
                save_json(LOOKUP_CACHE_PATH, cache)
    except KeyboardInterrupt:
        print("\nInterrompu — sauvegarde…")
    finally:
        save_json(CORPUS_PATH, corpus)
        save_json(LOOKUP_CACHE_PATH, cache)

    print(f"\nTerminé. +{added} entrées ({with_label} avec label). "
          f"Corpus total : {len(corpus)}. Cache lookups : {len(cache)}.")
    print("Recharge l'onglet 🎧 de l'appli.")


if __name__ == "__main__":
    main()
