"""Fonctionnalités mises en pause le 2026-09-17 (décisions utilisateur) :
« Nouveautés » (/veille) et « Vendeurs » (catalogue partagé + regroupement de
la wantlist par vendeur, ce dernier se faisant mieux directement sur Discogs).

Vérifie que les interrupteurs de `radar/features.py` suffisent : routes en 404,
sections de gabarit masquées, jobs non lançables (`scan_veille`/`scan_sellers`
et `scan_catalog`), boucle hebdo du worker désarmée même si RADAR_SELLER_SCAN=1
— et que le reste de l'appli (Recherche, Reco Radar, Réglages, leurs jobs)
n'est pas touché.

Lancer : python3 -m unittest tests.test_features_paused -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `radar_web.app` lit les chemins de données à l'import (cf. test_cart_add_vinyl_fallback).
_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

from fastapi.testclient import TestClient  # noqa: E402

from radar_web import app as appmod, worker as workermod  # noqa: E402
from radar_web.radar import features  # noqa: E402

SELLERS_ROUTES = [
    ("get", "/cart/sellers"),
    ("get", "/sellers/catalog"),
    ("post", "/sellers/catalog/toggle"),
    ("post", "/sellers/catalog/add"),
]

VEILLE_ROUTES = [
    ("get", "/veille"),
    ("get", "/inbox/veille"),
    ("get", "/inbox/sellers"),
    ("get", "/inbox/meta?ids=123"),
    ("post", "/inbox/veille/clear"),
    ("post", "/inbox/veille/dismiss"),
    ("post", "/veille/rules"),
    ("post", "/sellers/add"),
    ("post", "/sellers/remove"),
]


class FeaturesPausedTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        p = mock.patch.object(appmod.websession, "dev_mode", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_toutes_les_routes_nouveautes_repondent_404(self):
        for method, url in VEILLE_ROUTES:
            with self.subTest(url=url):
                r = getattr(self.client, method)(url)
                self.assertEqual(r.status_code, 404, url)

    def test_les_jobs_des_deux_fonctionnalites_ne_sont_plus_lancables(self):
        for job in ("scan_veille", "scan_sellers", "scan_catalog"):
            self.assertNotIn(job, appmod.VALID_JOBS)
        # les jobs des autres pages restent valides (la pause ne déborde pas).
        for job in ("scan_recos", "publish_recos", "fetch_collection", "build_graph"):
            self.assertIn(job, appmod.VALID_JOBS)

    def test_les_gardes_laissent_repasser_les_drapeaux_a_True(self):
        # Moitié runtime des interrupteurs (les routes). L'autre moitié
        # (VALID_JOBS, globaux Jinja) est calculée à l'import : remettre un
        # drapeau à True demande un redémarrage de l'appli, pas plus.
        with mock.patch.object(features, "VEILLE_ENABLED", True):
            appmod._veille_guard()
        with mock.patch.object(features, "SELLERS_ENABLED", True):
            appmod._sellers_guard()

    def test_toutes_les_routes_vendeurs_repondent_404(self):
        for method, url in SELLERS_ROUTES:
            with self.subTest(url=url):
                r = getattr(self.client, method)(url)
                self.assertEqual(r.status_code, 404, url)

    def test_gabarits_sans_trace_des_deux_fonctionnalites(self):
        self.assertIs(appmod.templates.env.globals["veille_enabled"], False)
        self.assertIs(appmod.templates.env.globals["sellers_enabled"], False)
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('href="/veille"', r.text)
        self.assertNotIn("Nouveautés", r.text)
        self.assertNotIn("Catalogue de vendeurs", r.text)
        # les autres entrées de nav et sections de Réglages sont toujours là.
        for href in ('href="/search"', 'href="/univers"', 'href="/reco-radar"'):
            self.assertIn(href, r.text)
        self.assertIn("Référentiel Discogs local", r.text)

    def test_la_boucle_hebdo_du_worker_ignore_RADAR_SELLER_SCAN(self):
        # Le worker n'enfile PAS via VALID_JOBS : sans cette garde, la boucle
        # continuerait de lancer scan_catalog malgré la pause côté web.
        with mock.patch.dict(os.environ, {"RADAR_SELLER_SCAN": "1"}), \
                mock.patch.object(workermod.jobs, "launch") as launch, \
                mock.patch.object(workermod.sellers, "load_catalog", return_value={}):
            workermod._last_seller_check = 0.0
            workermod._maybe_weekly_scan()
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
