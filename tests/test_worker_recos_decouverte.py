import sys
import os
import unittest
from unittest import mock
import types
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radar_web import worker as workermod


class TestWorkerRecosDecouverte(unittest.TestCase):
    """Tests de la logique des recos de découverte et de la purge de minuit."""

    @mock.patch.dict(os.environ, {'RADAR_RECOS_SCAN': '1'})
    @mock.patch.object(workermod.paths, 'all_uids')
    @mock.patch.object(workermod.jobs, 'load_queue')
    @mock.patch.object(workermod.jobs, 'launch')
    @mock.patch.object(workermod.store, 'load', return_value=[])
    @mock.patch.object(workermod.paths, 'user_paths', side_effect=lambda uid: types.SimpleNamespace(recos_candidates='c-' + uid, recos_candidates_decouverte='d-' + uid))
    def test_scan_recos_1_all_uids_owner_queue_vide(self, mock_user_paths, mock_store_load, mock_launch, mock_load_queue, mock_all_uids):
        """1. all_uids=['owner'], load_queue=[], store.load -> [] : [scan_recos owner, scan_recos_decouverte owner]."""
        mock_all_uids.return_value = ['owner']
        mock_load_queue.return_value = []
        workermod._last_recos_check = 0.0

        workermod._maybe_recos_scan()

        expected_calls = [
            mock.call('scan_recos', {}, uid='owner', priority=0),
            mock.call('scan_recos_decouverte', {}, uid='owner', priority=0)
        ]
        self.assertEqual(mock_launch.call_args_list, expected_calls)

    @mock.patch.dict(os.environ, {'RADAR_RECOS_SCAN': '1'})
    @mock.patch.object(workermod.paths, 'all_uids')
    @mock.patch.object(workermod.jobs, 'load_queue')
    @mock.patch.object(workermod.jobs, 'launch')
    @mock.patch.object(workermod.store, 'load', side_effect=lambda path, default: [{'x': 1}] if path == 'd-owner' else [])
    @mock.patch.object(workermod.paths, 'user_paths', side_effect=lambda uid: types.SimpleNamespace(recos_candidates='c-' + uid, recos_candidates_decouverte='d-' + uid))
    def test_scan_recos_2_all_uids_owner_decouverte_pleine(self, mock_user_paths, mock_store_load, mock_launch, mock_load_queue, mock_all_uids):
        """2. all_uids=['owner'], load_queue=[], store.load side_effect -> [scan_recos owner, publish_recos_decouverte owner]."""
        mock_all_uids.return_value = ['owner']
        mock_load_queue.return_value = []
        workermod._last_recos_check = 0.0

        workermod._maybe_recos_scan()

        expected_calls = [
            mock.call('scan_recos', {}, uid='owner', priority=0),
            mock.call('publish_recos_decouverte', {}, uid='owner', priority=0)
        ]
        self.assertEqual(mock_launch.call_args_list, expected_calls)

    @mock.patch.dict(os.environ, {'RADAR_RECOS_SCAN': '1'})
    @mock.patch.object(workermod.paths, 'all_uids')
    @mock.patch.object(workermod.jobs, 'load_queue')
    @mock.patch.object(workermod.jobs, 'launch')
    @mock.patch.object(workermod.store, 'load', return_value=[])
    @mock.patch.object(workermod.paths, 'user_paths', side_effect=lambda uid: types.SimpleNamespace(recos_candidates='c-' + uid, recos_candidates_decouverte='d-' + uid))
    def test_scan_recos_3_queue_publish_decouverte(self, mock_user_paths, mock_store_load, mock_launch, mock_load_queue, mock_all_uids):
        """3. all_uids=['owner'], load_queue=[{'uid': 'owner', 'name': 'scan_recos_decouverte'}] -> [scan_recos owner]."""
        mock_all_uids.return_value = ['owner']
        mock_load_queue.return_value = [{'uid': 'owner', 'name': 'scan_recos_decouverte'}]
        workermod._last_recos_check = 0.0

        workermod._maybe_recos_scan()

        expected_calls = [
            mock.call('scan_recos', {}, uid='owner', priority=0)
        ]
        self.assertEqual(mock_launch.call_args_list, expected_calls)

    @mock.patch.dict(os.environ, {'RADAR_RECOS_SCAN': '1'})
    @mock.patch.object(workermod.paths, 'all_uids')
    @mock.patch.object(workermod.jobs, 'load_queue')
    @mock.patch.object(workermod.jobs, 'launch')
    @mock.patch.object(workermod.store, 'load', return_value=[])
    @mock.patch.object(workermod.paths, 'user_paths', side_effect=lambda uid: types.SimpleNamespace(recos_candidates='c-' + uid, recos_candidates_decouverte='d-' + uid))
    def test_scan_recos_4_queue_publish_recos(self, mock_user_paths, mock_store_load, mock_launch, mock_load_queue, mock_all_uids):
        """4. all_uids=['owner'], load_queue=[{'uid': 'owner', 'name': 'publish_recos'}] -> [scan_recos_decouverte owner]."""
        mock_all_uids.return_value = ['owner']
        mock_load_queue.return_value = [{'uid': 'owner', 'name': 'publish_recos'}]
        workermod._last_recos_check = 0.0

        workermod._maybe_recos_scan()

        expected_calls = [
            mock.call('scan_recos_decouverte', {}, uid='owner', priority=0)
        ]
        self.assertEqual(mock_launch.call_args_list, expected_calls)

    @mock.patch.dict(os.environ, {'RADAR_RECOS_SCAN': '1'})
    @mock.patch.object(workermod.paths, 'all_uids')
    @mock.patch.object(workermod.jobs, 'load_queue')
    @mock.patch.object(workermod.jobs, 'launch')
    @mock.patch.object(workermod.store, 'load', return_value=[])
    @mock.patch.object(workermod.paths, 'user_paths', side_effect=lambda uid: types.SimpleNamespace(recos_candidates='c-' + uid, recos_candidates_decouverte='d-' + uid))
    def test_scan_recos_5_multi_uids(self, mock_user_paths, mock_store_load, mock_launch, mock_load_queue, mock_all_uids):
        """5. all_uids=['abc', 'owner'], load_queue=[] -> [scan_recos owner, scan_recos_decouverte owner, scan_recos abc, scan_recos_decouverte abc]."""
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

    @mock.patch.object(workermod.paths, 'all_uids', return_value=['owner'])
    @mock.patch.object(workermod.store, 'save')
    @mock.patch.object(workermod.store, 'load', side_effect=lambda path, default: [{'played': True}, {'title': 't'}] if path == 'pd-owner' else [])
    @mock.patch.object(workermod.paths, 'user_paths', side_effect=lambda uid: types.SimpleNamespace(recos_playlist='p-' + uid, recos_playlist_decouverte='pd-' + uid))
    def test_midnight_purge(self, mock_user_paths, mock_store_load, mock_store_save, mock_all_uids):
        """6. Test _maybe_recos_midnight_purge sauvegarde correctement les pistes non jouées."""
        workermod._last_midnight_purge_check = 0.0
        workermod._last_midnight_purge_date = datetime.date(2000, 1, 1)

        workermod._maybe_recos_midnight_purge()

        mock_store_save.assert_called_once_with('pd-owner', [{'title': 't'}])


if __name__ == '__main__':
    unittest.main()
