"""Menu déroulant Mon profil topbar — migration wantlist (issue #62) :

- Le menu déroulant du profil (dans la topbar) contient maintenant trois liens :
  /patte, /wantlist et /tracks-aimees.
- L'ancien lien séparé avec la class "chip cart-tab" a été supprimé.
- La structure du menu est maintenant un <details> avec un <summary>.

Vérifie l'accessibilité de la page /patte en dev_mode et la présence des éléments HTML attendus.
Lancer : python3 -m unittest tests.test_profile_menu -v
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


class ProfileMenuTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        p = mock.patch.object(appmod.websession, "dev_mode", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def _page(self):
        r = self.client.get("/patte")
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_liens_dans_le_menu_deroulant(self):
        html = self._page()
        self.assertIn("href=\"/patte\"", html)
        self.assertIn("href=\"/wantlist\"", html)
        self.assertIn("href=\"/tracks-aimees\"", html)

    def test_ancien_lien_cart_tab_supprime(self):
        html = self._page()
        self.assertNotIn('class="chip cart-tab"', html)

    def test_structure_details_summary(self):
        html = self._page()
        self.assertIn('<details class="profile-menu">', html)
        self.assertIn('<summary class="chip profile-tab', html)


if __name__ == "__main__":
    unittest.main()
