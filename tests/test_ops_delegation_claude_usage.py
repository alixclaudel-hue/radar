import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from radar_ops import delegation


class TestClaudeUsageRows(unittest.TestCase):
    """Tests pour la fonction _claude_usage_rows."""

    def test_claude_usage_rows_nominal(self):
        """Un event claude_usage valide produit une row avec jetons agrégés."""
        events = [{
            "kind": "claude_usage",
            "ts": 1000.0,
            "model": "claude-sonnet-5",
            "input_tokens": 2,
            "output_tokens": 50,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 10,
        }]
        rows = delegation._claude_usage_rows(events)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["ts"], 1000.0)
        self.assertEqual(row["model"], "claude-sonnet-5")
        self.assertEqual(row["prompt_tokens"], 112)  # 2 + 100 + 10
        self.assertEqual(row["output_tokens"], 50)
        self.assertEqual(row["total_tokens"], 162)   # 112 + 50

    def test_claude_usage_rows_ignore_autres_kinds(self):
        """Les events d'autres kinds (tool, prompt) sont complètement ignorés."""
        events = [
            {"kind": "tool", "ts": 1000.0, "model": "x", "input_tokens": 5},
            {"kind": "prompt", "ts": 1001.0, "model": "y", "output_tokens": 3},
            {"kind": "claude_usage", "ts": 1002.0, "model": "claude-1", "input_tokens": 1, "output_tokens": 1},
        ]
        rows = delegation._claude_usage_rows(events)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "claude-1")

    def test_claude_usage_rows_sans_ts_ou_invalide(self):
        """Event sans ts ou ts non numérique est ignoré sans lever d'exception."""
        events = [
            {"kind": "claude_usage", "model": "m1", "input_tokens": 1},
            {"kind": "claude_usage", "ts": "pas_un_nombre", "model": "m2", "input_tokens": 1},
            {"kind": "claude_usage", "ts": None, "model": "m3", "input_tokens": 1},
            {"kind": "claude_usage", "ts": 2000.0, "model": "m4", "input_tokens": 1},
        ]
        rows = delegation._claude_usage_rows(events)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "m4")
        self.assertEqual(rows[0]["ts"], 2000.0)

    def test_claude_usage_rows_sans_model(self):
        """Event sans model produit une row avec model='inconnu'."""
        events = [{"kind": "claude_usage", "ts": 3000.0, "input_tokens": 5, "output_tokens": 7}]
        rows = delegation._claude_usage_rows(events)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "inconnu")
        self.assertEqual(rows[0]["prompt_tokens"], 5)
        self.assertEqual(rows[0]["output_tokens"], 7)
        self.assertEqual(rows[0]["total_tokens"], 12)

    def test_claude_usage_rows_champs_jetons_absents_ou_invalides(self):
        """Champs de jetons absents ou non numériques valent 0 dans la row."""
        events = [
            {"kind": "claude_usage", "ts": 4000.0, "model": "m1",
             "input_tokens": "pas_un_nombre", "output_tokens": None},
            {"kind": "claude_usage", "ts": 4001.0, "model": "m2"},
        ]
        rows = delegation._claude_usage_rows(events)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["prompt_tokens"], 0)
            self.assertEqual(row["output_tokens"], 0)
            self.assertEqual(row["total_tokens"], 0)

    def test_claude_usage_rows_liste_vide(self):
        """Liste vide en entrée renvoie liste vide."""
        self.assertEqual(delegation._claude_usage_rows([]), [])


