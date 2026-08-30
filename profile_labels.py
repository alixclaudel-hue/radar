"""
profile_labels.py — profile les labels candidats aux recommandations (ceux qui
viennent de ta collection Discogs + du corpus YouTube/Bandcamp) : échantillonne
leurs sorties sur Discogs et agrège genres/styles -> labels_profile.json.

Lance :
    python3 profile_labels.py            # top 150 non profilés
    python3 profile_labels.py 300        # top 300

1 appel API Discogs par label (~1,1 s). Reprenable, interruptible (Ctrl+C).
Ferme l'appli Streamlit avant (elle réécrirait labels_profile.json).
"""

import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "crate_radar_config.json")
COLLECTION_CACHE_PATH = os.path.join(HERE, "collection_cache.json")
CORPUS_PATH = os.path.join(HERE, "taste_corpus.json")
RESOLVED_PATH = os.path.join(HERE, "labels_resolved.json")
PROFILE_PATH = os.path.join(HERE, "labels_profile.json")
DISCOGS_UA = "CrateRadar/1.0 +personal-use"
SAVE_EVERY = 15

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 150


def normalize_label(name):
    n = (name or "").strip().lower()
    n = re.sub(r"\s*\(\d+\)\s*$", "", n)
    n = re.sub(r"\s+", " ", n)
    return n.strip()


def load(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    cfg = load(CONFIG_PATH, {})
    token = cfg.get("token", "")
    if not token:
        raise SystemExit("Pas de token Discogs.")

    coll = load(COLLECTION_CACHE_PATH, {})
    corpus = load(CORPUS_PATH, [])
    resolved = load(RESOLVED_PATH, {})
    profile = load(PROFILE_PATH, {})

    # signal de priorité par label (clé normalisée)
    score = Counter()
    display = {}
    for k, n in coll.get("label_counts", {}).items():
        score[k] += n
        display.setdefault(k, coll.get("label_ids", {}).get(k, {}).get("name") or k)
    for k, n in coll.get("want_label_counts", {}).items():
        score[k] += 0.6 * n
        display.setdefault(k, coll.get("label_ids", {}).get(k, {}).get("name") or k)
    for r in corpus:
        lab = r.get("label")
        if not lab:
            continue
        k = normalize_label(lab)
        score[k] += 0.5
        display.setdefault(k, lab)

    candidates = [k for k, _ in score.most_common() if k not in profile]
    todo = candidates[:LIMIT]
    print(f"{len(score)} labels candidats, {len(candidates)} non profilés, "
          f"on traite les {len(todo)} premiers.\n")

    def canonical(k):
        e = resolved.get(k)
        if e and e.get("status") in ("exact", "approx", "confirmed") and e.get("discogs_name"):
            return e["discogs_name"]
        return display.get(k, k)

    done = 0
    try:
        for i, k in enumerate(todo, 1):
            name = canonical(k)
            r = requests.get("https://api.discogs.com/database/search",
                             params={"type": "release", "token": token, "label": name,
                                     "per_page": 100, "page": 1, "sort": "want",
                                     "sort_order": "desc"},
                             headers={"User-Agent": DISCOGS_UA}, timeout=20)
            if r.status_code == 429:
                print("  … 429, pause 60 s"); time.sleep(60); continue
            res = r.json().get("results", []) if r.ok else []
            sc, gc = Counter(), Counter()
            for x in res:
                sc.update(x.get("style") or [])
                gc.update(x.get("genre") or [])
            profile[k] = {
                "original": display.get(k, k), "sampled": len(res),
                "style_counts": dict(sc), "genre_counts": dict(gc),
                "total_items": r.json().get("pagination", {}).get("items", len(res)) if r.ok else 0,
                "profiled_at": datetime.now().isoformat(timespec="seconds"),
            }
            done += 1
            top = ", ".join(f"{s}({n})" for s, n in sc.most_common(4))
            print(f"[{i:>3}/{len(todo)}] {name:32.32} {len(res):>3} sorties  {top}")
            if i % SAVE_EVERY == 0:
                save(PROFILE_PATH, profile)
            time.sleep(1.1)
    except KeyboardInterrupt:
        print("\nInterrompu — sauvegarde…")
    finally:
        save(PROFILE_PATH, profile)

    print(f"\n+{done} labels profilés. Total : {len(profile)}. Relance l'appli.")


if __name__ == "__main__":
    main()
