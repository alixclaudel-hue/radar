'''Tests hors ligne du cycle OAuth Discogs du groupe 1.'''

import json
import os
import stat
import tempfile
import unittest
import urllib.parse
from unittest import mock

from radar_web.radar import discogsauth


class ReponseFactice:
    def __init__(self, status_code=200, text='', json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        return self._json_data


class TestsOAuthDiscogsHorsLigne(unittest.TestCase):
    CLE_CONSUMER = 'cle-consommateur'
    SECRET_CONSUMER = 'secret-consommateur'
    DOMAINE = 'exemple.test'
    JETON_PENDING = 'jeton-pending'
    SECRET_PENDING = 'secret-pending'

    def setUp(self):
        self.repertoire = tempfile.TemporaryDirectory()
        self.addCleanup(self.repertoire.cleanup)
        self.racine = self.repertoire.name

        self.patch_paths = mock.patch.object(
            discogsauth.paths,
            'user_dir',
            side_effect=self._user_dir,
        )
        self.patch_paths.start()
        self.addCleanup(self.patch_paths.stop)

        self.patch_environ = mock.patch.dict(
            os.environ,
            {
                'RADAR_DISCOGS_CONSUMER_KEY': self.CLE_CONSUMER,
                'RADAR_DISCOGS_CONSUMER_SECRET': self.SECRET_CONSUMER,
                'RADAR_DISCOGS_REDIRECT_URI': '',
                'RADAR_DOMAIN': self.DOMAINE,
            },
            clear=False,
        )
        self.patch_environ.start()
        self.addCleanup(self.patch_environ.stop)

        self.http = mock.Mock(name='requetes_http')
        self.patch_http = mock.patch.object(
            discogsauth.requests,
            'request',
            self.http,
        )
        self.patch_http.start()
        self.addCleanup(self.patch_http.stop)

        self.patch_heure = mock.patch.object(
            discogsauth.time,
            'time',
            return_value=1_700_000_000,
        )
        self.patch_heure.start()
        self.addCleanup(self.patch_heure.stop)

        self.patch_nonce = mock.patch.object(
            discogsauth.secrets,
            'token_urlsafe',
            return_value='nonce-factice',
        )
        self.patch_nonce.start()
        self.addCleanup(self.patch_nonce.stop)

    def _user_dir(self, uid):
        return os.path.join(self.racine, uid)

    def _fichier_utilisateur(self, uid, nom):
        return os.path.join(self.racine, uid, nom)

    def _ecrire_json(self, chemin, donnees):
        os.makedirs(os.path.dirname(chemin), mode=0o700, exist_ok=True)
        with open(chemin, 'w', encoding='utf-8') as fichier:
            json.dump(donnees, fichier, separators=(',', ':'))
        os.chmod(chemin, 0o600)

    def _reponse_formulaire(self, valeurs):
        return ReponseFactice(
            status_code=200,
            text=urllib.parse.urlencode(valeurs),
        )

    def _installer_pending(self, uid='u1'):
        donnees = {
            'oauth_token': self.JETON_PENDING,
            'oauth_token_secret': self.SECRET_PENDING,
        }
        chemin = self._fichier_utilisateur(
            uid,
            'discogs_oauth_pending.json',
        )
        self._ecrire_json(chemin, donnees)
        return chemin, donnees

    def test_demarrage_cree_une_autorisation_et_un_fichier_pending(self):
        self.http.return_value = self._reponse_formulaire(
            {
                'oauth_token': self.JETON_PENDING,
                'oauth_token_secret': self.SECRET_PENDING,
            }
        )

        url = discogsauth.start('u1')

        self.assertEqual(self.http.call_count, 1)
        appel = self.http.call_args
        self.assertEqual(
            appel.args,
            ('POST', discogsauth.REQUEST_TOKEN_ENDPOINT),
        )
        self.assertEqual(
            appel.kwargs['timeout'],
            discogsauth.REQUEST_TIMEOUT,
        )
        self.assertFalse(appel.kwargs['allow_redirects'])

        autorisation = appel.kwargs['headers']['Authorization']
        guillemet = chr(34)
        self.assertIn(
            f'oauth_signature_method={guillemet}PLAINTEXT{guillemet}',
            autorisation,
        )
        self.assertIn(
            f'oauth_signature={guillemet}{self.SECRET_CONSUMER}%26{guillemet}',
            autorisation,
        )

        prefixe = 'https://www.discogs.com/oauth/authorize?oauth_token='
        self.assertTrue(url.startswith(prefixe))
        self.assertEqual(
            url,
            f'{discogsauth.AUTHORIZE_ENDPOINT}?oauth_token={self.JETON_PENDING}',
        )

        pending_path = self._fichier_utilisateur(
            'u1',
            'discogs_oauth_pending.json',
        )
        self.assertEqual(discogsauth._pending_path('u1'), pending_path)
        self.assertTrue(os.path.isfile(pending_path))
        self.assertEqual(
            stat.S_IMODE(os.stat(pending_path).st_mode),
            0o600,
        )
        with open(pending_path, 'r', encoding='utf-8') as fichier:
            self.assertEqual(
                json.load(fichier),
                {
                    'oauth_token': self.JETON_PENDING,
                    'oauth_token_secret': self.SECRET_PENDING,
                },
            )

    def test_finalisation_avec_un_jeton_different_echoue_sans_reseau(self):
        pending_path, _ = self._installer_pending('u1')

        resultat = discogsauth.complete(
            'u1',
            'jeton-different',
            'v',
        )

        self.assertFalse(resultat)
        self.http.assert_not_called()
        self.assertTrue(os.path.exists(pending_path))

    def test_finalisation_reussie_ecrit_les_jetons_prives(self):
        pending_path, _ = self._installer_pending('u1')
        self.http.side_effect = [
            self._reponse_formulaire(
                {
                    'oauth_token': 'jeton-acces',
                    'oauth_token_secret': 'secret-acces',
                }
            ),
            ReponseFactice(
                status_code=200,
                json_data={'username': 'alice'},
            ),
        ]

        resultat = discogsauth.complete(
            'u1',
            self.JETON_PENDING,
            'v',
        )

        self.assertTrue(resultat)
        self.assertEqual(
            [
                (appel.args[0], appel.args[1])
                for appel in self.http.call_args_list
            ],
            [
                ('POST', discogsauth.ACCESS_TOKEN_ENDPOINT),
                ('GET', discogsauth.IDENTITY_ENDPOINT),
            ],
        )
        for appel in self.http.call_args_list:
            self.assertEqual(
                appel.kwargs['timeout'],
                discogsauth.REQUEST_TIMEOUT,
            )
            self.assertFalse(appel.kwargs['allow_redirects'])

        token_path = self._fichier_utilisateur(
            'u1',
            'discogs_oauth.json',
        )
        self.assertEqual(discogsauth._token_path('u1'), token_path)
        self.assertTrue(os.path.isfile(token_path))
        self.assertEqual(
            stat.S_IMODE(os.stat(token_path).st_mode),
            0o600,
        )
        with open(token_path, 'r', encoding='utf-8') as fichier:
            self.assertEqual(
                json.load(fichier),
                {
                    'access_token': 'jeton-acces',
                    'access_secret': 'secret-acces',
                    'username': 'alice',
                },
            )

        self.assertFalse(os.path.exists(pending_path))
        self.assertTrue(discogsauth.is_connected('u1'))
        self.assertEqual(
            discogsauth.credential('u1'),
            'oauth:jeton-acces:secret-acces',
        )

    def test_deconnexion_supprime_les_jetons_et_le_pending(self):
        token_path = self._fichier_utilisateur(
            'u1',
            'discogs_oauth.json',
        )
        pending_path = self._fichier_utilisateur(
            'u1',
            'discogs_oauth_pending.json',
        )
        self._ecrire_json(
            token_path,
            {
                'access_token': 'jeton-acces',
                'access_secret': 'secret-acces',
                'username': 'alice',
            },
        )
        self._ecrire_json(
            pending_path,
            {
                'oauth_token': self.JETON_PENDING,
                'oauth_token_secret': self.SECRET_PENDING,
            },
        )
        self.assertTrue(discogsauth.is_connected('u1'))

        discogsauth.disconnect('u1')

        self.assertFalse(os.path.exists(token_path))
        self.assertFalse(os.path.exists(pending_path))
        self.assertFalse(discogsauth.is_connected('u1'))
        self.assertIsNone(discogsauth.credential('u1'))
        self.http.assert_not_called()

    def test_configuration_est_faus_sans_variables(self):
        variables = (
            'RADAR_DISCOGS_CONSUMER_KEY',
            'RADAR_DISCOGS_CONSUMER_SECRET',
            'RADAR_DISCOGS_REDIRECT_URI',
            'RADAR_DOMAIN',
        )
        with mock.patch.dict(os.environ, {}):
            for variable in variables:
                os.environ.pop(variable, None)
            self.assertFalse(discogsauth.configured())

        self.http.assert_not_called()


if __name__ == '__main__':
    unittest.main()
