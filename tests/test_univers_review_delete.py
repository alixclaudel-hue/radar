"""Retour utilisateur issue #62 (20/09) : dans le panneau « À vérifier » de
/univers, une entrée « jamais identifiée sur Discogs » n'avait aucun moyen de
suppression -- juste un texte suggérant de « retirer l'entrée si besoin »,
sans bouton pour le faire (« que se passe-t-il si le label ne correspond à
aucune option ? je supprimerais le label de la liste »).

Vérifie que POST /univers/review/{kind}/delete retire l'entrée ET le
label/artiste correspondant de la base (label_categories / artist_categories),
pour les labels comme pour les artistes, et laisse le reste de la base intact.

Lancer : python3 -m unittest tests.test_univers_review_delete -v
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


class UniversReviewDeleteTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        p = mock.patch.object(appmod.websession, "dev_mode", return_value=True)
        p.start()
        self.addCleanup(p.stop)

        self.uid = paths.DEFAULT_UID
        cfg = store.load_config(self.uid)
        cfg["label_categories"] = {"1": ["Keep Records"], "2": ["Not A Real Label"]}
        cfg["artist_categories"] = {"1": ["Real Artist"], "2": ["Not A Real Artist"]}
        store.save_config(cfg, self.uid)

        self.resolved_path = paths.user_paths(self.uid).resolved
        self.artists_res_path = paths.user_paths(self.uid).artists_res
        store.save(self.resolved_path, {
            "keep records": {"original": "Keep Records", "status": "confirmed"},
            "not a real label": {"original": "Not A Real Label", "status": "not_found"},
        })
        store.save(self.artists_res_path, {
            "real artist": {"original": "Real Artist", "status": "confirmed"},
            "not a real artist": {"original": "Not A Real Artist", "status": "not_found"},
        })

    def test_delete_label_retire_de_la_base_et_du_resolu(self):
        r = self.client.post("/univers/review/label/delete", data={"key": "not a real label"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Not A Real Label", r.text)

        cfg = store.load_config(self.uid)
        self.assertNotIn("Not A Real Label", cfg["label_categories"]["2"])
        self.assertIn("Keep Records", cfg["label_categories"]["1"])  # reste intact
        resolved = store.load(self.resolved_path, {})
        self.assertNotIn("not a real label", resolved)
        self.assertIn("keep records", resolved)  # reste intact

    def test_delete_artist_retire_de_la_base_et_du_resolu(self):
        r = self.client.post("/univers/review/artist/delete", data={"key": "not a real artist"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Not A Real Artist", r.text)

        cfg = store.load_config(self.uid)
        self.assertNotIn("Not A Real Artist", cfg["artist_categories"]["2"])
        self.assertIn("Real Artist", cfg["artist_categories"]["1"])  # reste intact
        resolved = store.load(self.artists_res_path, {})
        self.assertNotIn("not a real artist", resolved)
        self.assertIn("real artist", resolved)  # reste intact

    def test_kind_invalide_404(self):
        r = self.client.post("/univers/review/bogus/delete", data={"key": "x"})
        self.assertEqual(r.status_code, 404)

    def test_action_inconnue_404(self):
        r = self.client.post("/univers/review/label/bogus", data={"key": "x"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
