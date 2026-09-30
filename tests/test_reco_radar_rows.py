"""Playlist RECOS RADAR — 3 retours utilisateur du 2026-09-18 (issue #62) :

- Release et Label fusionnés dans une seule colonne.
- Colonne « Score » supprimée : le score passe en pastille dans la colonne du
  numéro de ligne (gain de largeur en portrait sur mobile).
- Suppression d'une piste (🗑️) sans rechargement de page : la route renvoie le
  tableau à jour au lieu d'une redirection 303.

Nouveau gabarit (issue suivante) :
- Suppression de la colonne numérotée "#".
- Suppression du bouton cœur like-toggle.
- Menu d'achat unifié (<details class=\"buy-menu\">) avec 3 liens boutiques,
  bouton Playlist (data-pl-add), et gestion wantlist (formulaire ou message).
- Score en pastille dans td.trk-main.
- Pouces (.vote) et poubelle (delete) conservés.

Vérifie aussi l'invariant qui rend la suppression en place possible : les lignes
portent l'identifiant de la vidéo (`data-vid`), jamais leur position — le lecteur
IFrame est piloté par la liste visible et adresse les vidéos par identifiant, pas
par rang (cf. pages/reco_radar.html, correctif issue #62 F3/F4/F7).

Lancer : python3 -m unittest tests.test_reco_radar_rows -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `radar_web.app` lit les chemins de données à l'import (cf. test_features_paused).
_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

from fastapi.testclient import TestClient  # noqa: E402

from radar_web import app as appmod  # noqa: E402
from radar_web.radar import paths  # noqa: E402

TRACKS = [
    {"video_id": "vid_aaa", "artist": "Inland Knights", "title": "12 Till 8",
     "release_id": 1228717, "release_title": "Drop Music", "label": "USM Records",
     "year": 2005, "album_score": 84},
    {"video_id": "vid_bbb", "artist": "Soichi Terada", "title": "Sun Showers",
     "release_id": 555, "release_title": "Asakusa Light", "label": "Rush Hour",
     "year": 2021, "album_score": 77},
]


class RecoRadarRowsTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        p = mock.patch.object(appmod.websession, "dev_mode", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.path = paths.user_paths(paths.DEFAULT_UID).recos_playlist
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(TRACKS, f)

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _page(self):
        r = self.client.get("/reco-radar")
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_release_et_label_dans_une_seule_colonne(self):
        html = self._page()
        self.assertIn("Release / Label", html)
        # plus d'en-tête « Label » isolé entre Release et Année
        self.assertNotIn("<th>Release</th>", html)
        self.assertNotIn("<th>Label</th>", html)
        # les deux valeurs restent affichées, dans la même cellule
        self.assertIn("Drop Music", html)
        self.assertIn("USM Records", html)

    def test_score_en_pastille_plus_de_colonne_score(self):
        html = self._page()
        self.assertNotIn(">Score<", html)
        self.assertIn('<span class="badge" title="Score album /100">', html)
        self.assertIn("84", html)

    def test_suppression_renvoie_le_tableau_sans_redirection(self):
        r = self.client.post("/reco-radar/delete", data={"video_id": "vid_aaa"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 200)          # pas 303 : plus de rechargement
        # htmx repère l'élément de table en tête de réponse : le fragment doit
        # commencer par le tbody, sans blanc ni commentaire devant.
        self.assertTrue(r.text.startswith('<tbody id="reco-rows">'), repr(r.text[:40]))
        self.assertNotIn("vid_aaa", r.text)
        self.assertIn("vid_bbb", r.text)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual([t["video_id"] for t in json.load(f)], ["vid_bbb"])

    def test_plus_de_colonne_numerotation(self):
        """Le gabarit ne rend plus la colonne '#' (loop.index).
        Après suppression, la première cellule ne doit pas être un numéro de ligne."""
        html = self._page()
        self.assertNotIn("<th>#</th>", html)
        self.assertNotIn("<th>N°</th>", html)
        # Après suppression, le tbody ne doit pas contenir de colonne d'index
        r = self.client.post("/reco-radar/delete", data={"video_id": "vid_aaa"})
        body = r.text[r.text.index("<tbody"):]
        # La première cellule de la ligne restante ne doit pas être un chiffre (numéro de ligne)
        first_td_content = body.split("<td")[1].split(">")[1].split("<")[0].strip()
        self.assertNotEqual(first_td_content, "1")
        self.assertNotEqual(first_td_content, "2")

    def test_lignes_appariees_par_video_id_pas_par_position(self):
        """Invariant du lecteur IFrame : sans `data-vid`, une suppression en place
        décalerait les lignes par rapport à la liste chargée par le lecteur."""
        html = self._page()
        self.assertIn('data-vid="vid_aaa"', html)
        self.assertNotIn("data-index", html)
        # le lecteur est adressé par recherche de l'identifiant dans SA liste,
        # pas par le rang de la ligne cliquée
        self.assertIn(".indexOf(vid)", html)

    def test_suppression_inconnue_laisse_la_playlist_intacte(self):
        r = self.client.post("/reco-radar/delete", data={"video_id": "nope"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("vid_aaa", r.text)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)), 2)

    # --- Nouveaux tests pour le gabarit mis à jour ---

    def test_aucun_bouton_like_toggle_dans_le_html(self):
        """Le bouton cœur (like-toggle) a été supprimé de la ligne."""
        html = self._page()
        self.assertNotIn("like-toggle", html)
        self.assertNotIn('id="like-', html)

    def test_presence_des_trois_liens_boutiques_encodes(self):
        """Le menu d'achat contient les 3 liens vers Bandcamp, Discogs (TS) et Bandcamp (BP)."""
        html = self._page()
        self.assertIn('<details class="buy-menu">', html)
        self.assertIn('/bc/go?', html)
        self.assertIn('/ts/go?', html)
        self.assertIn('/bp/go?', html)

    def test_absence_lien_ouvrir_sur_youtube(self):
        """L'ancien lien 'Ouvrir sur YouTube' n'est plus présent."""
        html = self._page()
        self.assertNotIn("Ouvrir sur YouTube", html)

    def test_bouton_playlist_data_pl_add_present(self):
        """Le bouton Playlist avec data-pl-add et data-vid est présent."""
        html = self._page()
        self.assertIn('data-pl-add', html)
        self.assertIn('data-vid="vid_aaa"', html)
        self.assertIn('Playlist', html)

    def test_deja_en_wantlist_si_release_id_dans_in_cart(self):
        """Si le release_id est dans le panier (in_cart), on affiche 'déjà en wantlist'."""
        # Écriture du fichier panier pour l'utilisateur de test
        cart_path = paths.user_paths(paths.DEFAULT_UID).cart
        os.makedirs(os.path.dirname(cart_path), exist_ok=True)
        with open(cart_path, "w", encoding="utf-8") as f:
            json.dump([{"id": 1228717}], f)  # release_id de vid_aaa
        self.addCleanup(lambda: os.remove(cart_path) if os.path.exists(cart_path) else None)

        html = self._page()
        # Vérifie la présence du message pour la piste concernée
        self.assertIn('class="buy-menu__done"', html)
        self.assertIn("déjà en wantlist", html.lower())


if __name__ == "__main__":
    unittest.main()
