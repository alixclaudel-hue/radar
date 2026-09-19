#!/usr/bin/env python3
"""Banc de mesure hors-ligne pour ytcache.search_video_diag.

Rejoue le SCORING (radar_web.radar.ytcache._best_match) contre des réponses
YouTube capturées une fois (tests/fixtures/ytcache/<cas>/search.json +
videos.json) — aucun appel réseau, aucun token requis. Sert de baseline pour
itérer sur ytcache.py sans consommer de quota (méthode : cf. CLAUDE.md).

Un cas = tests/fixtures/ytcache/cases.json (artist/title/label/query,
expect="match"+expected_video_id ou expect="no_match") + un dossier du même
nom contenant les 2 réponses API brutes pour ce cas.

Pour ajouter un cas depuis une vraie recherche (sur le VPS, avec un vrai
token) : capturer les JSON bruts de /search et /videos pour la requête, les
déposer dans un nouveau dossier, ajouter l'entrée dans cases.json.

Usage : python scripts/bench_ytcache.py [-v]
Sortie : code 0 si tous les cas passent, 1 sinon (utilisable par un agent en
boucle pour décider si une modification de ytcache.py est une régression).
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FIXTURES = os.path.join(REPO, "tests", "fixtures", "ytcache")

# Isolé de /data réel avant tout import de radar_web (paths.py fixe DATA et
# crée des dossiers à l'import) — un banc de mesure ne doit jamais lire ni
# écrire les vraies données de l'utilisateur.
os.environ["CRATE_DATA_DIR"] = tempfile.mkdtemp(prefix="ytcache_bench_")
sys.path.insert(0, REPO)

from radar_web.radar import ytcache  # noqa: E402


def _load(case_id, name):
    with open(os.path.join(FIXTURES, case_id, name), encoding="utf-8") as f:
        return json.load(f)


def _mock_request(case_id):
    """Remplace ytcache.request : sert les fixtures du cas au lieu d'appeler
    l'API réelle. Route sur le chemin (/search vs /videos), ignore les
    paramètres exacts (une fixture répond à toute requête pour CE cas)."""
    search_resp = _load(case_id, "search.json")
    videos_resp = _load(case_id, "videos.json")

    def fake(path, params, keys, timeout=15):
        if path == "/search":
            return search_resp
        if path == "/videos":
            return videos_resp
        raise AssertionError(f"chemin non mocké : {path}")
    return fake


def run_case(case, verbose=False):
    real_request = ytcache.request
    ytcache.request = _mock_request(case["id"])
    try:
        vid, why = ytcache.search_video_diag(
            case["query"], keys=["fixture"],
            artist=case.get("artist"), title=case.get("title"),
            label=case.get("label", ""), force=True,
        )
    finally:
        ytcache.request = real_request

    if case["expect"] == "match":
        ok = vid == case["expected_video_id"]
        detail = f"attendu={case['expected_video_id']!r} obtenu={vid!r}"
    elif case["expect"] == "no_match":
        ok = vid is None
        detail = f"attendu=aucune vidéo obtenu={vid!r} ({why})"
    else:
        raise ValueError(f"expect inconnu : {case['expect']!r}")

    if verbose or not ok:
        status = "OK  " if ok else "FAIL"
        print(f"[{status}] {case['id']} — {detail}")
    return ok


def main():
    verbose = "-v" in sys.argv[1:]
    cases = json.load(open(os.path.join(FIXTURES, "cases.json"), encoding="utf-8"))
    results = [(c["id"], run_case(c, verbose)) for c in cases]
    n_ok = sum(1 for _, ok in results if ok)
    print(f"\n{n_ok}/{len(results)} cas passés ({100 * n_ok / len(results):.0f}%)")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
