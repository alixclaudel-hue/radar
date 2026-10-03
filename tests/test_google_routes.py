"""Connexion Google/YouTube (« Mes goûts ») et fenêtre « Ajouter à une playlist ».

Aucun réseau : `googleauth` est patché. Vérifie surtout le garde CSRF du callback
(état signé lié à l'utilisateur) et les trois états de la fenêtre playlist.

Lancer : python3 -m unittest tests.test_google_routes -v
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
from radar_web.radar import googleauth  # noqa: E402


class GoogleRoutesTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        p = mock.patch.object(appmod.websession, "dev_mode", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def _patch(self, **kw):
        for name, val in kw.items():
            p = mock.patch.object(googleauth, name, val)
            p.start()
            self.addCleanup(p.stop)

    def test_start_sans_config_renvoie_sur_patte(self):
        self._patch(configured=lambda: False)
        r = self.client.get("/oauth/google/start", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/patte?google=unconfigured", r.headers["location"])

    def test_start_redirige_vers_google_avec_etat(self):
        self._patch(configured=lambda: True)
        with mock.patch.dict(os.environ, {"RADAR_GOOGLE_CLIENT_ID": "cid",
                                          "RADAR_GOOGLE_CLIENT_SECRET": "sec",
                                          "RADAR_GOOGLE_REDIRECT_URI": "https://x.test/oauth/google/callback"}):
            r = self.client.get("/oauth/google/start", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("https://accounts.google.com/"))
        self.assertIn("state=", r.headers["location"])

    def test_callback_etat_invalide_refuse_sans_echange(self):
        ex = mock.Mock()
        self._patch(exchange_code=ex)
        r = self.client.get("/oauth/google/callback?code=abc&state=forge", follow_redirects=False)
        self.assertIn("google=refused", r.headers["location"])
        ex.assert_not_called()

    def test_callback_etat_valide_echange_le_code(self):
        ex = mock.Mock()
        self._patch(exchange_code=ex)
        state = googleauth.make_state("owner")
        r = self.client.get(f"/oauth/google/callback?code=abc&state={state}", follow_redirects=False)
        self.assertIn("google=ok", r.headers["location"])
        ex.assert_called_once_with("owner", "abc")

    def test_callback_erreur_google_refuse(self):
        r = self.client.get("/oauth/google/callback?error=access_denied", follow_redirects=False)
        self.assertIn("google=refused", r.headers["location"])

    def test_playlists_non_configure(self):
        self._patch(configured=lambda: False)
        r = self.client.get("/reco-radar/yt-playlists?video_id=v1")
        self.assertIn("pas encore configurée", r.text)

    def test_playlists_non_relie_propose_la_connexion(self):
        self._patch(configured=lambda: True, is_connected=lambda uid: False)
        r = self.client.get("/reco-radar/yt-playlists?video_id=v1")
        self.assertIn("/oauth/google/start", r.text)

    def test_playlists_relie_liste_les_playlists(self):
        self._patch(configured=lambda: True, is_connected=lambda uid: True,
                    list_playlists=lambda uid: [{"id": "PL1", "title": "Deep <b>"}])
        r = self.client.get("/reco-radar/yt-playlists?video_id=v1")
        self.assertIn("/reco-radar/yt-playlist-add", r.text)
        self.assertIn("Deep &lt;b&gt;", r.text)          # titre échappé

    def test_ajout_valide_appelle_google(self):
        add = mock.Mock()
        self._patch(add_to_playlist=add)
        r = self.client.post("/reco-radar/yt-playlist-add",
                             data={"video_id": "v1", "playlist_id": "PL1", "playlist_title": "Deep"})
        self.assertEqual(r.status_code, 200)
        add.assert_called_once_with("owner", "PL1", "v1")
        self.assertIn("Ajoutée", r.text)

    def test_ajout_identifiant_invalide_refuse(self):
        add = mock.Mock()
        self._patch(add_to_playlist=add)
        r = self.client.post("/reco-radar/yt-playlist-add",
                             data={"video_id": "v1", "playlist_id": "../x"})
        self.assertEqual(r.status_code, 400)
        add.assert_not_called()

    def test_ajout_erreur_google_affichee(self):
        self._patch(add_to_playlist=mock.Mock(side_effect=googleauth.GoogleError("Quota YouTube du jour atteint")))
        r = self.client.post("/reco-radar/yt-playlist-add",
                             data={"video_id": "v1", "playlist_id": "PL1"})
        self.assertIn("Quota YouTube", r.text)

    def test_patte_montre_la_section_google(self):
        self._patch(configured=lambda: False, is_connected=lambda uid: False)
        r = self.client.get("/patte")
        self.assertEqual(r.status_code, 200)
        self.assertIn("YouTube", r.text)
        self.assertIn("RADAR_GOOGLE_CLIENT_ID", r.text)


if __name__ == "__main__":
    unittest.main()
