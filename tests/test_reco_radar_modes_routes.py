"""Tests des modes de la page RECOS RADAR.

Lancer :
    python3 -m unittest tests.test_reco_radar_modes -v
"""
import json
import os
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# radar_web.app lit les chemins de données à l'import.
_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

from fastapi.testclient import TestClient  # noqa: E402

from radar_web import app as appmod  # noqa: E402
from radar_web.radar import paths  # noqa: E402


def _piste(video_id, titre, release_id, why=None, played=False):
    piste = {
        "video_id": video_id,
        "artist": "Artiste " + video_id,
        "title": titre,
        "release_id": release_id,
        "release_title": "Release " + video_id,
        "label": "Label " + video_id,
        "year": 2026,
        "album_score": 80,
        "played": played,
    }
    if why is not None:
        piste["why"] = why
        # comme les vrais candidats Découverte (job_scan_recos_decouverte)
        piste["mode"] = "decouverte"
    return piste


PISTE_APPROFONDIR_1 = _piste(
    "vid_approfondir_1", "Piste approfondie", 1001
)
PISTE_APPROFONDIR_2 = _piste(
    "vid_approfondir_2", "Deuxième piste approfondie", 1002
)
PISTE_DECOUVERTE_1 = _piste(
    "vid_decouverte_1", "Piste de la découverte", 2001, why="3× avec X"
)
PISTE_DECOUVERTE_2 = _piste(
    "vid_decouverte_2", "Deuxième piste de découverte", 2002
)
CANDIDAT_APPROFONDIR = _piste(
    "candidat_approfondir", "Candidat approfondi", 3001
)
CANDIDAT_DECOUVERTE = _piste(
    "candidat_decouverte", "Candidat de découverte", 3002
)


