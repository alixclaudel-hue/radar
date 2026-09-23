"""Vérification de la présence du bouton de suppression sur les entrées au
statut 'approx' (devinées par l'API) dans le panneau « À vérifier » de /univers.

Lancer : python3 -m unittest tests.test_univers_review_approx_delete -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `radar_web.app` lit les chemins de données à l'import (cf. test_reco_radar_rows).
_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

from fastapi.testclient import TestClient  # noqa: E402

from radar_web import app as appmod  # noqa: E402
from radar_web.radar import paths, store  # noqa: E402


class ReviewApproxDeleteButtonTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        p = mock.patch.object(appmod, "_dev_mode", return_value=True)
        p.start()
        self.addCleanup(p.stop)

        self.uid = paths.DEFAULT_UID
        cfg = store.load_config(self.uid)
        cfg["label_categories"] = {"1": ["Guess Label"]}
        cfg["artist_categories"] = {"1": ["Guess Artist"]}
        store.save_config(cfg, self.uid)

        self.resolved_path = paths.user_paths(self.uid).resolved
        self.artists_res_path = paths.user_paths(self.uid).artists_res

    def test_bouton_delete_present_sur_label_approx(self):
        store.save(self.resolved_path, {
            "guess label": {
                "original": "Guess Label",
                "status": "approx",
                "discogs_name": "Guess Label",
                "discogs_id": 123,
            }
        })

        r = self.client.get("/univers?tab=labels")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/univers/review/label/confirm", r.text)
        self.assertIn("/univers/review/label/delete", r.text)

    def test_bouton_delete_present_sur_artist_approx(self):
        store.save(self.artists_res_path, {
            "guess artist": {
                "original": "Guess Artist",
                "status": "approx",
                "discogs_name": "Guess Artist",
                "discogs_id": 456,
            }
        })

        r = self.client.get("/univers?tab=artists")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/univers/review/artist/confirm", r.text)
        self.assertIn("/univers/review/artist/delete", r.text)


if __name__ == "__main__":
    unittest.main()
