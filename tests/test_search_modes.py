"""Retour utilisateur issue #62 (20/09) : /search proposait 3 façons de chercher
(label seul catalogue entier, "chercher dans mes labels", "vendeur Discogs")
comme 3 champs coexistants sans jamais le dire -- l'utilisateur voulait 3 modes
explicites. Le backend `search_run` n'a pas changé (il priorisait déjà vendeur
> mes labels > label seul) : seule la page devient un sélecteur à 3 options qui
montre/cache les champs du mode actif.

Vérifie que /search affiche le bon mode actif selon la dernière recherche de
l'historique (ou "normale" par défaut), et que le champ label précède toujours
le champ vendeur dans le HTML (invariant déjà couvert par test_search_seller).

Lancer : python3 -m unittest tests.test_search_modes -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

from fastapi.testclient import TestClient  # noqa: E402

from radar_web import app as appmod  # noqa: E402
from radar_web.radar import paths, store  # noqa: E402


class SearchModesTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        p = mock.patch.object(appmod, "_dev_mode", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.hist_path = paths.user_paths(paths.DEFAULT_UID).search_hist
        store.save(self.hist_path, [])

    def _set_last_search(self, params):
        store.save(self.hist_path, [{"id": "abc123", "ts": "2026-09-22 12:00",
                                     "params": params, "n": 0, "n_matches": 0,
                                     "results": [], "searched": [], "dump_date": None}])

    def test_mode_normal_par_defaut_sans_historique(self):
        html = self.client.get("/search").text
        self.assertNotIn('id="mode-normal" hidden', html)
        self.assertIn('id="mode-seller" hidden', html)
        self.assertIn('id="mode-labels" hidden', html)
        self.assertLess(html.index('name="label"'), html.index('name="seller"'))

    def test_mode_vendeur_actif_si_dernier_historique_vendeur(self):
        self._set_last_search({"seller": "boutique", "label": "", "base_metric": ""})
        html = self.client.get("/search?sid=abc123").text
        self.assertIn('id="mode-normal" hidden', html)
        self.assertNotIn('id="mode-seller" hidden', html)
        self.assertIn('id="mode-labels" hidden', html)
        self.assertIn('value="boutique"', html)

    def test_mode_mes_labels_actif_si_dernier_historique_base_metric(self):
        self._set_last_search({"seller": "", "label": "", "base_metric": "reco"})
        html = self.client.get("/search?sid=abc123").text
        self.assertIn('id="mode-normal" hidden', html)
        self.assertIn('id="mode-seller" hidden', html)
        self.assertNotIn('id="mode-labels" hidden', html)
        self.assertIn('value="reco" checked', html)


if __name__ == "__main__":
    unittest.main()
