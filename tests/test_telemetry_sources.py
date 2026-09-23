"""Tests unitaires pour la gestion de la télémétrie et son rapatriement.

Couvre les sources de données lues par radar_ops.delegation et les mécanismes
d'ingestion et de rotation de scripts/telemetry_pull.py.
"""

import importlib.util
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import radar_ops.delegation as delegation

# Chargement dynamique de scripts/telemetry_pull.py (scripts/ n'est pas un paquet)
_SCRIPT_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "scripts", "telemetry_pull.py"
    )
)
_spec = importlib.util.spec_from_file_location("telemetry_pull", _SCRIPT_PATH)
telemetry_pull = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(telemetry_pull)


class TestDelegationSources(unittest.TestCase):
    """Vérifie l'agrégation et le suivi des sources locales et distantes."""

    def test_snapshot_fusionne_local_et_remote(self):
        """Vérifie que snapshot() agrège événements locaux et rapatriés."""
        with tempfile.TemporaryDirectory() as root:
            ops_dir = os.path.join(root, "ops")
            remote_dir = os.path.join(ops_dir, "remote")
            os.makedirs(remote_dir, exist_ok=True)

            now = time.time()
            ts_recent = now - 100
            ts_ancien = now - 200

            # Journal local de télémétrie (ts récent)
            with open(
                os.path.join(ops_dir, "telemetry.jsonl"), "w", encoding="utf-8"
            ) as f:
                f.write(
                    json.dumps(
                        {"ts": ts_recent, "kind": "prompt", "session": "s1"}
                    )
                    + "\n"
                )

            # Journal rapatrié de télémétrie (ts plus ancien)
            with open(
                os.path.join(remote_dir, "telemetry.jsonl"),
                "w",
                encoding="utf-8",
            ) as f:
                f.write(
                    json.dumps(
                        {"ts": ts_ancien, "kind": "tool", "session": "s2"}
                    )
                    + "\n"
                )

            # Journal des reçus Gemini
            with open(
                os.path.join(ops_dir, "gemini-receipts.jsonl"),
                "w",
                encoding="utf-8",
            ) as f:
                f.write(
                    json.dumps(
                        {
                            "ts": now - 50,
                            "mode": "cli",
                            "status": "ok",
                            "total_tokens": 12,
                        }
                    )
                    + "\n"
                )

            with patch.dict(os.environ, {}, clear=True):
                snap = delegation.snapshot(root)

            self.assertEqual(snap["events_seen"], 2)
            self.assertEqual(snap["receipts_seen"], 1)
            self.assertEqual(snap["missing"], [])

    def test_snapshot_missing_seulement_si_les_deux_origines_manquent(self):
        """Vérifie qu'un journal n'est absent que si local et remote manquent."""
        with tempfile.TemporaryDirectory() as root:
            ops_dir = os.path.join(root, "ops")
            remote_dir = os.path.join(ops_dir, "remote")
            os.makedirs(remote_dir, exist_ok=True)

            # Uniquement gemini-receipts.jsonl sous remote/
            with open(
                os.path.join(remote_dir, "gemini-receipts.jsonl"),
                "w",
                encoding="utf-8",
            ) as f:
                f.write(
                    json.dumps(
                        {
                            "ts": time.time() - 10,
                            "mode": "chat",
                            "status": "ok",
                        }
                    )
                    + "\n"
                )

            with patch.dict(os.environ, {}, clear=True):
                snap = delegation.snapshot(root)

            self.assertNotIn("gemini-receipts.jsonl", snap["missing"])
            self.assertIn("telemetry.jsonl", snap["missing"])


class TestPullDestination(unittest.TestCase):
    """Vérifie le routage des réceptions et la politique de rotation."""

    def test_destination_par_defaut_est_un_sous_dossier_remote(self):
        """Vérifie que DEFAULT_DEST isole les réceptions dans ops/remote."""
        basename = os.path.basename(telemetry_pull.DEFAULT_DEST)
        parent = os.path.basename(
            os.path.dirname(telemetry_pull.DEFAULT_DEST)
        )
        self.assertEqual(basename, "remote")
        self.assertEqual(parent, "ops")

    def test_merge_fait_tourner_le_fichier_au_dela_de_la_borne(self):
        """Vérifie la rotation en .1 dès que MAX_BYTES est franchi."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            local_file = os.path.join(tmp_dir, "telemetry.jsonl")
            rotated_file = local_file + ".1"

            # Ligne volumineuse dépassant le seuil de bascule
            payload = "x" * (telemetry_pull.MAX_BYTES + 1024)
            large_row = json.dumps({"ts": 1000.0, "pad": payload}) + "\n"
            with open(local_file, "w", encoding="utf-8") as f:
                f.write(large_row)

            # Ligne entrante postérieure
            incoming = json.dumps({"ts": 2000.0, "event": "fresh"}) + "\n"
            added = telemetry_pull.merge(local_file, incoming)

            self.assertEqual(added, 1)
            self.assertTrue(
                os.path.exists(rotated_file),
                "Le fichier d'archive .1 doit être présent.",
            )
            with open(local_file, "r", encoding="utf-8") as f:
                content = f.read()

            # Après rotation, le fichier local ne doit conserver que la ligne reçue
            self.assertEqual(content, incoming)


if __name__ == "__main__":
    unittest.main()
