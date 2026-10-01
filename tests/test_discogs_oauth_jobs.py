import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import radar_jobs.common as common
import radar_jobs.sources as sources


class TestDiscogsOAuthJobs(unittest.TestCase):

    def test_discogs_get_oauth_avec_cles_consommateur(self):
        """(a) discogs_get oauth avec clés consommateur en env"""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {'a': 1}

        with patch.dict(os.environ, {'RADAR_DISCOGS_CONSUMER_KEY': 'ck', 'RADAR_DISCOGS_CONSUMER_SECRET': 'cs'}):
            with patch('radar_jobs.sources.requests.get', return_value=mock_resp) as mock_get:
                with patch('radar_jobs.sources.time.sleep'):
                    result = sources.discogs_get('oauth:TOK:SEC', '/x')

        self.assertEqual(result, {'a': 1})
        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        self.assertIn('Authorization', kwargs['headers'])
        auth = kwargs['headers']['Authorization']
        self.assertTrue(auth.startswith('OAuth '))
        self.assertIn('oauth_token="TOK"', auth)
        self.assertIn('oauth_signature="cs&SEC"', auth)
        self.assertNotIn('token', kwargs['params'])

    def test_discogs_get_oauth_sans_cles_consommateur(self):
        """(b) discogs_get oauth sans clés consommateur -> {} sans appel réseau"""
        env = {k: v for k, v in os.environ.items() if k not in ('RADAR_DISCOGS_CONSUMER_KEY', 'RADAR_DISCOGS_CONSUMER_SECRET')}
        with patch.dict(os.environ, env, clear=True):
            with patch('radar_jobs.sources.requests.get') as mock_get:
                result = sources.discogs_get('oauth:TOK:SEC', '/x')

        self.assertEqual(result, {})
        mock_get.assert_not_called()

    def test_discogs_get_perso_token_dans_params(self):
        """(c) discogs_get avec token personnel -> params contient token, pas d'en-tête Authorization"""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {'a': 1}

        with patch('radar_jobs.sources.requests.get', return_value=mock_resp) as mock_get:
            with patch('radar_jobs.sources.time.sleep'):
                result = sources.discogs_get('perso', '/x')

        self.assertEqual(result, {'a': 1})
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs['params']['token'], 'perso')
        self.assertNotIn('Authorization', kwargs['headers'])

    def test_discogs_get_token_none(self):
        """Cas limite: token None"""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {}

        with patch('radar_jobs.sources.requests.get', return_value=mock_resp) as mock_get:
            with patch('radar_jobs.sources.time.sleep'):
                result = sources.discogs_get(None, '/x')

        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs['params']['token'], None)

    def test_discogs_get_token_chaine_vide(self):
        """Cas limite: token chaîne vide"""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {}

        with patch('radar_jobs.sources.requests.get', return_value=mock_resp) as mock_get:
            with patch('radar_jobs.sources.time.sleep'):
                result = sources.discogs_get('', '/x')

        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs['params']['token'], '')

    def test_discogs_get_params_liste_vide(self):
        """Cas limite: params liste vide"""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {'a': 1}

        with patch('radar_jobs.sources.requests.get', return_value=mock_resp) as mock_get:
            with patch('radar_jobs.sources.time.sleep'):
                result = sources.discogs_get('perso', '/x', params=[])

        self.assertEqual(result, {'a': 1})
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs['params']['token'], 'perso')

    def test_discogs_get_params_zero(self):
        """Cas limite: params zéro"""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {'a': 1}

        with patch('radar_jobs.sources.requests.get', return_value=mock_resp) as mock_get:
            with patch('radar_jobs.sources.time.sleep'):
                result = sources.discogs_get('perso', '/x', params=0)

        self.assertEqual(result, {'a': 1})
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs['params']['token'], 'perso')

    def test_discogs_get_erreur_reseau(self):
        """Cas d'erreur: discogs_get avec erreur réseau"""
        with patch('radar_jobs.sources.requests.get', side_effect=sources.requests.RequestException('network error')) as mock_get:
            with patch('radar_jobs.sources.time.sleep'):
                result = sources.discogs_get('perso', '/x')

        self.assertEqual(result, {})

    def test_discogs_get_erreur_429(self):
        """Cas d'erreur: discogs_get avec 429 rate limit"""
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 429

        with patch('radar_jobs.sources.requests.get', return_value=mock_resp) as mock_get:
            with patch('radar_jobs.sources.time.sleep'):
                result = sources.discogs_get('perso', '/x')

        self.assertEqual(result, {})
        self.assertEqual(mock_get.call_count, 5)

    def test_discogs_get_erreur_http(self):
        """Cas d'erreur: discogs_get avec erreur HTTP"""
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 404
        mock_resp.json.return_value = {}

        with patch('radar_jobs.sources.requests.get', return_value=mock_resp) as mock_get:
            with patch('radar_jobs.sources.time.sleep'):
                result = sources.discogs_get('perso', '/x')

        self.assertEqual(result, {})

    def test_save_json_sans_token_precedent(self):
        """(d) save_json: fichier écrit sans token si pas de token avant"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({}, f)
            config_path = f.name

        try:
            with patch.object(common, 'CONFIG_PATH', config_path):
                common.save_json(common.CONFIG_PATH, {'token': 'oauth:a:b', 'x': 1})
                with open(config_path) as f:
                    data = json.load(f)

            self.assertNotIn('token', data)
            self.assertEqual(data.get('x'), 1)
        finally:
            os.unlink(config_path)

    def test_save_json_conserve_token_personnel(self):
        """(d) save_json: conserve l'ancien token personnel"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({'token': 'perso'}, f)
            config_path = f.name

        try:
            with patch.object(common, 'CONFIG_PATH', config_path):
                common.save_json(common.CONFIG_PATH, {'token': 'oauth:a:b', 'x': 1})
                with open(config_path) as f:
                    data = json.load(f)

            self.assertEqual(data.get('token'), 'perso')
            self.assertEqual(data.get('x'), 1)
        finally:
            os.unlink(config_path)

    def test_cfg_load_avec_oauth_file(self):
        """(e) cfg_load avec discogs_oauth.json renvoie oauth token"""
        with tempfile.TemporaryDirectory() as tmpdir:
            oauth_file = os.path.join(tmpdir, 'discogs_oauth.json')
            with open(oauth_file, 'w') as f:
                json.dump({'access_token': 'a', 'access_secret': 'b'}, f)

            config_path = os.path.join(tmpdir, 'config.json')
            with open(config_path, 'w') as f:
                json.dump({}, f)

            env = {k: v for k, v in os.environ.items() if k != 'DISCOGS_TOKEN'}
            env['RADAR_UID'] = 'owner'
            with patch.dict(os.environ, env, clear=True):
                with patch.object(common, 'USER_DIR', tmpdir):
                    with patch.object(common, 'CONFIG_PATH', config_path):
                        result = common.cfg_load()

        self.assertEqual(result.get('token'), 'oauth:a:b')


if __name__ == '__main__':
    unittest.main()
