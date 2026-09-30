"""Routes Spotify : connexion OAuth et ajout d'une piste à une playlist.

Aucun réseau : `spotifyauth` est entièrement patché. Vérifie les redirections
OAuth, les trois états de la fenêtre playlist et les cas d'erreur Spotify.

Lancer : python3 -m unittest tests.test_spotify_routes -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

from fastapi.testclient import TestClient  # noqa: E402

from radar_web import app as appmod  # noqa: E402
from radar_web.radar import spotifyauth  # noqa: E402


class SpotifyRoutesTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        p = mock.patch.object(appmod.websession, "dev_mode", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def _patch(self, **kw):
        for name, val in kw.items():
            p = mock.patch.object(spotifyauth, name, val)
            p.start()
            self.addCleanup(p.stop)

    def test_start_sans_config_renvoie_sur_patte(self):
        self._patch(configured=lambda: False)
        r = self.client.get("/oauth/spotify/start", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/patte?spotify=unconfigured", r.headers["location"])

    def test_start_redirige_vers_spotify_avec_etat(self):
        self._patch(configured=lambda: True)
        env = {
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        }
        with mock.patch.dict(os.environ, env):
            r = self.client.get("/oauth/spotify/start", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        loc = r.headers["location"]
        self.assertTrue(loc.startswith("https://accounts.spotify.com/authorize"))
        q = parse_qs(urlparse(loc).query)
        self.assertEqual(q["client_id"], ["cid"])
        self.assertEqual(q["redirect_uri"], [env["RADAR_SPOTIFY_REDIRECT_URI"]])
        self.assertIn("state", q)
        self.assertIn("playlist-modify-private", q["scope"][0])

    def test_callback_etat_invalide_refuse_sans_echange(self):
        ex = mock.Mock()
        self._patch(check_state=mock.Mock(return_value=False), exchange_code=ex)
        r = self.client.get(
            "/oauth/spotify/callback?code=abc&state=forge",
            follow_redirects=False,
        )
        self.assertIn("spotify=refused", r.headers["location"])
        ex.assert_not_called()

    def test_callback_erreur_presente_refuse(self):
        ex = mock.Mock()
        self._patch(exchange_code=ex)
        r = self.client.get(
            "/oauth/spotify/callback?error=access_denied&state=forge",
            follow_redirects=False,
        )
        self.assertIn("spotify=refused", r.headers["location"])
        ex.assert_not_called()

    def test_callback_code_vide_refuse(self):
        ex = mock.Mock()
        self._patch(exchange_code=ex)
        r = self.client.get(
            "/oauth/spotify/callback?state=forge",
            follow_redirects=False,
        )
        self.assertIn("spotify=refused", r.headers["location"])
        ex.assert_not_called()

    def test_callback_etat_valide_echange_le_code(self):
        ex = mock.Mock()
        self._patch(check_state=mock.Mock(return_value=True), exchange_code=ex)
        r = self.client.get(
            "/oauth/spotify/callback?code=abc&state=ok",
            follow_redirects=False,
        )
        self.assertIn("spotify=ok", r.headers["location"])
        ex.assert_called_once()
        args, kwargs = ex.call_args
        self.assertIn("abc", args + tuple(kwargs.values()))

    def test_callback_erreur_spotify_redirige_error(self):
        self._patch(
            check_state=mock.Mock(return_value=True),
            exchange_code=mock.Mock(
                side_effect=spotifyauth.SpotifyError("boom")
            ),
        )
        r = self.client.get(
            "/oauth/spotify/callback?code=abc&state=ok",
            follow_redirects=False,
        )
        self.assertIn("spotify=error", r.headers["location"])

    def test_disconnect_redirige_off_et_appelle(self):
        disc = mock.Mock()
        self._patch(disconnect=disc)
        r = self.client.post("/oauth/spotify/disconnect", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/patte?spotify=off", r.headers["location"])
        disc.assert_called_once_with("owner")

    def test_playlists_non_configure(self):
        self._patch(configured=lambda: False)
        r = self.client.get("/reco-radar/sp-playlists?artist=Radiohead&title=Creep")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("/oauth/spotify/start", r.text)
        self.assertNotIn("/reco-radar/sp-playlist-add", r.text)

    def test_playlists_non_relie_propose_la_connexion(self):
        self._patch(configured=lambda: True, is_connected=lambda uid: False)
        r = self.client.get("/reco-radar/sp-playlists?artist=Radiohead&title=Creep")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/oauth/spotify/start", r.text)

    def test_playlists_piste_introuvable(self):
        self._patch(
            configured=lambda: True,
            is_connected=lambda uid: True,
            search_track_uri=lambda uid, artist, title: None,
        )
        r = self.client.get("/reco-radar/sp-playlists?artist=Radiohead&title=Creep")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("/reco-radar/sp-playlist-add", r.text)

    def test_playlists_relie_liste_les_playlists(self):
        self._patch(
            configured=lambda: True,
            is_connected=lambda uid: True,
            search_track_uri=lambda uid, artist, title: "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
            list_playlists=lambda uid: [
                {"id": "37i9dQZF1DXcBWIGoYBM5M", "name": "Deep <b>"},
                {"id": "PL2", "name": "Chill"},
            ],
        )
        r = self.client.get("/reco-radar/sp-playlists?artist=Radiohead&title=Creep")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Deep &lt;b&gt;", r.text)
        self.assertIn("Chill", r.text)
        self.assertIn("/reco-radar/sp-playlist-add", r.text)

    def test_playlists_liste_vide(self):
        self._patch(
            configured=lambda: True,
            is_connected=lambda uid: True,
            search_track_uri=lambda uid, artist, title: "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
            list_playlists=lambda uid: [],
        )
        r = self.client.get("/reco-radar/sp-playlists?artist=Radiohead&title=Creep")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("/reco-radar/sp-playlist-add", r.text)

    def test_ajout_valide_appelle_spotify(self):
        add = mock.Mock()
        self._patch(add_to_playlist=add)
        r = self.client.post(
            "/reco-radar/sp-playlist-add",
            data={
                "track_uri": "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
                "uri": "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
                "playlist_id": "37i9dQZF1DXcBWIGoYBM5M",
                "playlist_name": "Deep",
            },
        )
        self.assertEqual(r.status_code, 200)
        add.assert_called_once_with("owner", "37i9dQZF1DXcBWIGoYBM5M", "spotify:track:4uLU6hMCjMI75M1A2tKUQC")
        self.assertIn("Ajoutée", r.text)

    def test_ajout_identifiant_invalide_refuse(self):
        add = mock.Mock()
        self._patch(add_to_playlist=add)
        r = self.client.post(
            "/reco-radar/sp-playlist-add",
            data={"track_uri": "spotify:track:4uLU6hMCjMI75M1A2tKUQC", "playlist_id": "../x"},
        )
        self.assertEqual(r.status_code, 400)
        add.assert_not_called()

    def test_ajout_erreur_spotify_affichee(self):
        self._patch(
            add_to_playlist=mock.Mock(
                side_effect=spotifyauth.SpotifyError("Playlist Spotify introuvable")
            )
        )
        r = self.client.post(
            "/reco-radar/sp-playlist-add",
            data={"track_uri": "spotify:track:4uLU6hMCjMI75M1A2tKUQC", "playlist_id": "37i9dQZF1DXcBWIGoYBM5M"},
        )
        self.assertIn("Playlist Spotify introuvable", r.text)


if __name__ == "__main__":
    unittest.main()
