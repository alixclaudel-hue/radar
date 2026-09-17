"""Fonctionnalité « Nouveautés » (/veille) mise en pause le 2026-09-17 (décision
utilisateur : pas utilisée pour l'instant, gardée pour plus tard).

Vérifie qu'un seul interrupteur (`app.VEILLE_ENABLED`) suffit : page et routes
associées en 404, entrée de nav et tuile de Mon profil masquées, les deux jobs
(`scan_veille`/`scan_sellers`) non lançables — et que le reste de l'appli
(Recherche, Reco Radar, Réglages, leurs jobs) n'est pas touché.

Lancer : python3 -m unittest tests.test_veille_paused -v
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

from radar_web import app as appmod  # noqa: E402

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


class VeillePausedTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        p = mock.patch.object(appmod, "_dev_mode", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_toutes_les_routes_nouveautes_repondent_404(self):
        for method, url in VEILLE_ROUTES:
            with self.subTest(url=url):
                r = getattr(self.client, method)(url)
                self.assertEqual(r.status_code, 404, url)

    def test_les_deux_jobs_ne_sont_plus_lancables(self):
        for job in ("scan_veille", "scan_sellers"):
            self.assertNotIn(job, appmod.VALID_JOBS)
        # les jobs des autres pages restent valides (la pause ne déborde pas).
        for job in ("scan_recos", "publish_recos", "fetch_collection", "scan_catalog"):
            self.assertIn(job, appmod.VALID_JOBS)

    def test_le_garde_laisse_repasser_la_fonctionnalite_a_True(self):
        # Moitié runtime de l'interrupteur (les routes). L'autre moitié
        # (VALID_JOBS, global Jinja) est calculée à l'import : remettre
        # VEILLE_ENABLED = True demande un redémarrage de l'appli, pas plus.
        with mock.patch.object(appmod, "VEILLE_ENABLED", True):
            appmod._veille_guard()

    def test_nav_et_tuile_de_mon_profil_sans_trace_des_nouveautes(self):
        self.assertIs(appmod.templates.env.globals["veille_enabled"], False)
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('href="/veille"', r.text)
        self.assertNotIn("Nouveautés", r.text)
        # les autres entrées de nav sont toujours là.
        for href in ('href="/search"', 'href="/univers"', 'href="/reco-radar"'):
            self.assertIn(href, r.text)


if __name__ == "__main__":
    unittest.main()
