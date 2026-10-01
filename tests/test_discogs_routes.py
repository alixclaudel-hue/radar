'''Tests des routes OAuth Discogs.

Aucun réseau ni stockage OAuth réel : les fonctions concernées sont mockées et
le répertoire de données de l’application est temporaire.

Lancer : python3 -m unittest tests.test_discogs_routes -v
'''
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_REPERTOIRE_TEMPORAIRE = tempfile.TemporaryDirectory(prefix='radar-test-discogs-')
os.environ['CRATE_DATA_DIR'] = _REPERTOIRE_TEMPORAIRE.name

from fastapi.testclient import TestClient  # noqa: E402

from radar_web import app as appmod  # noqa: E402
from radar_web.radar import discogsauth, googleauth  # noqa: E402

JETON_TEST = 'jeton-test'
VERIFICATEUR_TEST = 'verificateur-test'
URL_AUTORISATION_TEST = (
    'https://www.discogs.com/oauth/authorize?oauth_token=RT'
)


class RoutesDiscogsTest(unittest.TestCase):
    def setUp(self):
        environnement = mock.patch.dict(
            os.environ,
            {
                'RADAR_DISCOGS_CONSUMER_KEY': 'cle-de-test',
                'RADAR_DISCOGS_CONSUMER_SECRET': 'secret-de-test',
                'RADAR_DISCOGS_REDIRECT_URI': (
                    'https://radar.test/oauth/discogs/callback'
                ),
                'RADAR_DOMAIN': '',
            },
            clear=False,
        )
        environnement.start()
        self.addCleanup(environnement.stop)

        garde_reseau = mock.patch(
            'requests.sessions.Session.request',
            side_effect=AssertionError('Appel réseau interdit dans les tests'),
        )
        garde_reseau.start()
        self.addCleanup(garde_reseau.stop)

        horloge = mock.Mock()
        horloge.time.return_value = 1_700_000_000.0
        horloge_discogs = mock.patch.object(discogsauth, 'time', horloge)
        horloge_discogs.start()
        self.addCleanup(horloge_discogs.stop)

        self.client = TestClient(
            appmod.app,
            raise_server_exceptions=False,
        )
        self.addCleanup(self.client.close)

        mode_development = mock.patch.object(
            appmod.websession,
            'dev_mode',
            return_value=True,
        )
        mode_development.start()
        self.addCleanup(mode_development.stop)

        self._patch_discogs(
            is_connected=mock.Mock(return_value=True),
            credential=mock.Mock(
                return_value='oauth:jeton-test:secret-de-test'
            ),
            _read_private_json=mock.Mock(
                side_effect=AssertionError(
                    'Lecture OAuth sur disque interdite dans les tests'
                )
            ),
            _write_private_json=mock.Mock(
                side_effect=AssertionError(
                    'Écriture OAuth sur disque interdite dans les tests'
                )
            ),
            _delete_private_file=mock.Mock(
                side_effect=AssertionError(
                    'Suppression OAuth sur disque interdite dans les tests'
                )
            ),
        )

        for nom, valeur in (
            ('configured', False),
            ('is_connected', False),
        ):
            patch_google = mock.patch.object(
                googleauth,
                nom,
                return_value=valeur,
            )
            patch_google.start()
            self.addCleanup(patch_google.stop)

    def _patch_discogs(self, **methodes):
        for nom, valeur in methodes.items():
            patch = mock.patch.object(discogsauth, nom, valeur)
            patch.start()
            self.addCleanup(patch.stop)

    def test_demarrage_sans_configuration_redirige_vers_unconfigured(self):
        demarrage = mock.Mock()
        self._patch_discogs(start=demarrage)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('RADAR_DISCOGS_CONSUMER_KEY', None)
            os.environ.pop('RADAR_DISCOGS_CONSUMER_SECRET', None)
            self.assertFalse(discogsauth.configured())
            reponse = self.client.get(
                '/oauth/discogs/start',
                follow_redirects=False,
            )

        self.assertEqual(reponse.status_code, 303)
        self.assertEqual(
            reponse.headers['location'],
            '/patte?discogs=unconfigured',
        )
        demarrage.assert_not_called()

    def test_demarrage_configure_redirige_vers_une_url_discogs(self):
        demarrage = mock.Mock(return_value=URL_AUTORISATION_TEST)
        self._patch_discogs(start=demarrage)

        self.assertTrue(discogsauth.configured())
        reponse = self.client.get(
            '/oauth/discogs/start',
            follow_redirects=False,
        )

        self.assertEqual(reponse.status_code, 303)
        self.assertEqual(
            reponse.headers['location'],
            URL_AUTORISATION_TEST,
        )
        demarrage.assert_called_once_with('owner')

    def test_erreur_de_demarrage_redirige_vers_error(self):
        demarrage = mock.Mock(
            side_effect=discogsauth.DiscogsError('échec simulé')
        )
        self._patch_discogs(start=demarrage)

        reponse = self.client.get(
            '/oauth/discogs/start',
            follow_redirects=False,
        )

        self.assertEqual(reponse.status_code, 303)
        self.assertEqual(
            reponse.headers['location'],
            '/patte?discogs=error',
        )
        demarrage.assert_called_once_with('owner')

    def test_callback_refuse_si_complete_retourne_faux(self):
        complete = mock.Mock(return_value=False)
        self._patch_discogs(complete=complete)

        reponse = self.client.get(
            '/oauth/discogs/callback'
            f'?oauth_token={JETON_TEST}'
            f'&oauth_verifier={VERIFICATEUR_TEST}',
            follow_redirects=False,
        )

        self.assertEqual(reponse.status_code, 303)
        self.assertEqual(
            reponse.headers['location'],
            '/patte?discogs=refused',
        )
        complete.assert_called_once_with(
            'owner',
            JETON_TEST,
            VERIFICATEUR_TEST,
        )

    def test_callback_refuse_les_valeurs_fausses_de_complete(self):
        complete = mock.Mock()
        self._patch_discogs(complete=complete)

        for resultat, libelle in (
            (None, 'None'),
            ([], 'liste vide'),
            (0, 'zéro'),
        ):
            with self.subTest(valeur=libelle):
                complete.reset_mock()
                complete.return_value = resultat
                reponse = self.client.get(
                    '/oauth/discogs/callback'
                    f'?oauth_token={JETON_TEST}'
                    f'&oauth_verifier={VERIFICATEUR_TEST}',
                    follow_redirects=False,
                )

                self.assertEqual(reponse.status_code, 303)
                self.assertEqual(
                    reponse.headers['location'],
                    '/patte?discogs=refused',
                )
                complete.assert_called_once_with(
                    'owner',
                    JETON_TEST,
                    VERIFICATEUR_TEST,
                )

    def test_callback_accepte_si_complete_retourne_vrai(self):
        complete = mock.Mock(return_value=True)
        self._patch_discogs(complete=complete)

        reponse = self.client.get(
            '/oauth/discogs/callback'
            f'?oauth_token={JETON_TEST}'
            f'&oauth_verifier={VERIFICATEUR_TEST}',
            follow_redirects=False,
        )

        self.assertEqual(reponse.status_code, 303)
        self.assertEqual(
            reponse.headers['location'],
            '/patte?discogs=ok',
        )
        complete.assert_called_once_with(
            'owner',
            JETON_TEST,
            VERIFICATEUR_TEST,
        )

    def test_callback_erreur_discogs_redirige_vers_error(self):
        complete = mock.Mock(
            side_effect=discogsauth.DiscogsError('échec simulé')
        )
        self._patch_discogs(complete=complete)

        reponse = self.client.get(
            '/oauth/discogs/callback'
            f'?oauth_token={JETON_TEST}'
            f'&oauth_verifier={VERIFICATEUR_TEST}',
            follow_redirects=False,
        )

        self.assertEqual(reponse.status_code, 303)
        self.assertEqual(
            reponse.headers['location'],
            '/patte?discogs=error',
        )
        complete.assert_called_once_with(
            'owner',
            JETON_TEST,
            VERIFICATEUR_TEST,
        )

    def test_callback_denied_refuse_sans_appeler_complete(self):
        complete = mock.Mock()
        self._patch_discogs(complete=complete)

        reponse = self.client.get(
            '/oauth/discogs/callback'
            f'?oauth_token={JETON_TEST}'
            f'&oauth_verifier={VERIFICATEUR_TEST}'
            '&denied=1',
            follow_redirects=False,
        )

        self.assertEqual(reponse.status_code, 303)
        self.assertEqual(
            reponse.headers['location'],
            '/patte?discogs=refused',
        )
        complete.assert_not_called()

    def test_callback_sans_verificateur_refuse_sans_appeler_complete(self):
        complete = mock.Mock()
        self._patch_discogs(complete=complete)

        urls = (
            (
                'verificateur absent',
                f'/oauth/discogs/callback?oauth_token={JETON_TEST}',
            ),
            (
                'verificateur vide',
                f'/oauth/discogs/callback?oauth_token={JETON_TEST}'
                '&oauth_verifier=',
            ),
        )

        for libelle, url in urls:
            with self.subTest(cas=libelle):
                reponse = self.client.get(
                    url,
                    follow_redirects=False,
                )

                self.assertEqual(reponse.status_code, 303)
                self.assertEqual(
                    reponse.headers['location'],
                    '/patte?discogs=refused',
                )

        complete.assert_not_called()

    def test_deconnexion_appelle_discogsauth_puis_redirige(self):
        deconnexion = mock.Mock()
        self._patch_discogs(disconnect=deconnexion)

        reponse = self.client.post(
            '/oauth/discogs/disconnect',
            follow_redirects=False,
        )

        self.assertEqual(reponse.status_code, 303)
        self.assertEqual(
            reponse.headers['location'],
            '/patte?discogs=off',
        )
        deconnexion.assert_called_once_with('owner')

    def test_patte_annonce_la_reconnexion_discogs(self):
        self._patch_discogs(
            configured=mock.Mock(return_value=True),
        )

        reponse = self.client.get('/patte?discogs=ok')

        self.assertEqual(reponse.status_code, 200)
        self.assertIn('Compte Discogs relié', reponse.text)


if __name__ == '__main__':
    unittest.main()