class _CollecteurDeLiens(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.liens = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.liens.append(dict(attrs))


class TestRecoRadarModes(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)

        patch_dev_mode = mock.patch.object(
            appmod.websession, "dev_mode", return_value=True
        )
        patch_dev_mode.start()
        self.addCleanup(patch_dev_mode.stop)

        self.utilisateur = paths.user_paths(paths.DEFAULT_UID)
        self.fichiers = {
            "playlist_approfondir": self.utilisateur.recos_playlist,
            "candidats_approfondir": self.utilisateur.recos_candidates,
            "playlist_decouverte": self.utilisateur.recos_playlist_decouverte,
            "candidats_decouverte": self.utilisateur.recos_candidates_decouverte,
        }
        self.contenus = {
            "playlist_approfondir": [
                PISTE_APPROFONDIR_1,
                PISTE_APPROFONDIR_2,
            ],
            "candidats_approfondir": [CANDIDAT_APPROFONDIR],
            "playlist_decouverte": [
                PISTE_DECOUVERTE_1,
                PISTE_DECOUVERTE_2,
            ],
            "candidats_decouverte": [CANDIDAT_DECOUVERTE],
        }

        for nom, contenu in self.contenus.items():
            self._ecrire_json(self.fichiers[nom], contenu)
            self.addCleanup(self._supprimer_fichier, self.fichiers[nom])

    def _ecrire_json(self, fichier, contenu):
        os.makedirs(os.path.dirname(fichier), exist_ok=True)
        with open(fichier, "w", encoding="utf-8") as flux:
            json.dump(contenu, flux, ensure_ascii=False)

    def _lire_json(self, fichier):
        with open(fichier, encoding="utf-8") as flux:
            return json.load(flux)

    def _supprimer_fichier(self, fichier):
        try:
            os.remove(fichier)
        except OSError:
            pass

    def test_page_decouverte_affiche_la_playlist_et_marque_le_lien(self):
        reponse = self.client.get(
            "/reco-radar", params={"mode": "decouverte"}
        )

        self.assertEqual(reponse.status_code, 200)
        self.assertIn(PISTE_DECOUVERTE_1["title"], reponse.text)
        self.assertNotIn(PISTE_APPROFONDIR_1["title"], reponse.text)
        self.assertIn('aria-current="page"', reponse.text)

        collecteur = _CollecteurDeLiens()
        collecteur.feed(reponse.text)
        self.assertTrue(
            any(
                lien.get("href") == "/reco-radar?mode=decouverte"
                and lien.get("aria-current") == "page"
                for lien in collecteur.liens
            )
        )

    def test_mode_inconnu_comporte_comme_approfondir(self):
        reponse = self.client.get(
            "/reco-radar", params={"mode": "nimportequoi"}
        )

        self.assertEqual(reponse.status_code, 200)
        self.assertIn(PISTE_APPROFONDIR_1["title"], reponse.text)
        self.assertNotIn(PISTE_DECOUVERTE_1["title"], reponse.text)

    def test_suppression_decouverte_ne_touche_pas_approfondir(self):
        reponse = self.client.post(
            "/reco-radar/delete",
            data={
                "video_id": PISTE_DECOUVERTE_1["video_id"],
                "mode": "decouverte",
            },
            follow_redirects=False,
        )

        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(
            self._lire_json(self.fichiers["playlist_decouverte"]),
            [PISTE_DECOUVERTE_2],
        )
        self.assertEqual(
            self._lire_json(self.fichiers["playlist_approfondir"]),
            [PISTE_APPROFONDIR_1, PISTE_APPROFONDIR_2],
        )
        self.assertEqual(
            self._lire_json(self.fichiers["candidats_decouverte"]),
            [CANDIDAT_DECOUVERTE],
        )
        self.assertEqual(
            self._lire_json(self.fichiers["candidats_approfondir"]),
            [CANDIDAT_APPROFONDIR],
        )

    def test_marquer_played_modifie_seulement_la_playlist_decouverte(self):
        reponse = self.client.post(
            "/reco-radar/mark-played",
            data={
                "video_id": PISTE_DECOUVERTE_1["video_id"],
                "mode": "decouverte",
            },
            follow_redirects=False,
        )

        self.assertIn(reponse.status_code, (200, 204, 303))
        playlist_decouverte = self._lire_json(
            self.fichiers["playlist_decouverte"]
        )
        self.assertEqual(len(playlist_decouverte), 2)
        piste = next(
            item
            for item in playlist_decouverte
            if item["video_id"] == PISTE_DECOUVERTE_1["video_id"]
        )
        self.assertIs(piste["played"], True)

        playlist_approfondir = self._lire_json(
            self.fichiers["playlist_approfondir"]
        )
        piste_approfondir = next(
            item
            for item in playlist_approfondir
            if item["video_id"] == PISTE_APPROFONDIR_1["video_id"]
        )
        self.assertIs(piste_approfondir["played"], False)

    def test_vidage_candidats_decouverte_redirige_vers_decouverte(self):
        reponse = self.client.post(
            "/reco-radar/clear-candidates",
            data={"mode": "decouverte"},
            follow_redirects=False,
        )

        self.assertEqual(reponse.status_code, 303)
        self.assertEqual(
            reponse.headers.get("location"),
            "/reco-radar?mode=decouverte",
        )
        self.assertEqual(
            self._lire_json(self.fichiers["candidats_decouverte"]), []
        )
        self.assertEqual(
            self._lire_json(self.fichiers["candidats_approfondir"]),
            [CANDIDAT_APPROFONDIR],
        )
        self.assertEqual(
            self._lire_json(self.fichiers["playlist_decouverte"]),
            [PISTE_DECOUVERTE_1, PISTE_DECOUVERTE_2],
        )
        self.assertEqual(
            self._lire_json(self.fichiers["playlist_approfondir"]),
            [PISTE_APPROFONDIR_1, PISTE_APPROFONDIR_2],
        )

    def test_raison_de_la_piste_decouverte_est_affichee(self):
        reponse = self.client.get(
            "/reco-radar", params={"mode": "decouverte"}
        )

        self.assertEqual(reponse.status_code, 200)
        self.assertIn("3× avec X", reponse.text)

    def test_mode_absent_selectionne_approfondir(self):
        reponse = self.client.get("/reco-radar")

        self.assertEqual(reponse.status_code, 200)
        self.assertIn(PISTE_APPROFONDIR_1["title"], reponse.text)
        self.assertNotIn(PISTE_DECOUVERTE_1["title"], reponse.text)

    def test_mode_vide_ou_zero_selectionne_approfondir(self):
        for mode in ("", "0"):
            with self.subTest(mode=mode):
                reponse = self.client.get(
                    "/reco-radar", params={"mode": mode}
                )
                self.assertEqual(reponse.status_code, 200)
                self.assertIn(PISTE_APPROFONDIR_1["title"], reponse.text)
                self.assertNotIn(PISTE_DECOUVERTE_1["title"], reponse.text)

    def test_pages_avec_des_listes_vides_restent_accessibles(self):
        for fichier in self.fichiers.values():
            self._ecrire_json(fichier, [])

        for mode in ("decouverte", "nimportequoi"):
            with self.subTest(mode=mode):
                reponse = self.client.get(
                    "/reco-radar", params={"mode": mode}
                )
                self.assertEqual(reponse.status_code, 200)

    def test_video_inconnue_ne_modifie_les_fichiers(self):
        reponse = self.client.post(
            "/reco-radar/mark-played",
            data={"video_id": "vid_inconnue", "mode": "decouverte"},
            follow_redirects=False,
        )

        self.assertIn(reponse.status_code, (200, 204, 303))
        self.assertEqual(
            self._lire_json(self.fichiers["playlist_decouverte"]),
            [PISTE_DECOUVERTE_1, PISTE_DECOUVERTE_2],
        )
        self.assertEqual(
            self._lire_json(self.fichiers["playlist_approfondir"]),
            [PISTE_APPROFONDIR_1, PISTE_APPROFONDIR_2],
        )


if __name__ == "__main__":
    unittest.main()
