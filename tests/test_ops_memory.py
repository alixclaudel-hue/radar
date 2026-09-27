import unittest
import tempfile
import os

import radar_ops.memory as memory


class TestMemoryOps(unittest.TestCase):
    """Tests pour radar_ops.memory."""

    def _write_meminfo(self, dirpath: str, content: str) -> str:
        """Écrit un fichier meminfo dans dirpath et retourne son chemin."""
        path = os.path.join(dirpath, 'meminfo')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return path

    def test_read_meminfo_fichier_valide_convertit_kb_en_octets(self):
        """read_meminfo sur un fixture valide : valeurs en octets, clés attendues présentes."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = """MemTotal:       1000000 kB
MemFree:         200000 kB
MemAvailable:    300000 kB
Buffers:          50000 kB
Cached:           80000 kB
SwapTotal:       500000 kB
SwapFree:        400000 kB
"""
            path = self._write_meminfo(tmp, fixture)
            data = memory.read_meminfo(path)

            self.assertEqual(data['MemTotal'], 1000000 * 1024)
            self.assertEqual(data['MemFree'], 200000 * 1024)
            self.assertEqual(data['MemAvailable'], 300000 * 1024)
            self.assertEqual(data['Buffers'], 50000 * 1024)
            self.assertEqual(data['Cached'], 80000 * 1024)
            self.assertEqual(data['SwapTotal'], 500000 * 1024)
            self.assertEqual(data['SwapFree'], 400000 * 1024)

    def test_read_meminfo_chemin_inexistant_renvoie_dict_vide(self):
        """read_meminfo sur un chemin inexistant renvoie un dict vide, sans lever."""
        data = memory.read_meminfo('/chemin/qui/n/existe/pas/meminfo')
        self.assertEqual(data, {})

    def test_read_meminfo_ignore_lignes_malformees(self):
        """read_meminfo ignore lignes sans ':', valeur non numérique, ligne vide, garde valides."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = """MemTotal:       1000 kB
Ligne sans deux-points
MemFree:       abc kB

MemAvailable:   500 kB
SwapTotal: 0 kB
"""
            path = self._write_meminfo(tmp, fixture)
            data = memory.read_meminfo(path)

            self.assertIn('MemTotal', data)
            self.assertEqual(data['MemTotal'], 1000 * 1024)
            self.assertIn('MemAvailable', data)
            self.assertEqual(data['MemAvailable'], 500 * 1024)
            self.assertIn('SwapTotal', data)
            self.assertEqual(data['SwapTotal'], 0)
            self.assertNotIn('MemFree', data)  # valeur non numérique ignorée

    def test_snapshot_cas_nominal_avec_memavailable(self):
        """Snapshot nominal avec MemTotal et MemAvailable : calculs corrects."""
        with tempfile.TemporaryDirectory() as tmp:
            # 1 000 000 kB total, 300 000 kB available
            fixture = """MemTotal:       1000000 kB
MemAvailable:    300000 kB
MemFree:         200000 kB
Buffers:          50000 kB
Cached:           80000 kB
"""
            path = self._write_meminfo(tmp, fixture)
            snap = memory.snapshot(path)

            self.assertTrue(snap['available'])
            self.assertIsNone(snap['reason'])
            self.assertEqual(snap['total_b'], 1000000 * 1024)
            self.assertEqual(snap['available_b'], 300000 * 1024)
            expected_used = (1000000 - 300000) * 1024
            self.assertEqual(snap['used_b'], expected_used)
            expected_pct = round(expected_used / snap['total_b'] * 100, 1)
            self.assertEqual(snap['used_pct'], expected_pct)
            self.assertEqual(snap['free_b'], 200000 * 1024)
            self.assertEqual(snap['buffers_b'], 50000 * 1024)
            self.assertEqual(snap['cached_b'], 80000 * 1024)

    def test_snapshot_repli_noyau_ancien_sans_memavailable(self):
        """Snapshot sans MemAvailable : repli sur MemFree+Buffers+Cached."""
        with tempfile.TemporaryDirectory() as tmp:
            # total 1 000 000, free 200 000, buffers 50 000, cached 80 000
            # available ≈ 330 000, used = 670 000
            fixture = """MemTotal:       1000000 kB
MemFree:         200000 kB
Buffers:          50000 kB
Cached:           80000 kB
"""
            path = self._write_meminfo(tmp, fixture)
            snap = memory.snapshot(path)

            self.assertTrue(snap['available'])
            self.assertIsNone(snap['reason'])
            self.assertEqual(snap['total_b'], 1000000 * 1024)
            expected_avail = (200000 + 50000 + 80000) * 1024
            self.assertEqual(snap['available_b'], expected_avail)
            expected_used = snap['total_b'] - expected_avail
            self.assertEqual(snap['used_b'], expected_used)
            self.assertEqual(snap['used_pct'], round(expected_used / snap['total_b'] * 100, 1))

    def test_snapshot_fichier_absent_disponible_false_raison_non_vide(self):
        """Snapshot quand le fichier est absent : available=False, reason non vide, valeurs None."""
        snap = memory.snapshot('/chemin/inexistant/meminfo')

        self.assertFalse(snap['available'])
        self.assertIsInstance(snap['reason'], str)
        self.assertTrue(len(snap['reason']) > 0)
        self.assertIsNone(snap['total_b'])
        self.assertIsNone(snap['used_b'])
        self.assertIsNone(snap['used_pct'])
        self.assertIsNone(snap['available_b'])
        self.assertIsNone(snap['free_b'])
        self.assertIsNone(snap['buffers_b'])
        self.assertIsNone(snap['cached_b'])
        self.assertIsNone(snap['swap_total_b'])
        self.assertIsNone(snap['swap_used_b'])
        self.assertIsNone(snap['swap_pct'])

    def test_snapshot_memtotal_absent_disponible_false_raison_non_vide(self):
        """Snapshot quand MemTotal est absent : available=False, reason non vide."""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = """MemFree:         200000 kB
MemAvailable:    300000 kB
"""
            path = self._write_meminfo(tmp, fixture)
            snap = memory.snapshot(path)

            self.assertFalse(snap['available'])
            self.assertIsInstance(snap['reason'], str)
            self.assertTrue(len(snap['reason']) > 0)
            self.assertIsNone(snap['total_b'])

    def test_swap_calcul_correct_et_swaptotal_zero(self):
        """Swap : used = total - free, pct correct ; SwapTotal=0 -> swap_pct=0.0 sans division par zéro."""
        with tempfile.TemporaryDirectory() as tmp:
            # Cas normal
            fixture = """MemTotal:       1000000 kB
MemAvailable:    500000 kB
SwapTotal:       500000 kB
SwapFree:        400000 kB
"""
            path = self._write_meminfo(tmp, fixture)
            snap = memory.snapshot(path)

            self.assertEqual(snap['swap_total_b'], 500000 * 1024)
            self.assertEqual(snap['swap_used_b'], 100000 * 1024)
            self.assertEqual(snap['swap_pct'], round(100000 / 500000 * 100, 1))

            # Cas SwapTotal = 0
            fixture_zero = """MemTotal:       1000000 kB
MemAvailable:    500000 kB
SwapTotal:           0 kB
SwapFree:            0 kB
"""
            path_zero = self._write_meminfo(tmp, fixture_zero)
            snap_zero = memory.snapshot(path_zero)

            self.assertEqual(snap_zero['swap_total_b'], 0)
            self.assertEqual(snap_zero['swap_used_b'], 0)
            self.assertEqual(snap_zero['swap_pct'], 0.0)


if __name__ == '__main__':
    unittest.main()
