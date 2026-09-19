#!/usr/bin/env python3
"""Banc de mesure hors-ligne pour ytcache.search_video_diag.

Rejoue le SCORING (radar_web.radar.ytcache._best_match) contre des réponses
YouTube capturées une fois (tests/fixtures/ytcache/<cas>/search.json +
videos.json) — aucun appel réseau, aucun token requis. Sert de baseline pour
itérer sur ytcache.py sans consommer de quota (méthode : cf. CLAUDE.md).

Un cas = tests/fixtures/ytcache/cases.json (artist/title/label/query,
expect="match"+expected_video_id ou expect="no_match") + un dossier du même
nom contenant les 2 réponses API brutes pour ce cas.

Pour ajouter un cas depuis une vraie recherche : scripts/capture_ytcache_fixtures.py
(sur le VPS, avec un vrai token) les capture dans un dossier séparé — --fixtures-dir
permet de les rejouer ici avant même de les intégrer au dépôt.

Usage : python scripts/bench_ytcache.py [-v] [--fixtures-dir DIR]
Sortie : code 0 si tous les cas passent, 1 sinon (utilisable par un agent en
boucle pour décider si une modification de ytcache.py est une régression).
"""
import argparse
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FIXTURES = os.path.join(REPO, "tests", "fixtures", "ytcache")  # défaut, cf. main()

# Isolé de /data réel avant tout import de radar_web (paths.py fixe DATA et
# crée des dossiers à l'import) — un banc de mesure ne doit jamais lire ni
# écrire les vraies données de l'utilisateur.
os.environ["CRATE_DATA_DIR"] = tempfile.mkdtemp(prefix="ytcache_bench_")
sys.path.insert(0, REPO)

from radar_web.radar import ytcache  # noqa: E402
from radar_web.radar.textmatch import overlap, toks  # noqa: E402

# Couverture minimale du titre de la PISTE par les métadonnées de la vidéo
# renvoyée, pour un cas `expect: "quality"`.
#
# Pourquoi un critère de qualité plutôt que l'identifiant exact attendu : les
# cas capturés depuis recos_playlist_history.json portent la vidéo choisie LE
# JOUR de l'ajout. Ce n'est pas une vérité terrain — une piste a souvent
# plusieurs uploads valables (chaîne officielle « Artiste - Topic », ré-upload
# tiers…), et les résultats YouTube dérivent avec le temps. Exiger le même
# identifiant ferait passer pour des régressions des choix au moins aussi bons
# (mesuré le 19/09 : 8 des 40 cas capturés, tous des préférences entre uploads
# équivalents, le code actuel préférant la chaîne officielle).
#
# Volontairement INDÉPENDANT du score de `_best_match` (qui mêle artiste,
# description, chaîne, bonus et pénalités) : sans ça le test passerait par
# construction, la fonction validant son propre classement.
QUALITY_TITLE_MIN = 0.6


def _load(case_id, name):
    with open(os.path.join(FIXTURES, case_id, name), encoding="utf-8") as f:
        return json.load(f)


def _video_snippet(case_id, vid):
    for it in _load(case_id, "videos.json").get("items", []):
        if it.get("id") == vid:
            return it.get("snippet", {})
    return {}


def _title_coverage(case_id, vid, title):
    """Part des mots du titre de piste couverte par le titre/la chaîne de la
    vidéo renvoyée. Mentions de version entre parenthèses retirées du titre
    demandé, comme le fait `_best_match` pour construire `want` : elles
    n'apparaissent presque jamais telles quelles côté YouTube."""
    sn = _video_snippet(case_id, vid)
    want = toks(ytcache._strip_parens(title))
    cand = toks(sn.get("title", "")) | toks(sn.get("channelTitle", ""))
    return overlap(want, cand)


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
    elif case["expect"] == "quality":
        if vid is None:
            ok, detail = False, f"aucune vidéo renvoyée ({why})"
        else:
            cov = _title_coverage(case["id"], vid, case.get("title") or "")
            ok = cov >= QUALITY_TITLE_MIN
            sn = _video_snippet(case["id"], vid)
            detail = (f"couverture titre {cov:.2f} (min {QUALITY_TITLE_MIN}) — "
                      f"{sn.get('title','?')!r} / {sn.get('channelTitle','?')!r}")
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
    global FIXTURES
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", action="store_true", help="détail de chaque cas, pas seulement les échecs")
    ap.add_argument("--fixtures-dir", default=FIXTURES,
                     help="dossier de fixtures à rejouer (défaut : tests/fixtures/ytcache/ du dépôt)")
    args = ap.parse_args()
    FIXTURES = os.path.abspath(os.path.expanduser(args.fixtures_dir))

    cases = json.load(open(os.path.join(FIXTURES, "cases.json"), encoding="utf-8"))
    results = [(c, run_case(c, args.v)) for c in cases]
    n_ok = sum(1 for _, ok in results if ok)

    # Deux familles, comptées SÉPARÉMENT : un total global masquerait que le
    # plancher passe quoi qu'il arrive (mesuré le 19/09 — matcher court-circuité
    # pour renvoyer le 1er résultat en aveugle : 25/25 du plancher passaient
    # encore). Seuls les cas adverses disent si une modification de scoring est
    # une amélioration ou une régression.
    for kind, libelle in (("adversarial", "cas adverses (discriminants)"),
                           ("floor", "plancher (non discriminant)")):
        sub = [(c, ok) for c, ok in results if c.get("kind", "floor") == kind]
        if sub:
            k_ok = sum(1 for _, ok in sub if ok)
            print(f"{k_ok}/{len(sub)} {libelle}")
    print(f"\n{n_ok}/{len(results)} cas passés ({100 * n_ok / len(results):.0f}%)")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
