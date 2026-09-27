import json
import os
import tempfile
import unittest
from unittest.mock import patch

from radar_ops import delegation


class TestClaudeTotals(unittest.TestCase):
    """Tests pour _claude_totals."""

    def test_liste_vide_retourne_zeros(self):
        result = delegation._claude_totals([], since_ts=1000.0, until_ts=2000.0)
        self.assertEqual(result, {
            "calls": 0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0, "cache_read_tokens": 0, "equiv_tokens": 0
        })

    def test_since_ts_none_compte_tout_jusqua_until_ts(self):
        rows = [
            {"ts": 500.0, "prompt_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            {"ts": 1500.0, "prompt_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            {"ts": 2500.0, "prompt_tokens": 30, "output_tokens": 15, "total_tokens": 45},
        ]
        result = delegation._claude_totals(rows, since_ts=None, until_ts=2000.0)
        self.assertEqual(result, {
            "calls": 2,
            "prompt_tokens": 30,
            "output_tokens": 15,
            "total_tokens": 45, "cache_read_tokens": 0, "equiv_tokens": 0
        })

    def test_since_ts_fourni_exclut_avant(self):
        rows = [
            {"ts": 500.0, "prompt_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            {"ts": 1500.0, "prompt_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            {"ts": 2500.0, "prompt_tokens": 30, "output_tokens": 15, "total_tokens": 45},
        ]
        result = delegation._claude_totals(rows, since_ts=1000.0, until_ts=3000.0)
        self.assertEqual(result, {
            "calls": 2,
            "prompt_tokens": 50,
            "output_tokens": 25,
            "total_tokens": 75, "cache_read_tokens": 0, "equiv_tokens": 0
        })

    def test_until_ts_exclut_apres(self):
        """Le point du correctif : until_ts n'était filtré nulle part avant."""
        rows = [
            {"ts": 500.0, "prompt_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            {"ts": 1500.0, "prompt_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            {"ts": 2500.0, "prompt_tokens": 30, "output_tokens": 15, "total_tokens": 45},
        ]
        result = delegation._claude_totals(rows, since_ts=0.0, until_ts=2000.0)
        self.assertEqual(result, {
            "calls": 2,
            "prompt_tokens": 30,
            "output_tokens": 15,
            "total_tokens": 45, "cache_read_tokens": 0, "equiv_tokens": 0
        })

    def test_bornes_inclusives(self):
        rows = [
            {"ts": 1000.0, "prompt_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            {"ts": 2000.0, "prompt_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        ]
        result = delegation._claude_totals(rows, since_ts=1000.0, until_ts=2000.0)
        self.assertEqual(result["calls"], 2)
        self.assertEqual(result["total_tokens"], 45)


class TestSousTraitance(unittest.TestCase):
    """Tests pour _sous_traitance."""

    def test_deux_positifs_ratio_correct(self):
        # share_delegated se calcule maintenant sur les tokens équivalents,
        # pas les jetons bruts : ici les deux coïncident (pas de cache/sortie).
        result = delegation._sous_traitance({"total_tokens": 300, "equiv_tokens": 300},
                                            {"total_tokens": 200, "equiv_tokens": 200})
        self.assertEqual(result["delegated_tokens"], 300)
        self.assertEqual(result["claude_tokens"], 200)
        self.assertEqual(result["total_tokens"], 500)
        self.assertEqual(result["delegated_equiv"], 300)
        self.assertEqual(result["claude_equiv"], 200)
        self.assertEqual(result["total_equiv"], 500)
        self.assertEqual(result["share_delegated"], round(300 / 500, 3))

    def test_claude_zero_delegue_positif_share_1(self):
        result = delegation._sous_traitance({"total_tokens": 100, "equiv_tokens": 100},
                                            {"total_tokens": 0, "equiv_tokens": 0})
        self.assertEqual(result["share_delegated"], 1.0)

    def test_delegue_zero_claude_positif_share_0(self):
        result = delegation._sous_traitance({"total_tokens": 0, "equiv_tokens": 0},
                                            {"total_tokens": 100, "equiv_tokens": 100})
        self.assertEqual(result["share_delegated"], 0.0)

    def test_les_deux_zero_share_none(self):
        """Distinction importante : None (absence de mesure), pas 0.0 (mesure nulle)."""
        result = delegation._sous_traitance({"total_tokens": 0}, {"total_tokens": 0})
        self.assertIsNone(result["share_delegated"])
        self.assertEqual(result["total_tokens"], 0)

    def test_champs_manquants_traites_comme_zero(self):
        result = delegation._sous_traitance({}, {})
        self.assertIsNone(result["share_delegated"])


class TestSnapshotWindowedClaudeTotals(unittest.TestCase):
    """Intégration : claude_totals/sous_traitance dans snapshot_windowed, fenêtrage custom."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_root = self.tmpdir.name
        self.ops_dir = os.path.join(self.data_root, "ops")
        os.makedirs(os.path.join(self.ops_dir, "ai"), exist_ok=True)
        with open(os.path.join(self.ops_dir, "ai", delegation.HEALTH_NAME), "w", encoding="utf-8") as f:
            json.dump({"entries": {}}, f)
        self.receipts_path = os.path.join(self.ops_dir, delegation.RECEIPTS_NAME)
        self.events_path = os.path.join(self.ops_dir, delegation.EVENTS_NAME)
        self.env_patch = patch.dict(os.environ, {"RADAR_OPS_TELEMETRY_DIR": self.ops_dir}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _write_receipt(self, ts, total_tokens):
        with open(self.receipts_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": ts, "provider": "gemini", "model": "gemini-3.5-flash-lite",
                "mode": "chat", "status": "ok",
                "prompt_tokens": total_tokens, "output_tokens": 0,
                "total_tokens": total_tokens,
            }) + "\n")

    def _write_claude_event(self, ts, total_tokens):
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "kind": "claude_usage", "ts": ts, "model": "claude-sonnet-5",
                "input_tokens": total_tokens, "output_tokens": 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            }) + "\n")

    def test_fenetre_custom_exclut_claude_apres_to_ts(self):
        """C'est le point précis du correctif : sans lui, une période custom bornée
        dans le passé compterait à tort l'activité Claude survenue après to_ts."""
        self._write_receipt(ts=1500.0, total_tokens=200)
        self._write_claude_event(ts=1500.0, total_tokens=95)
        self._write_claude_event(ts=2500.0, total_tokens=180)  # hors fenêtre

        snap = delegation.snapshot_windowed(self.data_root, preset="custom",
                                            from_ts=1000.0, to_ts=2000.0)

        self.assertEqual(snap["claude_totals"]["calls"], 1)
        self.assertEqual(snap["claude_totals"]["total_tokens"], 95)
        self.assertEqual(snap["totals"]["total_tokens"], 200)

        st = snap["sous_traitance"]
        self.assertEqual(st["delegated_tokens"], 200)
        self.assertEqual(st["claude_tokens"], 95)
        self.assertEqual(st["total_tokens"], 295)
        self.assertAlmostEqual(st["share_delegated"], round(200 / 295, 3), places=3)

    def test_fenetre_sans_recu_delegue(self):
        """Que du Claude dans la fenêtre : part sous-traitée à 0, pas None."""
        self._write_claude_event(ts=1500.0, total_tokens=70)

        snap = delegation.snapshot_windowed(self.data_root, preset="custom",
                                            from_ts=1000.0, to_ts=2000.0)

        self.assertEqual(snap["totals"]["total_tokens"], 0)
        self.assertEqual(snap["claude_totals"]["total_tokens"], 70)
        self.assertEqual(snap["sous_traitance"]["share_delegated"], 0.0)


if __name__ == "__main__":
    unittest.main()
