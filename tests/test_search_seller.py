"""Champ « Vendeur Discogs » de /search (demande utilisateur 2026-09-18).

Renseigner un nom d'utilisateur Discogs change la SOURCE des sorties — le stock
« For Sale » de ce vendeur — et laisse tous les autres filtres de la page
s'appliquer à l'identique : label, genre, style, période, seuils de score, et le
filtre vinyle implicite de cet écran.

L'inventaire Discogs ne porte que `release_id`, `artist` et `format` : styles,
genres, année et label sont relus dans le référentiel local par identifiant.
D'où les deux cas limites testés ici — sortie absente du référentiel, et sortie
non vinyle.

Aucun accès réseau : `discogs.seller_inventory` est simulée, le référentiel est
une base SQLite synthétique.

Lancer : python3 -m unittest tests.test_search_seller -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

from fastapi.testclient import TestClient  # noqa: E402

from radar_web import app as appmod  # noqa: E402
from radar_web.radar import discogs, discogs_dump as dd, paths, store  # noqa: E402

# release_id -> (titre, artiste, label, année, genres, styles, vinyle)
REFERENTIEL = {
    101: ("Deep One", "Artiste A", "Aim Records", 2001, "Electronic", "Deep House", 1),
    102: ("Techno Two", "Artiste B", "Autre Label", 2015, "Electronic", "Techno", 1),
    103: ("Galette CD", "Artiste C", "Aim Records", 2003, "Electronic", "Deep House", 0),
}

LISTINGS = [
    {"release_id": 101, "listing_id": 1, "price": 12.0, "currency": "EUR",
     "condition": "VG+", "sleeve": "VG+", "artist": "Artiste A",
     "format": '12"', "title": "Deep One"},
    {"release_id": 102, "listing_id": 2, "price": 9.0, "currency": "EUR",
     "condition": "NM", "sleeve": "NM", "artist": "Artiste B",
     "format": '12"', "title": "Techno Two"},
    {"release_id": 103, "listing_id": 3, "price": 5.0, "currency": "EUR",
     "condition": "NM", "sleeve": "NM", "artist": "Artiste C",
     "format": "CD", "title": "Galette CD"},
    # absente du référentiel : ajoutée sur Discogs depuis le dernier import mensuel
    {"release_id": 999, "listing_id": 4, "price": 20.0, "currency": "EUR",
     "condition": "M", "sleeve": "M", "artist": "Artiste Neuf",
     "format": '12"', "title": "Sortie Toute Neuve"},
]


def _build_referentiel(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE releases (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
                "label TEXT, catno TEXT, year INTEGER, country TEXT, format TEXT, "
                "genres TEXT, styles TEXT, master_id INTEGER, is_vinyl INTEGER)")
    for rid, (title, artist, label, year, genres, styles, vinyl) in REFERENTIEL.items():
        con.execute("INSERT INTO releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, title, artist, label, "CAT1", year, "FR", '12"',
                     genres, styles, None, vinyl))
    con.commit()
    con.close()


class SearchSellerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db = os.path.join(self._tmp.name, "dump.sqlite3")
        _build_referentiel(db)

        patchers = [
            mock.patch.object(appmod, "_dev_mode", return_value=True),
            mock.patch.object(dd, "DB_PATH", db),
            mock.patch.object(dd, "get_meta", return_value={}),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

        cfg = store.load_config(paths.DEFAULT_UID)
        cfg["token"] = "tok"
        store.save_config(cfg, paths.DEFAULT_UID)
        self.addCleanup(self._clear_token)
        self.client = TestClient(appmod.app, raise_server_exceptions=False)

    def _clear_token(self):
        cfg = store.load_config(paths.DEFAULT_UID)
        cfg["token"] = ""
        store.save_config(cfg, paths.DEFAULT_UID)

    def _post(self, inventory=None, truncated=False, **form):
        data = {"seller": "boutique", "label": "", "genre": "", "style": "",
                "year_from": "", "year_to": "", "pages": "2", "base_metric": "",
                "base_min": "", "label_min": "", "artist_min": "", "page": "1"}
        data.update(form)
        with mock.patch.object(discogs, "seller_inventory",
                                return_value=(LISTINGS if inventory is None else inventory,
                                              truncated)) as inv:
            r = self.client.post("/search", data=data)
        self.assertEqual(r.status_code, 200)
        return r.text, inv

    def test_champ_vendeur_present_sous_le_label(self):
        page = self.client.get("/search").text
        self.assertIn('name="seller"', page)
        self.assertIn("Vendeur Discogs", page)
        self.assertLess(page.index('name="label"'), page.index('name="seller"'))

    def test_stock_du_vendeur_filtre_vinyle(self):
        """La galette CD (103) sort, les deux 12\" restent, et la sortie absente du
        référentiel reste tant qu'aucun filtre ne porte dessus."""
        html, inv = self._post()
        self.assertEqual(inv.call_args[0][0], "boutique")
        self.assertIn("Deep One", html)
        self.assertIn("Techno Two", html)
        self.assertNotIn("Galette CD", html)
        self.assertIn("Sortie Toute Neuve", html)
        self.assertIn("1 hors vinyle", html)

    def test_filtre_style_applique_au_stock(self):
        html, _ = self._post(style="Deep House")
        self.assertIn("Deep One", html)
        self.assertNotIn("Techno Two", html)

    def test_filtre_label_applique_au_stock(self):
        html, _ = self._post(label="Aim Records")
        self.assertIn("Deep One", html)
        self.assertNotIn("Techno Two", html)

    def test_filtre_genre_applique_au_stock(self):
        html, _ = self._post(genre="Hip Hop")
        self.assertNotIn("Deep One", html)
        self.assertNotIn("Techno Two", html)

    def test_filtre_periode_applique_au_stock(self):
        html, _ = self._post(year_from="2010", year_to="2020")
        self.assertIn("Techno Two", html)
        self.assertNotIn("Deep One", html)

    def test_sortie_hors_referentiel_ecartee_des_qu_un_filtre_porte_dessus(self):
        """On ne peut pas affirmer qu'une sortie inconnue du référentiel passe un
        filtre de style : elle est écartée, et le nombre est annoncé."""
        html, _ = self._post(style="Deep House")
        self.assertNotIn("Sortie Toute Neuve", html)
        self.assertIn("non filtrable", html)

    def test_stock_tronque_signale(self):
        html, _ = self._post(truncated=True)
        self.assertIn("tronqué", html)

    def test_sans_token_message_explicite(self):
        self._clear_token()
        with mock.patch.object(discogs, "seller_inventory",
                                side_effect=AssertionError("aucun appel attendu")):
            r = self.client.post("/search", data={
                "seller": "boutique", "label": "", "genre": "", "style": "",
                "year_from": "", "year_to": "", "pages": "2", "base_metric": "",
                "base_min": "", "label_min": "", "artist_min": "", "page": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("token Discogs", r.text)

    def test_vendeur_inconnu_remonte_l_erreur_discogs(self):
        with mock.patch.object(discogs, "seller_inventory",
                                side_effect=discogs.DiscogsError("Erreur Discogs 404 : not found")):
            r = self.client.post("/search", data={
                "seller": "nexistepas", "label": "", "genre": "", "style": "",
                "year_from": "", "year_to": "", "pages": "2", "base_metric": "",
                "base_min": "", "label_min": "", "artist_min": "", "page": "1"})
        self.assertIn("nexistepas", r.text)
        self.assertIn("404", r.text)

    def test_sans_vendeur_le_chemin_habituel_est_inchange(self):
        """Non-régression : un POST sans vendeur ne doit jamais appeler l'inventaire."""
        with mock.patch.object(discogs, "seller_inventory",
                                side_effect=AssertionError("aucun appel attendu")):
            r = self.client.post("/search", data={
                "seller": "", "label": "", "genre": "", "style": "Deep House",
                "year_from": "", "year_to": "", "pages": "2", "base_metric": "",
                "base_min": "", "label_min": "", "artist_min": "", "page": "1"})
        self.assertEqual(r.status_code, 200)

    def test_arobase_en_tete_tolere(self):
        html, inv = self._post(seller="@boutique")
        self.assertEqual(inv.call_args[0][0], "boutique")


if __name__ == "__main__":
    unittest.main()
