import sys
import os
import unittest
from unittest import mock
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radar_web import worker as workermod


class TestRecosMultiUid(unittest.TestCase):
    """Tests de la logique multi-utilisateurs pour le scan RECOS (worker)."""

    @mock.patch.dict(os.environ, {'RADAR_RECOS_SCAN': '1'})
    @mock.patch.object(workermod.paths, 'all_uids')
    @mock.patch.object(workermod.jobs, 'load_queue')
    @mock.patch.object(workermod.jobs, 'launch')
    @mock.patch.object(workermod.store, 'load', return_value=[])
    @mock.patch.object(workermod.paths, 'user_paths', side_effect=lambda uid: types.SimpleNamespace(recos_candidates='c-' + uid, recos_candidates_decouverte='d-' + uid))
    def test_cas_a_all_uids_abc_owner_fichier_vide(self, mock_user_paths, mock_store_load, mock_launch, mock_load_queue, mock_all_uids):
        """Cas (a) : all_uids=['abc','owner'], fichier vide -> launch pour owner puis abc (scan_recos et scan_recos_decouverte)."""
        mock_all_uids.return_value = ['abc', 'owner']
        mock_load_queue.return_value = []
        workermod._last_recos_check = 0.0

        workermod._maybe_recos_scan()

        expected_calls = [
            mock.call('scan_recos', {}, uid='owner', priority=0),
            mock.call('scan_recos_decouverte', {}, uid='owner', priority=0),
            mock.call('scan_recos', {}, uid='abc', priority=0),
            mock.call('scan_recos_decouverte', {}, uid='abc', priority=0)
        ]
        self.assertEqual(mock_launch.call_args_list, expected_calls)

    @mock.patch.dict(os.environ, {'RADAR_RECOS_SCAN': '1'})
    @mock.patch.object(workermod.paths, 'all_uids')
    @mock.patch.object(workermod.jobs, 'load_queue')
    @mock.patch.object(workermod.jobs, 'launch')
    @mock.patch.object(workermod.store, 'load', return_value=[])
    @mock.patch.object(workermod.paths, 'user_paths', side_effect=lambda uid: types.SimpleNamespace(recos_candidates='c-' + uid, recos_candidates_decouverte='d-' + uid))
    def test_cas_b_load_queue_avec_scan_recos_abc(self, mock_user_paths, mock_store_load, mock_launch, mock_load_queue, mock_all_uids):
        """Cas (b) : load_queue contient scan_recos pour abc -> owner et le mode Découverte d'abc restent lancés."""
        mock_all_uids.return_value = ['abc', 'owner']
        mock_load_queue.return_value = [{'uid': 'abc', 'name': 'scan_recos'}]
        workermod._last_recos_check = 0.0

        workermod._maybe_recos_scan()

        expected_calls = [
            mock.call('scan_recos', {}, uid='owner', priority=0),
            mock.call('scan_recos_decouverte', {}, uid='owner', priority=0),
            mock.call('scan_recos_decouverte', {}, uid='abc', priority=0)
        ]
        self.assertEqual(mock_launch.call_args_list, expected_calls)

    @mock.patch.object(workermod.paths, 'all_uids')
    def test_cas_c_recos_uids_ordre_owner_premier(self, mock_all_uids):
        """Cas (c) : _recos_uids avec all_uids=['a','owner','b'] retourne ['owner','a','b']."""
        mock_all_uids.return_value = ['a', 'owner', 'b']

        result = workermod._recos_uids()

        self.assertEqual(result, ['owner', 'a', 'b'])


if __name__ == '__main__':
    unittest.main()
