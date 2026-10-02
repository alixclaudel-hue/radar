"""Tests hors ligne de la couche API Spotify de radar_web/radar/spotifyauth.py.

Aucun appel réseau réel : `spotifyauth._http` (donc `requests`) est remplacé par un
faux objet réponse, `spotifyauth.paths.user_dir` pointe vers un répertoire temporaire
et l'horloge est figée via `spotifyauth.time`. Chaque test isole `os.environ`.

Couvre : access_token (rafraîchissement), list_playlists (filtrage + pagination +
plafond), search_track_uri (sélection stricte) et add_to_playlist (statuts HTTP).

Lancer : python3 -m unittest tests.test_spotify_auth_api -v
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="radar-test-spotifyauth-api-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

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
        self.tmp = tempfile.mkdtemp(prefix="radar-spotify-api-uid-")
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

    def _jetons(self, uid):
        """Écrit des jetons valides (jamais expirés pour l'horloge figée)."""
        self._ecrit_fichier(uid, {
            "access_token": "acc",
            "refresh_token": "ref",
            "expires_at": self.MAINTENANT + 3600,
        })
        return uid


class TestAccessToken(BaseSpotify):
    def test_jeton_valide_n_appelle_pas_le_reseau(self):
        self._ecrit_fichier("alice", {"access_token": "bon",
                                      "refresh_token": "ref",
                                      "expires_at": self.MAINTENANT + 3600})
        with self._http_faux(FauxReponse(200, {})) as http:
            self.assertEqual(spotifyauth.access_token("alice"), "bon")
        http.assert_not_called()

    def test_jeton_juste_au_dessus_de_la_marge_n_est_pas_rafraichi(self):
        self._ecrit_fichier("alice", {
            "access_token": "bon",
            "refresh_token": "ref",
            "expires_at": self.MAINTENANT + spotifyauth.TOKEN_REFRESH_BUFFER + 1,
        })
        with self._http_faux(FauxReponse(200, {})) as http:
            self.assertEqual(spotifyauth.access_token("alice"), "bon")
        http.assert_not_called()

    def test_jeton_a_la_limite_de_la_marge_est_rafraichi(self):
        self._ecrit_fichier("alice", {
            "access_token": "vieux",
            "refresh_token": "ref",
            "expires_at": self.MAINTENANT + spotifyauth.TOKEN_REFRESH_BUFFER,
        })
        reponse = FauxReponse(200, {"access_token": "neuf", "expires_in": 3600})
        with self._http_faux(reponse) as http:
            self.assertEqual(spotifyauth.access_token("alice"), "neuf")
        self.assertEqual(http.call_count, 1)

    def test_jeton_expire_est_rafraichi_et_garde_l_ancien_refresh_token(self):
        self._ecrit_fichier("bob", {"access_token": "vieux",
                                     "refresh_token": "ref-ancien",
                                     "expires_at": self.MAINTENANT - 10})
        # Une réponse de refresh SANS refresh_token : l'ancien doit être conservé.
        reponse = FauxReponse(200, {"access_token": "neuf", "expires_in": 3600})
        with self._http_faux(reponse) as http:
            self.assertEqual(spotifyauth.access_token("bob"), "neuf")

        args, kwargs = http.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], spotifyauth.TOKEN_ENDPOINT)
        self.assertEqual(kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(kwargs["data"]["refresh_token"], "ref-ancien")

        data = self._lit_fichier("bob")
        self.assertEqual(data["access_token"], "neuf")
        self.assertEqual(data["refresh_token"], "ref-ancien")
        self.assertEqual(data["expires_at"], self.MAINTENANT + 3600)
        self.assertEqual(stat.S_IMODE(os.stat(self._token_file("bob")).st_mode), 0o600)

    def test_jeton_expire_avec_nouveau_refresh_token_le_remplace(self):
        self._ecrit_fichier("bob", {"access_token": "vieux",
                                     "refresh_token": "ref-ancien",
                                     "expires_at": self.MAINTENANT - 10})
        reponse = FauxReponse(200, {"access_token": "neuf",
                                    "refresh_token": "ref-neuf",
                                    "expires_in": 120})
        with self._http_faux(reponse):
            self.assertEqual(spotifyauth.access_token("bob"), "neuf")
        data = self._lit_fichier("bob")
        self.assertEqual(data["refresh_token"], "ref-neuf")
        self.assertEqual(data["expires_at"], self.MAINTENANT + 120)

    def test_expires_in_absent_utilise_3600(self):
        self._ecrit_fichier("bob", {"access_token": "vieux",
                                     "refresh_token": "ref",
                                     "expires_at": 1})
        reponse = FauxReponse(200, {"access_token": "neuf"})
        with self._http_faux(reponse):
            spotifyauth.access_token("bob")
        self.assertEqual(self._lit_fichier("bob")["expires_at"],
                         self.MAINTENANT + 3600)

    def test_invalid_grant_supprime_le_fichier_et_leve(self):
        self._ecrit_fichier("carol", {"access_token": "vieux",
                                       "refresh_token": "ref-morte",
                                       "expires_at": self.MAINTENANT - 10})
        reponse = FauxReponse(400, {"error": "invalid_grant",
                                    "error_description": "refresh token révoqué"})
        with self._http_faux(reponse):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.access_token("carol")
        self.assertIn("reconnecte", str(ctx.exception))
        self.assertFalse(os.path.exists(self._token_file("carol")))
        self.assertFalse(spotifyauth.is_connected("carol"))

    def test_400_autre_erreur_ne_supprime_pas_le_fichier(self):
        self._ecrit_fichier("carol", {"access_token": "vieux",
                                       "refresh_token": "ref",
                                       "expires_at": self.MAINTENANT - 10})
        reponse = FauxReponse(400, {"error": "invalid_client",
                                    "error_description": "client inconnu"})
        with self._http_faux(reponse):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.access_token("carol")
        self.assertEqual(str(ctx.exception), "client inconnu")
        self.assertTrue(os.path.exists(self._token_file("carol")))

    def test_reponse_non_json_leve_spotify_error(self):
        self._ecrit_fichier("dave", {"access_token": "vieux",
                                      "refresh_token": "ref",
                                      "expires_at": self.MAINTENANT - 10})
        with self._http_faux(FauxReponse(503, None)):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.access_token("dave")
        self.assertEqual(str(ctx.exception), "Erreur API Spotify")

    def test_sans_fichier_leve_non_connecte_sans_appel_reseau(self):
        http = mock.Mock()
        with mock.patch.object(spotifyauth, "_http", http):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.access_token("inconnu")
        self.assertIn("non connecté", str(ctx.exception))
        http.assert_not_called()

    def test_fichier_corrompu_leve_non_connecte(self):
        self._ecrit_fichier("dave", "{ pas du json ")
        with self._http_faux(FauxReponse(200, {})) as http:
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.access_token("dave")
        self.assertIn("non connecté", str(ctx.exception))
        http.assert_not_called()


class TestListePlaylists(BaseSpotify):
    def setUp(self):
        super().setUp()
        self.uid = self._jetons("u1")

    def _mock_http(self, fonction):
        http = mock.Mock(side_effect=fonction)
        p = mock.patch.object(spotifyauth, "_http", http)
        p.start()
        self.addCleanup(p.stop)
        return http

    @staticmethod
    def _playlist(pid, owner="u1", collab=False, nom=None):
        item = {"id": pid, "owner": {"id": owner}, "collaborative": collab}
        if nom is not None:
            item["name"] = nom
        return item

    def test_ne_garde_que_les_playlists_modifiables(self):
        def reponse(method, url, **kw):
            if url.endswith("/me"):
                return FauxReponse(200, {"id": "u1"})
            return FauxReponse(200, {"items": [
                self._playlist("p1", owner="u1", nom="Mienne"),
                self._playlist("p2", owner="autre", nom="Suivie"),
                self._playlist("p3", owner="autre", collab=True, nom="Collab"),
                self._playlist("p4", owner="u1", nom="Aussi mienne"),
            ], "next": None})

        http = self._mock_http(reponse)
        resultat = spotifyauth.list_playlists(self.uid)
        self.assertEqual(resultat, [{"id": "p1", "name": "Mienne"},
                                    {"id": "p3", "name": "Collab"},
                                    {"id": "p4", "name": "Aussi mienne"}])
        self.assertEqual(http.call_count, 2)
        args_me, kwargs_me = http.call_args_list[0]
        self.assertEqual(args_me, ("GET", spotifyauth.API_BASE + "/me"))
        self.assertEqual(kwargs_me["headers"]["Authorization"], "Bearer acc")

    def test_s_arrete_quand_next_est_none(self):
        offsets = []

        def reponse(method, url, **kw):
            if url.endswith("/me"):
                return FauxReponse(200, {"id": "u1"})
            offsets.append(kw["params"]["offset"])
            return FauxReponse(200, {"items": [self._playlist("p1", owner="u1")],
                                     "next": None})

        http = self._mock_http(reponse)
        resultat = spotifyauth.list_playlists(self.uid)
        self.assertEqual([p["id"] for p in resultat], ["p1"])
        self.assertEqual(offsets, [0])
        self.assertEqual(http.call_count, 2)
        _, kwargs = http.call_args_list[1]
        self.assertEqual(kwargs["params"]["limit"], spotifyauth.PAGE_SIZE)

    def test_page_vide_arrete_la_pagination(self):
        def reponse(method, url, **kw):
            if url.endswith("/me"):
                return FauxReponse(200, {"id": "u1"})
            return FauxReponse(200, {"items": [],
                                     "next": "https://api.spotify.com/v1/next"})

        http = self._mock_http(reponse)
        self.assertEqual(spotifyauth.list_playlists(self.uid), [])
        self.assertEqual(http.call_count, 2)

    def test_le_plafond_max_playlists_est_respecte(self):
        offsets = []

        def reponse(method, url, **kw):
            if url.endswith("/me"):
                return FauxReponse(200, {"id": "u1"})
            offset = kw["params"]["offset"]
            offsets.append(offset)
            items = [self._playlist(f"p{offset + i}", owner="u1")
                     for i in range(spotifyauth.PAGE_SIZE)]
            # `next` toujours présent : seule la borne MAX_PLAYLISTS arrête la boucle.
            return FauxReponse(200, {"items": items,
                                     "next": "https://api.spotify.com/v1/next"})

        http = self._mock_http(reponse)
        resultat = spotifyauth.list_playlists(self.uid)
        self.assertEqual(spotifyauth.MAX_PLAYLISTS, 200)
        self.assertEqual(len(resultat), spotifyauth.MAX_PLAYLISTS)
        self.assertEqual(resultat[0]["id"], "p0")
        self.assertEqual(resultat[-1]["id"], "p199")
        self.assertEqual(offsets, [0, 50, 100, 150])
        self.assertEqual(http.call_count, 1 + 4)

    def test_playlist_sans_nom_donne_chaine_vide(self):
        def reponse(method, url, **kw):
            if url.endswith("/me"):
                return FauxReponse(200, {"id": "u1"})
            return FauxReponse(200, {"items": [self._playlist("p1", owner="u1")],
                                     "next": None})

        self._mock_http(reponse)
        self.assertEqual(spotifyauth.list_playlists(self.uid),
                         [{"id": "p1", "name": ""}])

    def test_item_nul_ignore(self):
        def reponse(method, url, **kw):
            if url.endswith("/me"):
                return FauxReponse(200, {"id": "u1"})
            return FauxReponse(200, {"items": [None, self._playlist("p1", owner="u1")],
                                     "next": None})

        self._mock_http(reponse)
        self.assertEqual(len(spotifyauth.list_playlists(self.uid)), 1)

    def test_erreur_sur_me_leve_spotify_error(self):
        reponse = FauxReponse(403, {"error": {"status": 403,
                                               "message": "Accès refusé"}})
        with self._http_faux(reponse):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.list_playlists(self.uid)
        self.assertEqual(str(ctx.exception), "Accès refusé")

    def test_me_sans_id_leve_spotify_error(self):
        with self._http_faux(FauxReponse(200, {"display_name": "Zoe"})):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.list_playlists(self.uid)
        self.assertIn("inattendue", str(ctx.exception))

    def test_erreur_sur_la_page_de_playlists_leve_spotify_error(self):
        def reponse(method, url, **kw):
            if url.endswith("/me"):
                return FauxReponse(200, {"id": "u1"})
            return FauxReponse(500, {"error": {"status": 500,
                                                "message": "Boom"}})

        self._mock_http(reponse)
        with self.assertRaises(spotifyauth.SpotifyError) as ctx:
            spotifyauth.list_playlists(self.uid)
        self.assertEqual(str(ctx.exception), "Boom")

    def test_sans_jetons_leve_non_connecte(self):
        http = mock.Mock()
        with mock.patch.object(spotifyauth, "_http", http):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.list_playlists("inconnu")
        self.assertIn("non connecté", str(ctx.exception))
        http.assert_not_called()


class TestRecherchePiste(BaseSpotify):
    def setUp(self):
        super().setUp()
        self.uid = self._jetons("zoe")

    def test_retourne_l_uri_de_la_piste_correspondante(self):
        reponse = FauxReponse(200, {"tracks": {"items": [
            {"id": "T1", "name": "Don't Hurt", "artists": [{"name": "Beyoncé"}]},
        ]}})
        with self._http_faux(reponse) as http:
            uri = spotifyauth.search_track_uri(self.uid, "Beyonce", "Don't Hurt")
        self.assertEqual(uri, "spotify:track:T1")
        args, kwargs = http.call_args
        self.assertEqual(args, ("GET", spotifyauth.API_BASE + "/search"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer acc")
        self.assertEqual(kwargs["params"]["type"], "track")
        self.assertEqual(kwargs["params"]["limit"], 10)
        self.assertIn('track:"Don\'t Hurt"', kwargs["params"]["q"])
        self.assertIn('artist:"Beyonce"', kwargs["params"]["q"])

    def test_none_si_seul_un_remix_existe(self):
        reponse = FauxReponse(200, {"tracks": {"items": [
            {"id": "T1", "name": "Track (Remix)", "artists": [{"name": "Artiste"}]},
        ]}})
        with self._http_faux(reponse):
            self.assertIsNone(spotifyauth.search_track_uri(self.uid, "Artiste", "Track"))

    def test_none_si_l_artiste_ne_correspond_pas(self):
        reponse = FauxReponse(200, {"tracks": {"items": [
            {"id": "T1", "name": "Track", "artists": [{"name": "Autre"}]},
        ]}})
        with self._http_faux(reponse):
            self.assertIsNone(spotifyauth.search_track_uri(self.uid, "Artiste", "Track"))

    def test_none_si_le_titre_ne_correspond_pas(self):
        reponse = FauxReponse(200, {"tracks": {"items": [
            {"id": "T1", "name": "Autre chose", "artists": [{"name": "Artiste"}]},
        ]}})
        with self._http_faux(reponse):
            self.assertIsNone(spotifyauth.search_track_uri(self.uid, "Artiste", "Track"))

    def test_titre_vide_aucun_appel_reseau(self):
        with self._http_faux(FauxReponse(200, {})) as http:
            self.assertIsNone(spotifyauth.search_track_uri(self.uid, "Artiste", "   "))
        http.assert_not_called()

    def test_titre_none_aucun_appel_reseau(self):
        with self._http_faux(FauxReponse(200, {})) as http:
            self.assertIsNone(spotifyauth.search_track_uri(self.uid, "Artiste", None))
        http.assert_not_called()

    def test_artiste_vide_requete_sans_clause_artiste(self):
        reponse = FauxReponse(200, {"tracks": {"items": [
            {"id": "T1", "name": "Track", "artists": [{"name": "N'importe qui"}]},
        ]}})
        with self._http_faux(reponse) as http:
            uri = spotifyauth.search_track_uri(self.uid, "  ", "Track")
        self.assertEqual(uri, "spotify:track:T1")
        self.assertEqual(http.call_args[1]["params"]["q"], 'track:"Track"')

    def test_ignore_les_items_nuls_et_sans_id(self):
        reponse = FauxReponse(200, {"tracks": {"items": [
            None,
            {"name": "Track", "artists": [{"name": "Artiste"}]},
            {"id": "T9", "name": "Track", "artists": [{"name": "Artiste"}]},
        ]}})
        with self._http_faux(reponse):
            self.assertEqual(spotifyauth.search_track_uri(self.uid, "Artiste", "Track"),
                             "spotify:track:T9")

    def test_sans_resultat_retourne_none(self):
        with self._http_faux(FauxReponse(200, {"tracks": {"items": []}})):
            self.assertIsNone(spotifyauth.search_track_uri(self.uid, "Artiste", "Track"))
        with self._http_faux(FauxReponse(200, {})):
            self.assertIsNone(spotifyauth.search_track_uri(self.uid, "Artiste", "Track"))

    def test_erreur_http_leve_spotify_error(self):
        reponse = FauxReponse(429, {"error": {"status": 429,
                                               "message": "Trop de requêtes"}})
        with self._http_faux(reponse):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.search_track_uri(self.uid, "Artiste", "Track")
        self.assertEqual(str(ctx.exception), "Trop de requêtes")

    def test_reponse_non_json_leve_spotify_error(self):
        with self._http_faux(FauxReponse(200, None)):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.search_track_uri(self.uid, "Artiste", "Track")
        self.assertIn("inattendue", str(ctx.exception))

    def test_sans_jetons_leve_non_connecte(self):
        http = mock.Mock()
        with mock.patch.object(spotifyauth, "_http", http):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.search_track_uri("inconnu", "Artiste", "Track")
        self.assertIn("non connecté", str(ctx.exception))
        http.assert_not_called()


class TestAjoutPlaylist(BaseSpotify):
    def setUp(self):
        super().setUp()
        self.uid = self._jetons("u1")
        self.uri = "spotify:track:T1"

    def test_201_ok_le_corps_contient_uris(self):
        with self._http_faux(FauxReponse(201, {})) as http:
            self.assertIsNone(
                spotifyauth.add_to_playlist(self.uid, "PL1", self.uri))
        args, kwargs = http.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1],
                         spotifyauth.API_BASE + "/playlists/PL1/tracks")
        self.assertEqual(kwargs["json"], {"uris": [self.uri]})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer acc")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")

    def test_200_ok(self):
        with self._http_faux(FauxReponse(200, {})) as http:
            spotifyauth.add_to_playlist(self.uid, "PL1", self.uri)
        self.assertEqual(http.call_count, 1)

    def test_403_leve_spotify_error_avec_message_clair(self):
        reponse = FauxReponse(403, {"error": {"status": 403, "message": "Interdit"}})
        with self._http_faux(reponse):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.add_to_playlist(self.uid, "PL1", self.uri)
        message = str(ctx.exception)
        self.assertIn("refuse", message)
        self.assertIn("propriétaire", message)
        self.assertIn("collaborative", message)
        self.assertNotIn("Interdit", message)

    def test_404_leve_spotify_error_playlist_introuvable(self):
        reponse = FauxReponse(404, {"error": {"status": 404, "message": "Not found"}})
        with self._http_faux(reponse):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.add_to_playlist(self.uid, "PL1", self.uri)
        self.assertIn("introuvable", str(ctx.exception))

    def test_autre_erreur_utilise_le_message_spotify(self):
        reponse = FauxReponse(500, {"error": {"status": 500,
                                               "message": "Erreur interne"}})
        with self._http_faux(reponse):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.add_to_playlist(self.uid, "PL1", self.uri)
        self.assertEqual(str(ctx.exception), "Erreur interne")

    def test_erreur_non_json_message_generique(self):
        with self._http_faux(FauxReponse(502, None)):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.add_to_playlist(self.uid, "PL1", self.uri)
        self.assertEqual(str(ctx.exception), "Erreur API Spotify")

    def test_uri_transmise_telle_quelle(self):
        with self._http_faux(FauxReponse(201, {})) as http:
            spotifyauth.add_to_playlist(self.uid, "PL2", "")
        self.assertEqual(http.call_args[1]["json"], {"uris": [""]})

    def test_identifiant_de_playlist_insere_dans_l_url(self):
        with self._http_faux(FauxReponse(201, {})) as http:
            spotifyauth.add_to_playlist(self.uid, "a/b c", self.uri)
        self.assertEqual(http.call_args[0][1],
                         spotifyauth.API_BASE + "/playlists/a/b c/tracks")

    def test_sans_jetons_leve_non_connecte_sans_appel_reseau(self):
        http = mock.Mock()
        with mock.patch.object(spotifyauth, "_http", http):
            with self.assertRaises(spotifyauth.SpotifyError) as ctx:
                spotifyauth.add_to_playlist("inconnu", "PL1", self.uri)
        self.assertIn("non connecté", str(ctx.exception))
        http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
