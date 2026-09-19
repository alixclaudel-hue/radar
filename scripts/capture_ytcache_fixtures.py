#!/usr/bin/env python3
"""Capture des fixtures ytcache RÉELLES — à lancer SUR LE VPS (vrai token,
vrai réseau, coûte du quota YouTube réel). Contrepartie de scripts/bench_ytcache.py
(hors-ligne) : ce script-ci fait les vrais appels une fois, bench_ytcache.py les
rejoue ensuite gratuitement à volonté.

Écrit UNIQUEMENT dans --out, jamais dans le dépôt (~/radar est en lecture
seule pour une session VPS, cf. .claude/skills/vps-ops/SKILL.md) : --out doit
pointer hors du dépôt, ex. ~/radar-diag/ytcache-fixtures. La session cloud
récupère ensuite ce dossier pour l'intégrer à tests/fixtures/ytcache/ via PR.

Source des cas : recos_playlist_history.json (pistes déjà ajoutées à RECOS RADAR
un jour, donc déjà un match jugé correct à l'époque) — sert de corpus de
non-régression : un futur changement de ytcache.py ne doit pas casser ces
matches déjà trouvés.

Usage (sur le VPS) :
    python scripts/capture_ytcache_fixtures.py --out ~/radar-diag/ytcache-fixtures --limit 15
Puis commit/push de ~/radar-diag/ytcache-fixtures dans le dépôt radar-diag
(pas de secret dedans — uniquement des réponses API publiques : titres/
chaînes/ids de vidéos).
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
REPO_FIXTURES = os.path.join(REPO, "tests", "fixtures", "ytcache")
sys.path.insert(0, REPO)

from radar_web.radar import paths, store, ytcache  # noqa: E402


# Artistes sans valeur d'identification : le pipeline les écarte en amont depuis
# les pts 47/49 (`job_scan_recos` filtre les pistes sans artiste exploitable),
# mais l'historique RECOS en contient encore d'AVANT ce correctif — 15 des 40
# premiers cas capturés le 19/09. Les garder ferait tester un chemin mort, et
# figerait comme « attendu » le comportement que le pt 27 a justement corrigé.
# Doit rester aligné sur `crate_jobs._PLACEHOLDER_ARTISTS`.
_PLACEHOLDER_ARTISTS = {"various", "various artists", "va", "v/a",
                        "unknown artist", "unknown", "no artist"}


def _is_placeholder_artist(name):
    return re.sub(r"\s*\(\d+\)\s*$", "", (name or "").strip()).lower() in _PLACEHOLDER_ARTISTS


def _slug(artist, title):
    s = re.sub(r"[^a-z0-9]+", "_", f"{artist}_{title}".lower()).strip("_")
    return (s[:60] or "case")


def capture_one(out_dir, case_id, artist, title, keys):
    query = f"{artist} {title}"
    search_resp = ytcache.request(
        "/search", {"part": "id", "type": "video", "maxResults": 15, "q": query}, keys)
    ids = [((it.get("id") or {}).get("videoId") or "") for it in search_resp.get("items", [])]
    ids = [i for i in ids if i]
    if not ids:
        return None, "aucun résultat de recherche"
    videos_resp = ytcache.request("/videos", {"part": "snippet,status", "id": ",".join(ids)}, keys)

    d = os.path.join(out_dir, case_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "search.json"), "w", encoding="utf-8") as f:
        json.dump(search_resp, f, ensure_ascii=False, indent=2)
    with open(os.path.join(d, "videos.json"), "w", encoding="utf-8") as f:
        json.dump(videos_resp, f, ensure_ascii=False, indent=2)
    return query, None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True,
                     help="dossier de sortie, HORS du dépôt (ex. ~/radar-diag/ytcache-fixtures) "
                          "— refusé s'il pointe dans ce dépôt")
    ap.add_argument("--limit", type=int, default=10,
                     help="nombre de nouveaux cas à capturer (défaut 10 — chaque cas "
                          "coûte ~101 unités de quota YouTube réel : 100 pour /search + 1 pour /videos)")
    args = ap.parse_args()

    out_dir = os.path.abspath(os.path.expanduser(args.out))
    if out_dir == REPO or out_dir.startswith(REPO + os.sep):
        sys.exit(f"--out ne doit jamais pointer dans le dépôt ({REPO}) — "
                  "une session VPS n'écrit jamais dans ~/radar.")
    os.makedirs(out_dir, exist_ok=True)

    cfg = store.load_config(paths.DEFAULT_UID)
    keys = ytcache.youtube_keys(cfg)
    if not keys:
        sys.exit("Aucune clé YouTube utilisable (config ou variable YOUTUBE_API_KEY).")

    history_path = paths.user_paths(paths.DEFAULT_UID).recos_history
    if not os.path.isfile(history_path):
        sys.exit(f"Pas d'historique RECOS trouvé : {history_path}")
    history = json.load(open(history_path, encoding="utf-8"))
    candidates = [h for h in history
                  if isinstance(h, dict) and h.get("artist") and h.get("title") and h.get("video_id")
                  and not _is_placeholder_artist(h["artist"])]
    print(f"{len(candidates)} entrée(s) exploitable(s) dans l'historique RECOS "
          f"(sur {len(history)} au total).")

    # dédoublonnage contre les cas déjà dans le dépôt (lecture seule, OK) ET
    # contre un run précédent de ce script dans --out (accumulation entre runs)
    repo_cases_path = os.path.join(REPO_FIXTURES, "cases.json")
    repo_ids = {c["id"] for c in json.load(open(repo_cases_path, encoding="utf-8"))} \
        if os.path.isfile(repo_cases_path) else set()
    out_cases_path = os.path.join(out_dir, "cases.json")
    out_cases = json.load(open(out_cases_path, encoding="utf-8")) if os.path.isfile(out_cases_path) else []
    existing_ids = repo_ids | {c["id"] for c in out_cases}

    n_captured = 0
    for h in candidates:
        if n_captured >= args.limit:
            break
        case_id = _slug(h["artist"], h["title"])
        if case_id in existing_ids:
            continue
        query, err = capture_one(out_dir, case_id, h["artist"], h["title"], keys)
        if err:
            print(f"  [SKIP] {case_id} — {err}")
            continue
        out_cases.append({
            "id": case_id, "artist": h["artist"], "title": h["title"], "label": "",
            "query": query, "expect": "quality", "kind": "floor",
            "historical_video_id": h["video_id"],
            "comment": ("capturé depuis recos_playlist_history.json ; "
                        "historical_video_id est indicatif (choix du jour de l'ajout), "
                        "pas une vérité terrain"),
        })
        existing_ids.add(case_id)
        n_captured += 1
        print(f"  [OK] {case_id}")
        time.sleep(0.3)  # ménage le débit API, pas seulement le quota journalier

    with open(out_cases_path, "w", encoding="utf-8") as f:
        json.dump(out_cases, f, ensure_ascii=False, indent=2)
    print(f"\n{n_captured} nouveau(x) cas capturé(s) dans {out_dir}. "
          f"{len(out_cases)} au total dans ce dossier (hors dépôt).")
    print(f"Vérifie avec : python scripts/bench_ytcache.py --fixtures-dir {out_dir} -v")


if __name__ == "__main__":
    main()
