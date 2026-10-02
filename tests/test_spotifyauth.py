import os
import sys
import json
import time
import tempfile
import unittest
from unittest import mock

# --- Isolation du répertoire de données (chemin disque) ---
_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

# --- Import du module à tester ---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_web.radar import spotifyauth
from radar_web.radar import paths


class FakeResponse:
    """Objet réponse simulant requests.Response."""
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json


class SpotifyAuthTest(unittest.TestCase):
    def setUp(self):
        # Répertoire temporaire pour user_dir
        self.tmpdir = tempfile.mkdtemp(prefix="spotify-test-")

        def _fake_user_dir(uid):
            d = os.path.join(self.tmpdir, uid)
            os.makedirs(d, exist_ok=True)
            return d

        self._patch_user_dir = mock.patch.object(paths, "user_dir", _fake_user_dir)
        self._patch_user_dir.start()
        self.addCleanup(self._patch_user_dir.stop)

    def _patch_http(self, response):
        if isinstance(response, mock.Mock):
            p = mock.patch.object(spotifyauth, "_http", response)
        else:
            p = mock.patch.object(spotifyauth, "_http", return_value=response)
        p.start()
        self.addCleanup(p.stop)

    def _patch_env(self, env):
        p = mock.patch.dict(os.environ, env, clear=False)
        p.start()
        self.addCleanup(p.stop)

    # --- (1) _normalize ---
    def test_normalize_supprime_accents_ponctuation(self):
        result = spotifyauth._normalize("Beyoncé — Don't Hurt")
        self.assertEqual(result, "beyonce don t hurt")

    def test_normalize_gere_none_et_chaine_vide(self):
        self.assertEqual(spotifyauth._normalize(None), "")
        self.assertEqual(spotifyauth._normalize(""), "")

    # --- (2) _has_unwanted ---
    def test_has_unwanted_ecarte_remix_si_titre_ne_contient_pas_remix(self):
        self.assertTrue(spotifyauth._has_unwanted("track remix", "track"))

    def test_has_unwanted_ne_decourage_pas_edition_pour_edition(self):
        self.assertFalse(spotifyauth._has_unwanted("edition", "edition"))

    def test_has_unwanted_ne_decourage_pas_olive_pour_olive(self):
        self.assertFalse(spotifyauth._has_unwanted("olive", "olive"))

    def test_has_unwanted_ne_decourage_pas_remix_si_titre_contient_remix(self):
        self.assertFalse(spotifyauth._has_unwanted("track remix", "track remix"))

    # --- (3) _get_client_config ---
    def test_get_client_config_avec_variables_spotify(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        cid, secret, uri = spotifyauth._get_client_config()
        self.assertEqual(cid, "cid")
        self.assertEqual(secret, "sec")
        self.assertEqual(uri, "https://x.test/oauth/spotify/callback")

    def test_get_client_config_repli_domain(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_DOMAIN": "example.com",
        })
        _, _, uri = spotifyauth._get_client_config()
        self.assertEqual(uri, "https://example.com/oauth/spotify/callback")

    def test_configured_faux_sans_secret(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        self.assertFalse(spotifyauth.configured())

    # --- (4) exchange_code ---
    def test_exchange_code_ecrit_fichier_avec_droits_600(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        resp = FakeResponse(200, {
            "access_token": "acc",
            "refresh_token": "ref",
            "expires_in": 3600,
        })
        self._patch_http(resp)
        spotifyauth.exchange_code("user1", "code123")
        path = spotifyauth._token_path("user1")
        self.assertTrue(os.path.exists(path))
        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["access_token"], "acc")
        self.assertEqual(data["refresh_token"], "ref")

    def test_exchange_code_sans_refresh_token_lève_si_pas_de_fichier_existant(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        resp = FakeResponse(200, {
            "access_token": "acc",
            "expires_in": 3600,
        })
        self._patch_http(resp)
        with self.assertRaises(spotifyauth.SpotifyError):
            spotifyauth.exchange_code("user1", "code123")

    def test_exchange_code_sans_refresh_token_garde_ancien_si_fichier_existe(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        # Écrire un fichier existant
        spotifyauth._write_tokens("user1", {
            "access_token": "old_acc",
            "refresh_token": "old_ref",
            "expires_at": time.time() + 3600,
        })
        resp = FakeResponse(200, {
            "access_token": "new_acc",
            "expires_in": 3600,
        })
        self._patch_http(resp)
        spotifyauth.exchange_code("user1", "code123")
        data = spotifyauth._read_tokens("user1")
        self.assertEqual(data["access_token"], "new_acc")
        self.assertEqual(data["refresh_token"], "old_ref")

    # --- (5) access_token ---
    def test_access_token_rafraichit_jeton_expire(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        spotifyauth._write_tokens("user1", {
            "access_token": "old_acc",
            "refresh_token": "ref",
            "expires_at": time.time() - 100,  # expiré
        })
        resp = FakeResponse(200, {
            "access_token": "new_acc",
            "expires_in": 3600,
        })
        self._patch_http(resp)
        token = spotifyauth.access_token("user1")
        self.assertEqual(token, "new_acc")
        data = spotifyauth._read_tokens("user1")
        self.assertEqual(data["refresh_token"], "ref")  # conservé

    def test_access_token_garde_ancien_refresh_si_reponse_sans_refresh(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        spotifyauth._write_tokens("user1", {
            "access_token": "old_acc",
            "refresh_token": "ref",
            "expires_at": time.time() - 100,
        })
        resp = FakeResponse(200, {
            "access_token": "new_acc",
            "expires_in": 3600,
        })
        self._patch_http(resp)
        spotifyauth.access_token("user1")
        data = spotifyauth._read_tokens("user1")
        self.assertEqual(data["refresh_token"], "ref")

    def test_access_token_invalid_grant_supprime_fichier_et_leve(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        spotifyauth._write_tokens("user1", {
            "access_token": "old_acc",
            "refresh_token": "ref",
            "expires_at": time.time() - 100,
        })
        resp = FakeResponse(400, {"error": "invalid_grant"})
        self._patch_http(resp)
        with self.assertRaises(spotifyauth.SpotifyError):
            spotifyauth.access_token("user1")
        self.assertFalse(os.path.exists(spotifyauth._token_path("user1")))

    def test_access_token_lève_si_non_connecté(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        with self.assertRaises(spotifyauth.SpotifyError):
            spotifyauth.access_token("user1")

    # --- (6) list_playlists ---
    def test_list_playlists_filtre_par_propriétaire_ou_collaborative(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        spotifyauth._write_tokens("user1", {
            "access_token": "acc",
            "refresh_token": "ref",
            "expires_at": time.time() + 3600,
        })
        me_resp = FakeResponse(200, {"id": "user1"})
        playlists_resp = FakeResponse(200, {
            "items": [
                {"id": "p1", "name": "Owned", "owner": {"id": "user1"}, "collaborative": False},
                {"id": "p2", "name": "Collab", "owner": {"id": "other"}, "collaborative": True},
                {"id": "p3", "name": "Other", "owner": {"id": "other"}, "collaborative": False},
            ],
            "next": None,
        })
        # /me puis /me/playlists
        responses = [me_resp, playlists_resp]
        self._patch_http(mock.Mock(side_effect=responses))
        result = spotifyauth.list_playlists("user1")
        ids = [p["id"] for p in result]
        self.assertIn("p1", ids)
        self.assertIn("p2", ids)
        self.assertNotIn("p3", ids)

    # --- (7) search_track_uri ---
    def test_search_track_uri_retourne_bon_uri(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        spotifyauth._write_tokens("user1", {
            "access_token": "acc",
            "refresh_token": "ref",
            "expires_at": time.time() + 3600,
        })
        resp = FakeResponse(200, {
            "tracks": {
                "items": [
                    {
                        "id": "abc123",
                        "name": "Track", "artists": [{"name": "Artist"}],
                    },
                ],
            },
        })
        self._patch_http(resp)
        uri = spotifyauth.search_track_uri("user1", "Artist", "Track")
        self.assertEqual(uri, "spotify:track:abc123")

    def test_search_track_uri_retourne_none_si_seul_remix_existe(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        spotifyauth._write_tokens("user1", {
            "access_token": "acc",
            "refresh_token": "ref",
            "expires_at": time.time() + 3600,
        })
        resp = FakeResponse(200, {
            "tracks": {
                "items": [
                    {
                        "id": "r1", "name": "Track Remix", "artists": [{"name": "Artist"}],
                    },
                ],
            },
        })
        self._patch_http(resp)
        uri = spotifyauth.search_track_uri("user1", "Artist", "Track")
        self.assertIsNone(uri)

    def test_search_track_uri_retourne_none_si_artiste_ne_correspond_pas(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        spotifyauth._write_tokens("user1", {
            "access_token": "acc",
            "refresh_token": "ref",
            "expires_at": time.time() + 3600,
        })
        resp = FakeResponse(200, {
            "tracks": {
                "items": [
                    {
                        "id": "t1", "name": "Track", "artists": [{"name": "OtherArtist"}],
                    },
                ],
            },
        })
        self._patch_http(resp)
        uri = spotifyauth.search_track_uri("user1", "Artist", "Track")
        self.assertIsNone(uri)

    def test_search_track_uri_retourne_none_si_titre_vide(self):
        self._patch_env({
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://x.test/oauth/spotify/callback",
        })
        spotifyauth._write_tokens("user1", {
            "access_token": "acc",
            "refresh_token": "ref",
            "expires_at": time.time() + 3600,
        })
        resp = FakeResponse(200, {"tracks": {"items": []}})
        self._patch_http(resp)
        uri = spotifyauth.search_track_uri("user1", "Artist", "")
        self.assertIsNone(uri)


if __name__ == "__main__":
    unittest.main()