class TestBucketByTimeAvecClaudeRows(unittest.TestCase):
    """Tests pour bucket_by_time avec le paramètre claude_rows."""

    def test_bucket_by_time_claude_rows_dans_fenetre(self):
        """Une claude_row dans la fenêtre ajoute ses jetons au by_model du bucket, sans incrémenter calls du bucket."""
        now = 1_759_000_000.0
        receipts = []
        claude_rows = [{
            "ts": now - 1800,
            "model": "claude-sonnet-5",
            "prompt_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        }]
        buckets = delegation.bucket_by_time(
            receipts, now - 3600, now, "hour", claude_rows=claude_rows
        )
        # Au moins un bucket (celui contenant now-1800)
        bucket = next(b for b in buckets if b["calls"] == 0 and b["by_model"])
        self.assertEqual(bucket["calls"], 0)  # bucket calls inchangé
        self.assertIn("claude-sonnet-5", bucket["by_model"])
        m = bucket["by_model"]["claude-sonnet-5"]
        self.assertEqual(m["total_tokens"], 150)
        self.assertEqual(m["prompt_tokens"], 100)
        self.assertEqual(m["output_tokens"], 50)
        self.assertEqual(m["calls"], 1)  # by_model calls incrémenté pour la ligne claude

    def test_bucket_by_time_claude_rows_hors_fenetre(self):
        """Une claude_row hors de [since_ts, until_ts] est ignorée."""
        now = 1_759_000_000.0
        receipts = []
        claude_rows = [{
            "ts": now - 10 * 3600,
            "model": "claude-sonnet-5",
            "prompt_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        }]
        buckets = delegation.bucket_by_time(
            receipts, now - 3600, now, "hour", claude_rows=claude_rows
        )
        # Tous les buckets ont by_model vide
        for b in buckets:
            self.assertEqual(b["by_model"], {})

    def test_bucket_by_time_claude_rows_et_receipts_meme_modele(self):
        """Receipt et claude_row même modèle même bucket : jetons additionnés dans même entrée by_model."""
        now = 1_759_000_000.0
        ts = now - 1800
        receipts = [{
            "ts": ts,
            "model": "shared-model",
            "prompt_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "status": "ok",
        }]
        claude_rows = [{
            "ts": ts,
            "model": "shared-model",
            "prompt_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        }]
        buckets = delegation.bucket_by_time(
            receipts, now - 3600, now, "hour", claude_rows=claude_rows
        )
        bucket = next(b for b in buckets if b["calls"] > 0)
        self.assertEqual(bucket["calls"], 1)  # seul le reçu incrémente bucket calls
        m = bucket["by_model"]["shared-model"]
        self.assertEqual(m["total_tokens"], 180)  # 30 + 150
        self.assertEqual(m["prompt_tokens"], 110)  # 10 + 100
        self.assertEqual(m["output_tokens"], 70)   # 20 + 50
        self.assertEqual(m["calls"], 2)  # reçu + claude_row incrémentent tous deux by_model[model].calls

    def test_bucket_by_time_sans_claude_rows_defaut(self):
        """Appel sans passer claude_rows (None) ne lève pas d'erreur et se comporte comme avant."""
        now = 1_759_000_000.0
        receipts = [{"ts": now - 1800, "model": "m1", "total_tokens": 10, "status": "ok"}]
        # Ne doit pas lever
        buckets = delegation.bucket_by_time(receipts, now - 3600, now, "hour")
        self.assertEqual(len(buckets) >= 1, True)
        bucket = next(b for b in buckets if b["calls"] > 0)
        self.assertEqual(bucket["calls"], 1)
        self.assertEqual(bucket["by_model"]["m1"]["total_tokens"], 10)

    def test_bucket_by_time_plusieurs_claude_rows_modeles_differents(self):
        """Plusieurs claude_rows de modèles différents dans le même bucket créent des entrées distinctes."""
        now = 1_759_000_000.0
        ts = now - 1800
        receipts = []
        claude_rows = [
            {"ts": ts, "model": "claude-a", "prompt_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            {"ts": ts, "model": "claude-b", "prompt_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        ]
        buckets = delegation.bucket_by_time(
            receipts, now - 3600, now, "hour", claude_rows=claude_rows
        )
        bucket = next(b for b in buckets if b["by_model"])
        self.assertEqual(set(bucket["by_model"].keys()), {"claude-a", "claude-b"})
        self.assertEqual(bucket["by_model"]["claude-a"]["total_tokens"], 15)
        self.assertEqual(bucket["by_model"]["claude-b"]["total_tokens"], 30)


class TestSnapshotWindowedModelColors(unittest.TestCase):
    """Test pour l'extension de model_colors dans snapshot_windowed."""

    def test_snapshot_windowed_model_colors_inclut_claude_et_gemini(self):
        """model_colors contient une entrée pour le modèle Gemini des reçus ET pour le modèle Claude de la télémétrie, couleurs distinctes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ops = os.path.join(tmpdir, "ops")
            os.makedirs(ops, exist_ok=True)
            now = time.time()
            # Reçu Gemini
            with open(os.path.join(ops, delegation.RECEIPTS_NAME), "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": now - 60,
                    "model": "gemini-3.5-flash-lite",
                    "provider": "gemini",
                    "status": "ok",
                    "prompt_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                }) + "\n")
            # Event Claude usage
            with open(os.path.join(ops, delegation.EVENTS_NAME), "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "kind": "claude_usage",
                    "ts": now - 30,
                    "model": "claude-sonnet-5",
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                }) + "\n")
            # Health vide
            os.makedirs(os.path.join(ops, "ai"), exist_ok=True)
            with open(os.path.join(ops, "ai", delegation.HEALTH_NAME), "w", encoding="utf-8") as f:
                json.dump({"entries": {}}, f)

            with patch.dict(os.environ, {"RADAR_OPS_TELEMETRY_DIR": ops}, clear=True):
                snap = delegation.snapshot_windowed(tmpdir, preset="all")

            model_colors = snap["model_colors"]
            self.assertIn("gemini-3.5-flash-lite", model_colors)
            self.assertIn("claude-sonnet-5", model_colors)
            self.assertTrue(model_colors["gemini-3.5-flash-lite"].startswith("#"))
            self.assertTrue(model_colors["claude-sonnet-5"].startswith("#"))
            self.assertNotEqual(model_colors["gemini-3.5-flash-lite"], model_colors["claude-sonnet-5"])


if __name__ == "__main__":
    unittest.main()
