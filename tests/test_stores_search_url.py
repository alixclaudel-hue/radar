"""Tests pour `radar_web/radar/stores.py` et les routes `/bp/go`, `/ts/go`.

Contexte (2026-09-28) : l'utilisateur voulait générer une recherche Beatport /
Traxsource depuis l'appli pour acheter une track, mais son scraping Playwright
se faisait capter par Cloudflare. La décision est de NE PAS scraper côté serveur
(IP datacenter + navigateur headless = challenge systématique) mais de rediriger
le navigateur de l'utilisateur vers l'URL de recherche publique.

Ces tests verrouillent :
- la construction de l'URL (encodage des espaces, accents, `&`, label vide) ;
- le fait que les routes répondent bien par une redirection 302 vers la bonne
  boutique, sans jamais tenter d'appel réseau.

Aucun appel réseau : `stores.py` ne fait que construire des chaînes, et les
routes se contentent d'un `RedirectResponse`.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `radar_web.app` lit les chemins de données à l'import : le dossier temporaire
# doit être posé AVANT (même contrainte que test_cart_add_vinyl_fallback.py).
_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ["CRATE_DATA_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from radar_web import app as appmod  # noqa: E402
from radar_web.radar import stores  # noqa: E402


class SearchUrlTests(unittest.TestCase):
    """Construction pure des URLs — pas de réseau, pas de client HTTP."""

    def test_beatport_url_avec_artiste_titre_label(self):
        url = stores.beatport_search_url("Inland Knights", "Last Nite", "Drop Music")
        self.assertEqual(
            url,
            "https://www.beatport.com/search?q=Inland+Knights+Last+Nite+Drop+Music",
        )

    def test_traxsource_url_avec_artiste_titre_label(self):
        url = stores.traxsource_search_url("Inland Knights", "Last Nite", "Drop Music")
        self.assertEqual(
            url,
            "https://www.traxsource.com/search?q=Inland+Knights+Last+Nite+Drop+Music",
        )

    def test_label_vide_est_ignore(self):
        """Un label absent ne doit pas laisser d'espace double ni de `+` orphelin."""
        url = stores.beatport_search_url("Gavinco", "Silver", "")
        self.assertEqual(url, "https://www.beatport.com/search?q=Gavinco+Silver")

    def test_espaces_et_esperluette_encodes(self):
        """`&` doit être encodé, sinon il couperait la query string."""
        url = stores.beatport_search_url("A&B", "Me & You", "")
        self.assertIn("A%26B", url)
        self.assertIn("Me+%26+You", url)
        self.assertEqual(url.count("?"), 1)
        self.assertEqual(url.count("&"), 0)

    def test_accents_encodes(self):
        url = stores.traxsource_search_url("Étienne", "Café", "")
        self.assertIn("%C3%89tienne", url)   # É
        self.assertIn("Caf%C3%A9", url)      # é

    def test_champs_vides_rendent_la_page_de_recherche_nue(self):
        """Pas de `?q=` vide : on renvoie la page de recherche telle quelle."""
        self.assertEqual(stores.beatport_search_url(), "https://www.beatport.com/search")
        self.assertEqual(stores.traxsource_search_url("", "", ""),
                         "https://www.traxsource.com/search")

    def test_espaces_autour_des_champs_sont_ignores(self):
        url = stores.beatport_search_url("  Gavinco  ", "  Silver  ", "  ")
        self.assertEqual(url, "https://www.beatport.com/search?q=Gavinco+Silver")


class RedirectRouteTests(unittest.TestCase):
    """Les routes doivent rediriger (302) sans jamais scraper."""

    def setUp(self):
        self.client = TestClient(appmod.app)
        # Pas de compte/cookie dans un test : le mode dev du middleware
        # d'authentification résout l'utilisateur par défaut (même convention que
        # test_cart_add_vinyl_fallback.py).
        p = mock.patch.object(appmod.websession, "dev_mode", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_bp_go_redirige_vers_beatport(self):
        r = self.client.get("/bp/go", params={"a": "Gavinco", "t": "Silver", "l": "Houseum"},
                            follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            r.headers["location"],
            "https://www.beatport.com/search?q=Gavinco+Silver+Houseum",
        )

    def test_ts_go_redirige_vers_traxsource(self):
        r = self.client.get("/ts/go", params={"a": "Gavinco", "t": "Silver", "l": "Houseum"},
                            follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            r.headers["location"],
            "https://www.traxsource.com/search?q=Gavinco+Silver+Houseum",
        )

    def test_bp_go_sans_parametres_redirige_quand_meme(self):
        """Aucun paramètre : on ne doit pas planter, juste renvoyer la recherche nue."""
        r = self.client.get("/bp/go", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "https://www.beatport.com/search")


if __name__ == "__main__":
    unittest.main()
