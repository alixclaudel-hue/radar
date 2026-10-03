import unittest
from unittest.mock import Mock, patch

from radar_web.radar import discogs, discogsauth


class TestDiscogsWebAuth(unittest.TestCase):
    def setUp(self):
        self.patcher_current_uid = patch('radar_web.radar.store.current_uid')
        self.current_uid = self.patcher_current_uid.start()
        self.addCleanup(self.patcher_current_uid.stop)

        self.patcher_credential = patch(
            'radar_web.radar.discogsauth.credential'
        )
        self.credential = self.patcher_credential.start()
        self.addCleanup(self.patcher_credential.stop)

        self.patcher_authorization = patch(
            'radar_web.radar.discogsauth.authorization_header'
        )
        self.authorization_header = self.patcher_authorization.start()
        self.addCleanup(self.patcher_authorization.stop)

        self.patcher_requests = patch('radar_web.radar.discogs.requests')
        self.requests = self.patcher_requests.start()
        self.addCleanup(self.patcher_requests.stop)

        self.patcher_sleep = patch('radar_web.radar.discogs.time.sleep')
        self.mock_sleep = self.patcher_sleep.start()
        self.addCleanup(self.patcher_sleep.stop)

        self._configurer_auth()

    def tearDown(self):
        self.mock_sleep.assert_not_called()

    def _configurer_auth(self, uid=None, credential=None, authorization=None):
        self.current_uid.return_value = uid
        self.credential.return_value = credential
        self.authorization_header.return_value = authorization
        self.authorization_header.side_effect = None

    @staticmethod
    def _reponse(json_value=None, status_code=200, ok=True, text=''):
        reponse = Mock()
        reponse.status_code = status_code
        reponse.ok = ok
        reponse.text = text
        reponse.json.return_value = json_value
        return reponse

    def _mocker_requetes(self, methode, reponse):
        methode_mock = getattr(self.requests, methode)
        methode_mock.return_value = reponse
        return self.requests

    def test_auth_a_token_personnel_ajoute_token_sans_oauth(self):
        self._configurer_auth(
            uid='u1',
            credential='oauth:AT:AS',
            authorization='OAuth x',
        )
        params, headers = discogs._auth({'a': 1}, 'perso')

        self.assertEqual(params, {'a': 1, 'token': 'perso'})
        self.assertEqual(headers['User-Agent'], discogs.UA)
        self.assertNotIn('Authorization', headers)
        self.current_uid.assert_not_called()
        self.credential.assert_not_called()
        self.authorization_header.assert_not_called()

    def test_auth_b_token_vide_et_uid_absent_ne_fait_pas_de_repli(self):
        self._configurer_auth(
            uid=None,
            credential='oauth:AT:AS',
            authorization='OAuth x',
        )
        params, headers = discogs._auth({}, '')

        self.assertEqual(params, {})
        self.assertEqual(headers['User-Agent'], discogs.UA)
        self.assertNotIn('Authorization', headers)
        self.current_uid.assert_called_once_with()
        self.credential.assert_not_called()
        self.authorization_header.assert_not_called()

    def test_auth_c_oauth_du_compte_courant_ajoute_entete(self):
        self._configurer_auth(
            uid='u1',
            credential='oauth:AT:AS',
            authorization='OAuth x',
        )
        params, headers = discogs._auth({}, '')

        self.assertEqual(params, {})
        self.assertEqual(headers['User-Agent'], discogs.UA)
        self.assertEqual(headers['Authorization'], 'OAuth x')
        self.current_uid.assert_called_once_with()
        self.credential.assert_called_once_with('u1')
        self.authorization_header.assert_called_once_with(('AT', 'AS'))

    def test_auth_d_erreur_discogs_auth_laisse_appel_anonyme(self):
        self._configurer_auth(
            uid='u1',
            credential='oauth:AT:AS',
        )
        self.authorization_header.side_effect = discogsauth.DiscogsError(
            'configuration absente'
        )

        params, headers = discogs._auth({}, '')

        self.assertEqual(params, {})
        self.assertEqual(headers['User-Agent'], discogs.UA)
        self.assertNotIn('Authorization', headers)
        self.current_uid.assert_called_once_with()
        self.credential.assert_called_once_with('u1')
        self.authorization_header.assert_called_once_with(('AT', 'AS'))

    def test_auth_compte_sans_credential_reste_anonyme(self):
        self._configurer_auth(uid='u1', authorization='OAuth x')
        params, headers = discogs._auth({}, '')

        self.assertEqual(params, {})
        self.assertEqual(headers['User-Agent'], discogs.UA)
        self.assertNotIn('Authorization', headers)
        self.current_uid.assert_called_once_with()
        self.credential.assert_called_once_with('u1')
        self.authorization_header.assert_not_called()

    def test_auth_accepte_none_et_liste_vide(self):
        self._configurer_auth(
            credential='oauth:AT:AS',
            authorization='OAuth x',
        )
        params_none, headers_none = discogs._auth(None, None)
        params_liste, headers_liste = discogs._auth([], '')

        self.assertEqual(params_none, {})
        self.assertEqual(params_liste, {})
        self.assertEqual(headers_none['User-Agent'], discogs.UA)
        self.assertEqual(headers_liste['User-Agent'], discogs.UA)
        self.assertNotIn('Authorization', headers_none)
        self.assertNotIn('Authorization', headers_liste)
        self.assertEqual(self.current_uid.call_count, 2)
        self.credential.assert_not_called()
        self.authorization_header.assert_not_called()

    def test_auth_zero_est_considere_absent(self):
        self._configurer_auth(
            uid=0,
            credential='oauth:AT:AS',
            authorization='OAuth x',
        )
        params, headers = discogs._auth({}, 0)

        self.assertEqual(params, {})
        self.assertEqual(headers['User-Agent'], discogs.UA)
        self.assertNotIn('Authorization', headers)
        self.current_uid.assert_called_once_with()
        self.credential.assert_not_called()
        self.authorization_header.assert_not_called()

    def test_get_avec_oauth_renvoie_json_et_envoie_entete(self):
        reponse = self._reponse(json_value={'k': 1})
        self._configurer_auth(
            uid='u1',
            credential='oauth:AT:AS',
            authorization='OAuth x',
        )
        self._mocker_requetes('get', reponse)

        resultat = discogs.get('/x', token='')

        self.assertEqual(resultat, {'k': 1})
        self.requests.get.assert_called_once()
        args, options = self.requests.get.call_args
        self.assertEqual(args, (discogs.BASE + '/x',))
        self.assertEqual(options['params'], {})
        self.assertNotIn('token', options['params'])
        self.assertEqual(options['headers']['User-Agent'], discogs.UA)
        self.assertEqual(options['headers']['Authorization'], 'OAuth x')
        self.assertEqual(options['timeout'], 20)
        self.current_uid.assert_called_once_with()
        self.credential.assert_called_once_with('u1')
        self.authorization_header.assert_called_once_with(('AT', 'AS'))
        reponse.json.assert_called_once_with()

    def test_put_avec_token_explicite_renvoie_json(self):
        reponse = self._reponse(json_value={'saved': True}, text='ok')
        self._mocker_requetes('put', reponse)

        resultat = discogs.put('/x', {'a': 1}, token='perso')

        self.assertEqual(resultat, {'saved': True})
        self.requests.put.assert_called_once()
        args, options = self.requests.put.call_args
        self.assertEqual(args, (discogs.BASE + '/x',))
        self.assertEqual(options['params'], {'a': 1, 'token': 'perso'})
        self.assertEqual(options['headers']['User-Agent'], discogs.UA)
        self.assertNotIn('Authorization', options['headers'])
        self.assertEqual(options['timeout'], 20)
        reponse.json.assert_called_once_with()

    def test_put_reponse_sans_texte_retourne_dict_vide_et_conserve_zero(self):
        reponse = self._reponse(text='')
        self._mocker_requetes('put', reponse)

        resultat = discogs.put(
            '/x',
            params={'page': 0, 'per_page': 0},
            token='perso',
        )

        self.assertEqual(resultat, {})
        self.requests.put.assert_called_once()
        args, options = self.requests.put.call_args
        self.assertEqual(args, (discogs.BASE + '/x',))
        self.assertEqual(
            options['params'],
            {'page': 0, 'per_page': 0, 'token': 'perso'},
        )
        self.assertEqual(options['timeout'], 20)
        reponse.json.assert_not_called()

    def test_delete_avec_token_explicite_ne_retourne_rien(self):
        reponse = self._reponse(text='')
        self._mocker_requetes('delete', reponse)

        resultat = discogs.delete('/x', params={'a': 1}, token='perso')

        self.assertIsNone(resultat)
        self.requests.delete.assert_called_once()
        args, options = self.requests.delete.call_args
        self.assertEqual(args, (discogs.BASE + '/x',))
        self.assertEqual(options['params'], {'a': 1, 'token': 'perso'})
        self.assertEqual(options['headers']['User-Agent'], discogs.UA)
        self.assertNotIn('Authorization', options['headers'])
        self.assertEqual(options['timeout'], 20)
        reponse.json.assert_not_called()

    def test_get_401_leve_erreur_discogs_sans_json(self):
        reponse = self._reponse(
            status_code=401,
            ok=False,
            text='unauthorized',
        )
        self._mocker_requetes('get', reponse)

        with self.assertRaises(discogs.DiscogsError) as erreur:
            discogs.get('/x', token='perso')

        self.assertIn('401', str(erreur.exception))
        self.requests.get.assert_called_once()
        reponse.json.assert_not_called()

    def test_put_429_leve_erreur_discogs_sans_json(self):
        reponse = self._reponse(
            status_code=429,
            ok=False,
            text='rate limit',
        )
        self._mocker_requetes('put', reponse)

        with self.assertRaises(discogs.DiscogsError) as erreur:
            discogs.put('/x', token='perso')

        self.assertIn('429', str(erreur.exception))
        self.requests.put.assert_called_once()
        reponse.json.assert_not_called()

    def test_delete_500_leve_erreur_discogs(self):
        reponse = self._reponse(
            status_code=500,
            ok=False,
            text='failure',
        )
        self._mocker_requetes('delete', reponse)

        with self.assertRaises(discogs.DiscogsError) as erreur:
            discogs.delete('/x', token='perso')

        self.assertIn('500', str(erreur.exception))
        self.requests.delete.assert_called_once()
        reponse.json.assert_not_called()


if __name__ == '__main__':
    unittest.main()
