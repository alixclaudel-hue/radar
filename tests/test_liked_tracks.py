import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


class LikedTracksToggleTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        p = mock.patch.object(appmod.websession, "dev_mode", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.pu = paths.user_paths(paths.DEFAULT_UID)
        os.makedirs(os.path.dirname(self.pu.recos_playlist), exist_ok=True)
        with open(self.pu.recos_playlist, "w", encoding="utf-8") as f:
            json.dump(TRACKS, f)

    def tearDown(self):
        for p in (self.pu.recos_playlist, self.pu.liked_tracks):
            try:
                os.remove(p)
            except OSError:
                pass

    def _liked(self):
        with open(self.pu.liked_tracks, encoding="utf-8") as f:
            return json.load(f)

    def test_like_puis_unlike_meme_video_id(self):
        # Premier appel : like
        resp = self.client.post("/reco-radar/like-toggle", data={"video_id": "vid_aaa"})
        self.assertEqual(resp.status_code, 200)
        # Vérifie le fragment de réponse
        self.assertIn('id="like-vid_aaa"', resp.text)
        self.assertNotIn('<tbody id="reco-rows">', resp.text)
        # Vérifie le fichier liked_tracks.json
        liked = self._liked()
        self.assertEqual(len(liked), 1)
        self.assertEqual(liked[0]["video_id"], "vid_aaa")
        self.assertEqual(liked[0]["artist"], "Inland Knights")
        self.assertIn("liked_at", liked[0])
        self.assertTrue(liked[0]["liked_at"])  # non vide

        # Deuxième appel : unlike
        resp2 = self.client.post("/reco-radar/like-toggle", data={"video_id": "vid_aaa"})
        self.assertEqual(resp2.status_code, 200)
        # Le fichier doit être une liste vide
        liked_after = self._liked()
        self.assertEqual(liked_after, [])

    def test_like_toggle_video_id_inconnu_ne_leve_pas(self):
        # Vidéo inconnue, jamais aimée
        resp = self.client.post("/reco-radar/like-toggle", data={"video_id": "ne-existe-pas"})
        self.assertEqual(resp.status_code, 200)
        # Aucune entrée ajoutée
        if os.path.exists(self.pu.liked_tracks):
            liked = self._liked()
            self.assertEqual(liked, [])
        # else : le fichier n'existe pas, ce qui est aussi valide


class LikedTracksDedupTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        self._dev_mode_patch = mock.patch.object(appmod.websession, 'dev_mode', return_value=True)
        self._dev_mode_patch.start()
        self.addCleanup(self._dev_mode_patch.stop)
        self.pu = paths.user_paths(paths.DEFAULT_UID)
        os.makedirs(os.path.dirname(self.pu.recos_playlist), exist_ok=True)
        tracks = [
            {"video_id": "vid_one", "artist": "Some Artist", "title": "A Great Track"},
            {"video_id": "vid_two", "artist": "some-artist", "title": "a   great track"},
        ]
        with open(self.pu.recos_playlist, "w", encoding="utf-8") as f:
            json.dump(tracks, f)
        if os.path.exists(self.pu.liked_tracks):
            os.remove(self.pu.liked_tracks)

    def tearDown(self):
        for path in (self.pu.recos_playlist, self.pu.liked_tracks):
            try:
                os.remove(path)
            except OSError:
                pass

    def test_dedoublonnage_par_identite_artiste_titre(self):
        # même identité (artiste, titre) une fois normalisée, video_id différents
        resp = self.client.post("/reco-radar/like-toggle", data={"video_id": "vid_one"})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post("/reco-radar/like-toggle", data={"video_id": "vid_two"})
        self.assertEqual(resp.status_code, 200)
        # le deuxième appel est vu comme un "unlike" de la même identité, pas un
        # deuxième like : le fichier redevient vide plutôt que d'avoir 2 entrées.
        with open(self.pu.liked_tracks, encoding="utf-8") as f:
            liked = json.load(f)
        self.assertEqual(len(liked), 0)


class TracksAimeesPageTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        self.patcher = mock.patch.object(appmod.websession, "dev_mode", return_value=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.pu = paths.user_paths(paths.DEFAULT_UID)
        parent_dir = os.path.dirname(self.pu.liked_tracks)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

    def tearDown(self):
        try:
            os.remove(self.pu.liked_tracks)
        except OSError:
            pass

    def test_page_liste_les_pistes_aimees(self):
        tracks = [
            {"video_id": "vid_x", "artist": "Artist X", "title": "Title X"},
            {"video_id": "vid_y", "artist": "Artist Y", "title": "Title Y"},
        ]
        with open(self.pu.liked_tracks, "w", encoding="utf-8") as f:
            json.dump(tracks, f)
        resp = self.client.get("/tracks-aimees")
        self.assertEqual(resp.status_code, 200)
        text = resp.text
        self.assertIn('data-vid="vid_x"', text)
        self.assertIn('data-vid="vid_y"', text)
        self.assertIn("Artist X", text)
        self.assertIn("Artist Y", text)

    def test_page_vide_sans_erreur(self):
        if os.path.exists(self.pu.liked_tracks):
            os.remove(self.pu.liked_tracks)
        resp = self.client.get("/tracks-aimees")
        self.assertEqual(resp.status_code, 200)

    def test_export_watch_videos_par_lots_de_50(self):
        tracks = [{"video_id": f"vid_{i}", "artist": f"Artist {i}", "title": f"Title {i}"}
                  for i in range(120)]
        with open(self.pu.liked_tracks, "w", encoding="utf-8") as f:
            json.dump(tracks, f)
        resp = self.client.get("/tracks-aimees")
        self.assertEqual(resp.status_code, 200)
        text = resp.text
        blocks = re.findall(r'video_ids=([^"]+)"', text)
        # 120 pistes / 50 par lot = 3 lots (50 + 50 + 20)
        self.assertEqual(len(blocks), 3)
        total = 0
        for block in blocks:
            ids = block.split(",")
            self.assertLessEqual(len(ids), 50)
            total += len(ids)
        self.assertEqual(total, 120)


if __name__ == "__main__":
    unittest.main()
