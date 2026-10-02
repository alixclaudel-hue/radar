import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from radar_jobs import recos
from radar_web.radar import discogs_dump as dd
from radar_web.radar import discovery
from radar_web.radar import scoring


class HorlogeFixe:
    """Horloge déterministe utilisée pour les dates ajoutées."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class FakeCtx:
    """Contexte de scoring minimal avec un graphe configurable."""

    graph_globale = {"edges": {"x": {}}}

    def __init__(self, uid=None):
        self.uid = uid
        self.graph = json.loads(json.dumps(self.graph_globale))

    def album_score(self, row):
        score = 90 if "Hit" in row["title"] else 50
        return score, {"label": 1, "artist": 2, "style": 3}


class FakeJob:
    """Job memorizing les messages et l'état de fin."""

    def __init__(self):
        self.st = {}
        self.message = None
        self.error = None
        self.messages = []

    def stopped(self):
        return False

    def tick(self, last="", inc=1, total=None):
        self.st["dernier_traitement"] = last

    def msg(self, message):
        self.messages.append(message)

    def finish(self, message="", error=None):
        self.message = message
        self.error = error
        return message


class TestDecouverteRemixeur(unittest.TestCase):
    """Vérifie la sélection des remixes découverts chez un voisin."""

    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.addCleanup(self.dossier.cleanup)
        racine = self.dossier.name
        self.jobs_user_dir = os.path.join(racine, "jobs")
        os.makedirs(self.jobs_user_dir)

        self.candidats_path = os.path.join(racine, "candidats_decouverte.json")
        self.playlist_decouverte_path = os.path.join(racine, "playlist_decouverte.json")
        self.candidats_approfondir_path = os.path.join(racine, "candidats_approfondir.json")
        self.playlist_approfondir_path = os.path.join(racine, "playlist_approfondir.json")
        self.historique_path = os.path.join(racine, "historique.json")
        self.budget_path = os.path.join(racine, "budget.json")
        self.cart_path = os.path.join(racine, "cart.json")
        self.collection_path = os.path.join(racine, "collection.json")
        self.corpus_path = os.path.join(racine, "corpus.json")

        patches = [
            mock.patch.object(recos, "RECOS_CANDIDATES_PATH", self.candidats_approfondir_path),
            mock.patch.object(recos, "RECOS_PLAYLIST_PATH", self.playlist_approfondir_path),
            mock.patch.object(recos, "RECOS_CANDIDATES_DECOUVERTE_PATH", self.candidats_path),
            mock.patch.object(recos, "RECOS_PLAYLIST_DECOUVERTE_PATH", self.playlist_decouverte_path),
            mock.patch.object(recos, "RECOS_HISTORY_PATH", self.historique_path),
            mock.patch.object(recos, "RECOS_SEARCH_BUDGET_PATH", self.budget_path),
            mock.patch.object(recos, "CART_PATH", self.cart_path),
            mock.patch.object(recos, "COLLECTION_CACHE_PATH", self.collection_path),
            mock.patch.object(recos, "CORPUS_PATH", self.corpus_path),
            mock.patch.object(recos, "JOBS_USER_DIR", self.jobs_user_dir),
            mock.patch.object(recos, "cfg_load", return_value={}),
            mock.patch.object(recos, "datetime", HorlogeFixe),
            mock.patch.object(scoring, "Ctx", FakeCtx),
            mock.patch.object(discovery, "discovery_artists", return_value=self.artistes()),
            mock.patch.object(dd, "available", return_value=True),
            mock.patch.object(dd, "tracks_available", return_value=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        self.connexion_principale = mock.Mock()
        self.connexion_pistes = mock.Mock()
        patch_connect_readonly = mock.patch.object(
            dd, "connect_readonly", return_value=self.connexion_principale
        )
        patch_connect_readonly.start()
        self.addCleanup(patch_connect_readonly.stop)
        patch_connect_tracks_readonly = mock.patch.object(
            dd, "connect_tracks_readonly", return_value=self.connexion_pistes
        )
        patch_connect_tracks_readonly.start()
        self.addCleanup(patch_connect_tracks_readonly.stop)

        patch_releases = mock.patch.object(
            dd, "releases_for_artists", return_value=self.sorties()
        )
        self.mock_releases = patch_releases.start()
        self.addCleanup(patch_releases.stop)
        self.tracks_par_sortie = {
            101: self.pistes("Un"),
            102: self.pistes("Deux"),
            201: self.pistes("Deux"),
            202: self.pistes("Un"),
        }
        patch_tracks = mock.patch.object(
            dd,
            "tracks_for_release",
            side_effect=lambda release_id, **kwargs: self.tracks_par_sortie.get(int(release_id)),
        )
        self.mock_tracks = patch_tracks.start()
        self.addCleanup(patch_tracks.stop)

        self.addCleanup(FakeCtx.graph_globale.clear)
        FakeCtx.graph_globale = {"edges": {"x": {}}}

    @staticmethod
    def artistes():
        """Retourne deux artistes voisins synthétiques."""
        return {
            "a1": {
                "name": "Artiste Un",
                "id": 11,
                "score": 10.0,
                "why": ["w1", "w2", "w3"],
                "seeds": ["S"],
            },
            "a2": {
                "name": "Artiste Deux",
                "id": 22,
                "score": 5.0,
                "why": ["v1", "v2", "v3"],
                "seeds": ["S"],
            },
        }

    @staticmethod
    def sorties():
        """Retourne deux sorties pouvant chacune produire une piste."""
        return [
            {
                "id": 101,
                "title": "Album Un",
                "artist": "Artiste Un",
                "label": "Label Un",
                "catno": "CAT-101",
                "year": 2001,
                "genres": "Rock",
                "styles": "Rock, Indie",
                "artist_id": 11,
            },
            {
                "id": 202,
                "title": "Album Deux",
                "artist": "Artiste Deux",
                "label": "Label Deux",
                "catno": "CAT-202",
                "year": 2002,
                "genres": "Pop",
                "styles": "Pop",
                "artist_id": 22,
            },
        ]

    @staticmethod
    def pistes(suffixe):
        """Retourne deux pistes dont une seule est Hits."""
        return [
            {
                "position": "A1",
                "type_": "track",
                "title": f"Hit {suffixe}",
                "artists": [],
            },
            {
                "position": "A2",
                "type_": "track",
                "title": f"Morceau {suffixe}",
                "artists": [],
            },
        ]

    def ecrire_json(self, chemin, valeur):
        """Écrit une donnée locale de test."""
        with open(chemin, "w", encoding="utf-8") as fichier:
            json.dump(valeur, fichier, ensure_ascii=False)

    def lire_candidats(self):
        """Lit la file Découverte produite."""
        with open(self.candidats_path, encoding="utf-8") as fichier:
            return json.load(fichier)

    def test_piste_remix_candidate_mentions_le_voisin_dans_son_titre(self):
        """Vérifie que seule la piste remixée par le voisin est candidate."""
        voisin = self.artistes()["a1"].copy()
        voisin["name"] = "Neighbor Artist"
        voisin["id"] = 33
        discovery.discovery_artists.return_value = {"voisin": voisin}

        sortie = self.sorties()[0]
        sortie.update(
            {
                "id": 101,
                "title": "Known Star - Original",
                "artist": "Known Star",
                "artist_id": 33,
            }
        )
        self.mock_releases.return_value = [sortie]

        pistes = self.pistes("remix")
        pistes[0].update(
            {
                "title": "Known Star - Original",
                "artists": [{"name": "Known Star"}],
            }
        )
        pistes[1].update(
            {
                "title": "Known Star - Original (Neighbor Artist Remix)",
                "artists": [{"name": "Known Star"}],
            }
        )
        self.tracks_par_sortie[101] = pistes
        job = FakeJob()

        recos.job_scan_recos_decouverte(job, {})

        candidats = self.lire_candidats()
        self.assertEqual(1, len(candidats))
        self.assertEqual(101, candidats[0]["release_id"])
        self.assertEqual(
            "Known Star - Original (Neighbor Artist Remix)", candidats[0]["title"]
        )
        self.assertIsNone(job.error)

    def test_aucune_piste_sans_mention_du_voisin_est_candidate(self):
        """Vérifie l'absence de candidat sans mention dans le titre ou le crédit."""
        voisin = self.artistes()["a1"].copy()
        voisin["name"] = "Neighbor Artist"
        voisin["id"] = 33
        discovery.discovery_artists.return_value = {"voisin": voisin}

        sortie = self.sorties()[0]
        sortie.update(
            {
                "id": 101,
                "title": "Known Star - Original",
                "artist": "Known Star",
                "artist_id": 33,
            }
        )
        self.mock_releases.return_value = [sortie]

        pistes = self.pistes("original")
        pistes[0].update(
            {
                "title": "Known Star - Original",
                "artists": [{"name": "Known Star"}],
            }
        )
        pistes[1].update(
            {
                "title": "Known Star - Original (Another Artist Remix)",
                "artists": [{"name": "Known Star"}],
            }
        )
        self.tracks_par_sortie[101] = pistes
        job = FakeJob()

        recos.job_scan_recos_decouverte(job, {})

        self.assertEqual([], self.lire_candidats())
        self.assertIsNone(job.error)


if __name__ == "__main__":
    unittest.main()
