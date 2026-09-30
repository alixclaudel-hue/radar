"""Tests hors ligne de radar_web/radar/spotifyauth.py.

Aucun appel réseau réel : `spotifyauth._http` (donc `requests`) est remplacé par un
faux objet réponse, `spotifyauth.paths.user_dir` pointe vers un répertoire temporaire
et l'horloge est figée via `spotifyauth.time`. Chaque test isole `os.environ`.

Lancer : python3 -m unittest tests.test_spotify_auth_core -v
"""
import base64
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="radar-test-spotifyauth-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

import requests  # noqa: E402

from radar_web.radar import spotifyauth  # noqa: E402


class FauxReponse:
    """Faux `requests.Response` : status_code + json(), jamais de socket."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("aucun corps JSON")
        return self._payload


class BaseSpotify(unittest.TestCase):
    """Socle : disque temporaire, horloge figée, environnement isolé."""

    ENV = {
        "RADAR_SPOTIFY_CLIENT_ID": "cid",
        "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
        "RADAR_SPOTIFY_REDIRECT_URI": "https://radar.test/oauth/spotify/callback",
    }
    MAINTENANT = 1000000.0

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="radar-spotify-uid-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        p = mock.patch.object(spotifyauth.paths, "user_dir", self._user_dir)
        p.start()
        self.addCleanup(p.stop)

        self.clock = mock.Mock()
        self.clock.time.return_value = self.MAINTENANT
        h = mock.patch.object(spotifyauth, "time", self.clock)
        h.start()
        self.addCleanup(h.stop)

        e = mock.patch.dict(os.environ, self.ENV, clear=True)
        e.start()
        self.addCleanup(e.stop)

    def _user_dir(self, uid):
        chemin = os.path.join(self.tmp, uid)
        os.makedirs(chemin, exist_ok=True)
        return chemin

    def _token_file(self, uid):
        return os.path.join(self.tmp, uid, "spotify_oauth.json")

    def _ecrit_fichier(self, uid, contenu):
        chemin = self._token_file(uid)
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        with open(chemin, "w", encoding="utf-8") as f:
            f.write(contenu if isinstance(contenu, str) else json.dumps(contenu))
        return chemin

    def _lit_fichier(self, uid):
        with open(self._token_file(uid), encoding="utf-8") as f:
            return json.load(f)

    def _http_faux(self, reponse):
        """Remplace le seul point de sortie réseau du module."""
        return mock.patch.object(spotifyauth, "_http", mock.Mock(return_value=reponse))


class TestNormalize(unittest.TestCase):
    def test_retire_accents_apostrophes_et_tirets(self):
        self.assertEqual(spotifyauth._normalize("Beyoncé — Don't Hurt"),
                         "beyonce don t hurt")

    def test_chaine_vide(self):
        self.assertEqual(spotifyauth._normalize(""), "")

    def test_none_donne_chaine_vide(self):
        self.assertEqual(spotifyauth._normalize(None), "")

    def test_espaces_normalises(self):
        self.assertEqual(spotifyauth._normalize("  A   B  "), "a b")

    def test_chiffres_et_parentheses(self):
        self.assertEqual(spotifyauth._normalize("Track 99 (Live)"), "track 99 live")


class TestHasUnwanted(unittest.TestCase):
    def test_ecarte_remix_quand_le_titre_demande_ne_le_contient_pas(self):
        self.assertTrue(spotifyauth._has_unwanted("track remix", "track"))

    def test_n_ecarte_pas_edition(self):
        self.assertFalse(spotifyauth._has_unwanted("edition", "edition"))

    def test_n_ecarte_pas_olive(self):
        self.assertFalse(spotifyauth._has_unwanted("olive", "olive"))

    def test_n_ecarte_pas_le_remix_demande(self):
        self.assertFalse(spotifyauth._has_unwanted("track remix", "track remix"))

    def test_ecarte_live_quand_le_titre_demande_est_plus_court(self):
        self.assertTrue(spotifyauth._has_unwanted("track live", "track"))

    def test_chaines_vides(self):
        self.assertFalse(spotifyauth._has_unwanted("", ""))


class TestConfigurationClient(unittest.TestCase):
    def test_configuration_complete(self):
        env = {
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://radar.test/oauth/spotify/callback",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(spotifyauth._get_client_config(),
                             ("cid", "sec", "https://radar.test/oauth/spotify/callback"))
            self.assertTrue(spotifyauth.configured())
            self.assertEqual(spotifyauth.redirect_uri(),
                             "https://radar.test/oauth/spotify/callback")

    def test_repli_sur_radar_domain(self):
        env = {
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_DOMAIN": "radar.example",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            _, _, uri = spotifyauth._get_client_config()
            self.assertEqual(uri, "https://radar.example/oauth/spotify/callback")
            self.assertEqual(spotifyauth.redirect_uri(),
                             "https://radar.example/oauth/spotify/callback")
            self.assertTrue(spotifyauth.configured())

    def test_redirect_uri_explicite_prime_sur_le_domaine(self):
        env = {
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
            "RADAR_SPOTIFY_REDIRECT_URI": "https://perso.test/cb",
            "RADAR_DOMAIN": "radar.example",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            _, _, uri = spotifyauth._get_client_config()
        self.assertEqual(uri, "https://perso.test/cb")

    def test_sans_domaine_ni_redirect_uri(self):
        env = {
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "sec",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            _, _, uri = spotifyauth._get_client_config()
            self.assertEqual(uri, "")
            self.assertFalse(spotifyauth.configured())

    def test_configure_faux_sans_secret(self):
        env = {"RADAR_SPOTIFY_CLIENT_ID": "cid", "RADAR_DOMAIN": "radar.example"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(spotifyauth.configured())
            self.assertEqual(spotifyauth.redirect_uri(),
                             "https://radar.example/oauth/spotify/callback")

    def test_configure_faux_sans_client_id(self):
        env = {"RADAR_SPOTIFY_CLIENT_SECRET": "sec", "RADAR_DOMAIN": "radar.example"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(spotifyauth.configured())

    def test_environnement_entierement_vide(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(spotifyauth._get_client_config(), ("", "", ""))
            self.assertFalse(spotifyauth.configured())

    def test_auth_url_ne_divulgue_pas_le_secret(self):
        env = {
            "RADAR_SPOTIFY_CLIENT_ID": "cid",
            "RADAR_SPOTIFY_CLIENT_SECRET": "s3cr3t",
            "RADAR_DOMAIN": "radar.example",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            url = spotifyauth.auth_url("etat-signe")
        self.assertTrue(url.startswith(spotifyauth.AUTH_ENDPOINT + "?"))
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(params["client_id"], ["cid"])
        self.assertEqual(params["state"], ["etat-signe"])
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["redirect_uri"],
                         ["https://radar.example/oauth/spotify/callback"])
        self.assertIn("playlist-modify-private", params["scope"][0])
        self.assertNotIn("s3cr3t", url)


class TestExchangeCode(BaseSpotify):
    def test_ecrit_les_jetons_en_0600(self):
        reponse = FauxReponse(200, {"access_token": "acc-1",
                                    "refresh_token": "ref-1",
                                    "expires_in": 3600})
        with self._http_faux(reponse) as http:
            spotifyauth.exchange_code("alice", "code-abc")

        chemin = self._token_file("alice")
        self.assertTrue(os.path.isfile(chemin))
        data = self._lit_fichier("alice")
        self.assertEqual(data["access_token"], "acc-1")
        self.assertEqual(data["refresh_token"], "ref-1")
        self.assertEqual(data["expires_at"], self.MAINTENANT + 3600)
        self.assertEqual(stat.S_IMODE(os.stat(chemin).st_mode), 0o600)
        self.assertFalse(os.path.exists(chemin + ".tmp"))

        args, kwargs = http.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], spotifyauth.TOKEN_ENDPOINT)
        self.assertNotIn("code-abc", args[1])
        self.assertEqual(kwargs["data"]["code"], "code-abc")
        self.assertEqual(kwargs["data"]["grant_type"], "authorization_code")
        self.assertEqual(kwargs["data"]["redirect_uri"],
                         self.ENV["RADAR_SPOTIFY_REDIRECT_URI"])
        entete = kwargs["headers"]["Authorization"]
        self.assertTrue(entete.startswith("Basic "))
        raw = base64.b64decode(entete.split(" ", 1)[1]).decode("utf-8")
        self.assertEqual(raw, "cid:sec")

    def test_expires_in_absent_utilise_3600(self):
        reponse = FauxReponse(200, {"access_token": "acc", "refresh_token": "ref"})
        with self._http_faux(reponse):
            spotifyauth.exchange_code("alice", "code")
        self.assertEqual(self._lit_fichier("alice")["expires_at"],
                         self.MAINTENANT + 3600)

    def test_sans_refresh_token_et_rien_en_stock_leve_spotify_error(self):
        reponse = FauxReponse(200, {"access_token": "acc-1", "expires_in": 60})
        with self._http_faux(reponse):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.exchange_code("bob", "code")
        self.assertIn("refresh_token", str(ctx.exception))
        self.assertFalse(os.path.exists(self._token_file("bob")))

    def test_sans_refresh_token_garde_l_ancien(self):
        self._ecrit_fichier("carol", {"access_token": "vieux-acc",
                                       "refresh_token": "vieux-ref",
                                       "expires_at": 1})
        reponse = FauxReponse(200, {"access_token": "neuf-acc", "expires_in": 60})
        with self._http_faux(reponse):
            spotifyauth.exchange_code("carol", "code")
        data = self._lit_fichier("carol")
        self.assertEqual(data["refresh_token"], "vieux-ref")
        self.assertEqual(data["access_token"], "neuf-acc")
        self.assertEqual(data["expires_at"], self.MAINTENANT + 60)

    def test_erreur_token_http_leve_spotify_error_sans_ecrire(self):
        reponse = FauxReponse(400, {"error": "invalid_grant",
                                    "error_description": "code déjà utilisé"})
        with self._http_faux(reponse):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.exchange_code("dave", "code")
        self.assertIn("reconnecte", str(ctx.exception))
        self.assertFalse(os.path.exists(self._token_file("dave")))

    def test_erreur_api_forme_dict(self):
        reponse = FauxReponse(403, {"error": {"status": 403, "message": "Accès refusé"}})
        with self._http_faux(reponse):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.exchange_code("dave", "code")
        self.assertEqual(str(ctx.exception), "Accès refusé")

    def test_reponse_non_json_leve_spotify_error(self):
        with self._http_faux(FauxReponse(503, None)):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.exchange_code("eve", "code")
        self.assertEqual(str(ctx.exception), "Erreur API Spotify")
        self.assertFalse(os.path.exists(self._token_file("eve")))

    def test_200_sans_access_token_leve_spotify_error(self):
        reponse = FauxReponse(200, {"refresh_token": "ref"})
        with self._http_faux(reponse):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.exchange_code("eve", "code")
        self.assertIn("inattendue", str(ctx.exception))
        self.assertFalse(os.path.exists(self._token_file("eve")))

    def test_code_vide_transmis_tel_quel(self):
        reponse = FauxReponse(200, {"access_token": "a", "refresh_token": "r"})
        with self._http_faux(reponse) as http:
            spotifyauth.exchange_code("frank", "")
        self.assertEqual(http.call_args[1]["data"]["code"], "")


class TestStockageJetons(BaseSpotify):
    def test_is_connected_faux_sans_fichier(self):
        self.assertFalse(spotifyauth.is_connected("dave"))
        self.assertIsNone(spotifyauth._read_tokens("dave"))

    def test_is_connected_vrai_avec_fichier_valide(self):
        self._ecrit_fichier("dave", {"access_token": "a", "refresh_token": "r",
                                      "expires_at": 42})
        self.assertTrue(spotifyauth.is_connected("dave"))
        self.assertEqual(spotifyauth._read_tokens("dave")["access_token"], "a")

    def test_fichier_json_corrompu_ignore(self):
        self._ecrit_fichier("dave", "{ ceci n'est pas du json")
        self.assertIsNone(spotifyauth._read_tokens("dave"))
        self.assertFalse(spotifyauth.is_connected("dave"))

    def test_fichier_incomplet_ignore(self):
        self._ecrit_fichier("dave", {"access_token": "a", "expires_at": 42})
        self.assertIsNone(spotifyauth._read_tokens("dave"))

    def test_fichier_vide_ignore(self):
        self._ecrit_fichier("dave", "")
        self.assertIsNone(spotifyauth._read_tokens("dave"))

    def test_write_tokens_cree_le_fichier_en_0600(self):
        spotifyauth._write_tokens("gina", {"access_token": "a", "refresh_token": "r",
                                            "expires_at": 42})
        chemin = self._token_file("gina")
        self.assertEqual(stat.S_IMODE(os.stat(chemin).st_mode), 0o600)
        self.assertFalse(os.path.exists(chemin + ".tmp"))

    def test_disconnect_supprime_le_fichier_et_est_idempotent(self):
        self._ecrit_fichier("dave", {"access_token": "a", "refresh_token": "r",
                                      "expires_at": 42})
        spotifyauth.disconnect("dave")
        self.assertFalse(os.path.exists(self._token_file("dave")))
        self.assertFalse(spotifyauth.is_connected("dave"))
        spotifyauth.disconnect("dave")


class TestRecherchePiste(BaseSpotify):
    def _avec_jetons(self, uid="zoe"):
        self._ecrit_fichier(uid, {"access_token": "acc", "refresh_token": "ref",
                                  "expires_at": self.MAINTENANT + 3600})
        return uid

    def test_retourne_l_uri_de_la_piste_correspondante(self):
        uid = self._avec_jetons()
        reponse = FauxReponse(200, {"tracks": {"items": [
            {"id": "T1", "name": "Don't Hurt", "artists": [{"name": "Beyoncé"}]},
        ]}})
        with self._http_faux(reponse):
            uri = spotifyauth.search_track_uri(uid, "Beyonce", "Don't Hurt")
        self.assertEqual(uri, "spotify:track:T1")

    def test_ignore_les_remix_et_retourne_none(self):
        uid = self._avec_jetons()
        reponse = FauxReponse(200, {"tracks": {"items": [
            {"id": "T1", "name": "Track (Remix)", "artists": [{"name": "Artiste"}]},
        ]}})
        with self._http_faux(reponse):
            uri = spotifyauth.search_track_uri(uid, "Artiste", "Track")
        self.assertIsNone(uri)

    def test_titre_vide_aucun_appel_reseau(self):
        uid = self._avec_jetons()
        with self._http_faux(FauxReponse(200, {})) as http:
            self.assertIsNone(spotifyauth.search_track_uri(uid, "Artiste", "   "))
        http.assert_not_called()


class TestHttpHorsLigne(unittest.TestCase):
    def test_panne_reseau_devient_spotify_error(self):
        with mock.patch.object(spotifyauth.requests, "request",
                               side_effect=requests.ConnectionError("dns")) as req:
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth._http("GET", spotifyauth.API_BASE + "/me")
        self.assertIn("injoignable", str(ctx.exception))
        req.assert_called_once_with("GET", spotifyauth.API_BASE + "/me",
                                    timeout=spotifyauth.REQUEST_TIMEOUT)

    def test_timeout_devient_spotify_error(self):
        with mock.patch.object(spotifyauth.requests, "request",
                               side_effect=requests.Timeout("trop lent")):
            with self.assertRaises(spotifyauth.SpotifyError):
                spotifyauth._http("POST", spotifyauth.TOKEN_ENDPOINT)

    def test_succes_retourne_la_reponse_telle_quelle(self):
        reponse = FauxReponse(200, {"ok": True})
        with mock.patch.object(spotifyauth.requests, "request", return_value=reponse):
            self.assertIs(spotifyauth._http("GET", spotifyauth.API_BASE + "/me"), reponse)


if __name__ == "__main__":
    unittest.main()
