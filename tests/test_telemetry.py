import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch

# Chargement dynamique du module requis sans dépendre de son emplacement en tant que paquet
def _load_module(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

telemetry_path_file = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts", "hooks", "telemetry.py")
)

telemetry = _load_module("telemetry", telemetry_path_file)


class TestTelemetry(unittest.TestCase):
    """Suite de tests unitaire complète pour telemetry.py."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = self.temp_dir.name

    def tearDown(self):
        self.temp_dir.cleanup()

    # --- Tests pour telemetry.py ---

    def test_build_record_evenement_inconnu(self):
        """Vérifie que build_record renvoie None sur un événement inconnu."""
        event = {"hook_event_name": "Inconnu"}
        self.assertIsNone(telemetry.build_record(event, self.project_dir))

    def test_build_record_types_session_et_stop(self):
        """Vérifie que SessionStart, SessionEnd, Stop donnent les kinds attendus."""
        for name, expected_kind in [
            ("SessionStart", "session_start"),
            ("SessionEnd", "session_end"),
            ("Stop", "turn_end"),
        ]:
            event = {"hook_event_name": name, "session_id": "sess-123"}
            record = telemetry.build_record(event, self.project_dir)
            self.assertEqual(record["kind"], expected_kind)
            self.assertEqual(record["session"], "sess-123")
            self.assertIn("ts", record)

    def test_build_record_user_prompt_submit_politiques(self):
        """Vérifie que UserPromptSubmit journalise chars et respecte none, head, full."""
        prompt_text = "A" * 300
        event = {"hook_event_name": "UserPromptSubmit", "prompt": prompt_text}

        # Politique "none" (aucune clé head)
        rec_none = telemetry.build_record(event, self.project_dir, prompt_policy="none")
        self.assertEqual(rec_none["chars"], 300)
        self.assertNotIn("head", rec_none)

        # Politique "head" (200 premiers caractères seulement)
        rec_head = telemetry.build_record(event, self.project_dir, prompt_policy="head")
        self.assertEqual(rec_head["chars"], 300)
        self.assertEqual(rec_head["head"], "A" * 200)

        # Politique "full" (texte entier)
        rec_full = telemetry.build_record(event, self.project_dir, prompt_policy="full")
        self.assertEqual(rec_full["chars"], 300)
        self.assertEqual(rec_full["head"], prompt_text)

    def test_tool_label_bash_securite(self):
        """Vérifie que tool_label pour Bash renvoie la description et JAMAIS la commande."""
        tool_input = {
            "description": "Exécuter les tests unitaires",
            "command": "rm -rf / --secret-token=XYZ123"
        }
        label = telemetry.tool_label("Bash", tool_input, self.project_dir)
        self.assertEqual(label, "Exécuter les tests unitaires")
        self.assertNotIn("command", label)
        self.assertNotIn("XYZ123", label)

    def test_tool_label_chemins_relatifs(self):
        """Vérifie que tool_label pour Write/Edit/Read renvoie un chemin relatif au project_dir."""
        sub_path = os.path.join(self.project_dir, "src", "main.py")
        for tool_name in ["Write", "Edit", "Read", "NotebookEdit"]:
            input_key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
            tool_input = {input_key: sub_path}
            label = telemetry.tool_label(tool_name, tool_input, self.project_dir)
            self.assertEqual(label, os.path.join("src", "main.py"))

    def test_tool_label_glob_grep(self):
        """Vérifie que tool_label pour Glob/Grep renvoie le pattern."""
        for tool_name in ["Glob", "Grep"]:
            tool_input = {"pattern": "*.py"}
            label = telemetry.tool_label(tool_name, tool_input, self.project_dir)
            self.assertEqual(label, "*.py")

    def test_tool_label_troncature(self):
        """Vérifie que tool_label tronque à 160 caractères."""
        long_desc = "B" * 200
        tool_input = {"description": long_desc}
        label = telemetry.tool_label("Bash", tool_input, self.project_dir)
        self.assertEqual(len(label), telemetry.MAX_LABEL)
        self.assertEqual(label, "B" * telemetry.MAX_LABEL)

    def test_tool_ok_cas_limites(self):
        """Vérifie que tool_ok renvoie False sur erreur et True sur dict avec errors vide."""
        self.assertTrue(telemetry.tool_ok(None))
        self.assertTrue(telemetry.tool_ok({}))
        self.assertTrue(telemetry.tool_ok({"errors": []}))
        self.assertFalse(telemetry.tool_ok({"is_error": True}))
        self.assertFalse(telemetry.tool_ok({"isError": True}))
        self.assertFalse(telemetry.tool_ok({"error": "Échec critique"}))

    def test_write_line_creation_rotation(self):
        """Vérifie que write_line crée le dossier, écrit du JSONL et fait tourner .1 si MAX_BYTES dépassé."""
        target_path = os.path.join(self.project_dir, ".claude", "telemetry.jsonl")

        # Écriture initiale
        record1 = {"kind": "session_start", "ts": 123456789.0}
        telemetry.write_line(target_path, record1)
        self.assertTrue(os.path.exists(target_path))

        with open(target_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 1)

        # Forcer un dépassement de MAX_BYTES en modifiant temporairement la constante
        original_max_bytes = telemetry.MAX_BYTES
        telemetry.MAX_BYTES = 15  # Seuil très bas

        try:
            record2 = {"kind": "turn_end", "ts": 123456790.0}
            telemetry.write_line(target_path, record2)

            # Vérification de la rotation
            self.assertTrue(os.path.exists(target_path + ".1"))
            with open(target_path + ".1", "r", encoding="utf-8") as f:
                rotated_content = f.read()
            self.assertIn("session_start", rotated_content)
        finally:
            telemetry.MAX_BYTES = original_max_bytes

    def test_telemetry_path_environnement(self):
        """Vérifie telemetry_path par défaut et sous RADAR_TELEMETRY_DIR via patch.dict."""
        # Cas par défaut (ni variable, ni data/ops) → .claude/
        env_clean = {k: v for k, v in os.environ.items() if k != "RADAR_TELEMETRY_DIR"}
        with patch.dict(os.environ, env_clean, clear=True):
            default_p = telemetry.telemetry_path(self.project_dir)
            self.assertEqual(default_p, os.path.join(self.project_dir, ".claude", "telemetry.jsonl"))

        # Cas avec RADAR_TELEMETRY_DIR
        custom_dir = os.path.join(self.project_dir, "custom_ops")
        with patch.dict(os.environ, {"RADAR_TELEMETRY_DIR": custom_dir}):
            custom_p = telemetry.telemetry_path(self.project_dir)
            self.assertEqual(custom_p, os.path.join(custom_dir, "telemetry.jsonl"))

    def test_telemetry_path_data_ops(self):
        """Vérifie que telemetry_path préfère data/ops quand le dossier existe."""
        data_ops = os.path.join(self.project_dir, "data", "ops")
        os.makedirs(data_ops)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RADAR_TELEMETRY_DIR", None)
            p = telemetry.telemetry_path(self.project_dir)
            self.assertEqual(p, os.path.join(data_ops, "telemetry.jsonl"))

    def test_main_entree_vide_et_invalide(self):
        """Vérifie que main ne lève jamais et renvoie 0 sur entrée vide et JSON invalide."""
        # Entrée vide
        with patch("sys.stdin", unittest.mock.mock_open(read_data="")):
            self.assertEqual(telemetry.main(), 0)

        # JSON invalide
        with patch("sys.stdin", unittest.mock.mock_open(read_data="INVALID JSON {{{")):
            self.assertEqual(telemetry.main(), 0)

        # Entrée JSON non-dictionnaire
        with patch("sys.stdin", unittest.mock.mock_open(read_data="[1, 2, 3]")):
            self.assertEqual(telemetry.main(), 0)



if __name__ == "__main__":
    unittest.main()
