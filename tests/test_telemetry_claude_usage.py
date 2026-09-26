import importlib.util
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch


def _load_telemetry_module():
    """Charge le module telemetry.py depuis son chemin source."""
    spec = importlib.util.spec_from_file_location(
        "telemetry",
        os.path.join(os.path.dirname(__file__), "..", "scripts", "hooks", "telemetry.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTelemetryNouvellesFonctions(unittest.TestCase):
    """Tests pour les nouvelles fonctions de telemetry.py."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.telemetry = _load_telemetry_module()

    # --- usage_state_path ---
    def test_usage_state_path_renvoie_chemin_attendu(self):
        """usage_state_path renvoie <telemetry_dir>/.claude_usage_state/<session_id>.json."""
        telemetry_dir = os.path.join(self.tempdir.name, "telemetry")
        session_id = "sess-abc123"
        expected = os.path.join(telemetry_dir, ".claude_usage_state", "sess-abc123.json")
        self.assertEqual(self.telemetry.usage_state_path(telemetry_dir, session_id), expected)

    # --- load_usage_offset ---
    def test_load_usage_offset_fichier_absent_retourne_zero(self):
        """Fichier d'état absent -> 0."""
        state_path = os.path.join(self.tempdir.name, "absent.json")
        self.assertEqual(self.telemetry.load_usage_offset(state_path), 0)

    def test_load_usage_offset_json_invalide_retourne_zero(self):
        """Fichier JSON invalide -> 0."""
        state_path = os.path.join(self.tempdir.name, "invalid.json")
        with open(state_path, "w", encoding="utf-8") as f:
            f.write("{ pas du json }")
        self.assertEqual(self.telemetry.load_usage_offset(state_path), 0)

    def test_load_usage_offset_json_valide_pas_dict_retourne_zero(self):
        """Fichier JSON valide mais pas un dict -> 0."""
        state_path = os.path.join(self.tempdir.name, "not_dict.json")
        with open(state_path, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        self.assertEqual(self.telemetry.load_usage_offset(state_path), 0)

    def test_load_usage_offset_dict_sans_offset_retourne_zero(self):
        """Dict sans champ offset -> 0."""
        state_path = os.path.join(self.tempdir.name, "no_offset.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"autre": 123}, f)
        self.assertEqual(self.telemetry.load_usage_offset(state_path), 0)

    def test_load_usage_offset_offset_non_entier_retourne_zero(self):
        """Champ offset présent mais pas un entier -> 0."""
        state_path = os.path.join(self.tempdir.name, "bad_offset.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"offset": "pas un entier"}, f)
        self.assertEqual(self.telemetry.load_usage_offset(state_path), 0)

    def test_load_usage_offset_offset_entier_valide_retourne_offset(self):
        """Fichier {"offset": 123} -> 123."""
        state_path = os.path.join(self.tempdir.name, "valid.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"offset": 123}, f)
        self.assertEqual(self.telemetry.load_usage_offset(state_path), 123)

    # --- save_usage_offset ---
    def test_save_usage_offset_aller_retour_avec_load(self):
        """Écriture puis lecture redonne le même offset (aller-retour)."""
        state_path = os.path.join(self.tempdir.name, "state.json")
        self.telemetry.save_usage_offset(state_path, 456)
        self.assertEqual(self.telemetry.load_usage_offset(state_path), 456)

    def test_save_usage_offset_cree_dossier_parent_si_inexistant(self):
        """Crée le dossier parent s'il n'existe pas."""
        state_path = os.path.join(self.tempdir.name, "nouveau", "sous", "state.json")
        self.telemetry.save_usage_offset(state_path, 789)
        self.assertTrue(os.path.exists(state_path))
        self.assertEqual(self.telemetry.load_usage_offset(state_path), 789)

    def test_save_usage_offset_chemin_invalide_ne_leve_pas_exception(self):
        """Chemin invalide (composant existant comme fichier) -> ne lève aucune exception."""
        # Crée un fichier là où on s'attend à un dossier
        blocking_file = os.path.join(self.tempdir.name, "blocking")
        with open(blocking_file, "w") as f:
            f.write("je suis un fichier")
        # Tente d'écrire dans blocking/session.json (impossible car blocking n'est pas un dossier)
        state_path = os.path.join(blocking_file, "session.json")
        try:
            self.telemetry.save_usage_offset(state_path, 999)
        except Exception as e:
            self.fail(f"save_usage_offset a levé une exception: {e}")

    # --- claude_usage_deltas ---
    def _write_transcript(self, lines):
        """Écrit un transcript JSONL et retourne son chemin."""
        path = os.path.join(self.tempdir.name, "transcript.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        return path

    def test_claude_usage_deltas_meme_requestId_dedoublonne(self):
        """Deux lignes même requestId -> un seul delta avec les bons jetons."""
        line = {
            "type": "assistant",
            "requestId": "req1",
            "message": {
                "model": "claude-sonnet-5",
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 10
                }
            }
        }
        path = self._write_transcript([line, line])
        deltas, new_offset = self.telemetry.claude_usage_deltas(path, 0)
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["model"], "claude-sonnet-5")
        self.assertEqual(deltas[0]["input_tokens"], 2)
        self.assertEqual(deltas[0]["output_tokens"], 50)
        self.assertEqual(deltas[0]["cache_creation_input_tokens"], 100)
        self.assertEqual(deltas[0]["cache_read_input_tokens"], 10)

    def test_claude_usage_deltas_requestId_differents_deux_deltas(self):
        """Deux lignes requestId différents -> deux deltas distincts dans l'ordre."""
        line1 = {"type": "assistant", "requestId": "req1", "message": {"model": "m1", "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}}}
        line2 = {"type": "assistant", "requestId": "req2", "message": {"model": "m2", "usage": {"input_tokens": 5, "output_tokens": 6, "cache_creation_input_tokens": 7, "cache_read_input_tokens": 8}}}
        path = self._write_transcript([line1, line2])
        deltas, _ = self.telemetry.claude_usage_deltas(path, 0)
        self.assertEqual(len(deltas), 2)
        self.assertEqual(deltas[0]["model"], "m1")
        self.assertEqual(deltas[1]["model"], "m2")

    def test_claude_usage_deltas_type_user_ignore(self):
        """Ligne type user -> ignorée, aucun delta."""
        line = {"type": "user", "message": {"model": "m", "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}}}
        path = self._write_transcript([line])
        deltas, _ = self.telemetry.claude_usage_deltas(path, 0)
        self.assertEqual(deltas, [])

    def test_claude_usage_deltas_ligne_json_invalide_ignoree_silencieusement(self):
        """Ligne JSON invalide au milieu -> ignorée, autres lignes traitées."""
        line1 = {"type": "assistant", "requestId": "req1", "message": {"model": "m1", "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}}}
        line2 = {"type": "assistant", "requestId": "req2", "message": {"model": "m2", "usage": {"input_tokens": 5, "output_tokens": 6, "cache_creation_input_tokens": 7, "cache_read_input_tokens": 8}}}
        path = os.path.join(self.tempdir.name, "transcript.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(line1) + "\n")
            f.write("{ pas du json }\n")
            f.write(json.dumps(line2) + "\n")
        deltas, _ = self.telemetry.claude_usage_deltas(path, 0)
        self.assertEqual(len(deltas), 2)
        self.assertEqual(deltas[0]["model"], "m1")
        self.assertEqual(deltas[1]["model"], "m2")

    def test_claude_usage_deltas_ligne_incomplete_sans_retour_ligne_non_comptee(self):
        """Dernière ligne sans \n final -> non comptée, offset pointe avant elle."""
        line1 = {"type": "assistant", "requestId": "req1", "message": {"model": "m1", "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}}}
        line2 = {"type": "assistant", "requestId": "req2", "message": {"model": "m2", "usage": {"input_tokens": 5, "output_tokens": 6, "cache_creation_input_tokens": 7, "cache_read_input_tokens": 8}}}
        path = os.path.join(self.tempdir.name, "transcript.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(line1) + "\n")
            f.write(json.dumps(line2))  # SANS \n final
        # Premier appel : seule la première ligne complète est traitée
        deltas, new_offset = self.telemetry.claude_usage_deltas(path, 0)
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["model"], "m1")
        # Vérifie que le nouvel offset pointe juste avant la ligne incomplète
        with open(path, "rb") as f:
            f.seek(new_offset)
            remaining = f.read()
        self.assertEqual(remaining.decode("utf-8"), json.dumps(line2))
        # Complète le fichier avec \n et rappelle avec le nouvel offset
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n")
        deltas2, new_offset2 = self.telemetry.claude_usage_deltas(path, new_offset)
        self.assertEqual(len(deltas2), 1)
        self.assertEqual(deltas2[0]["model"], "m2")

    def test_claude_usage_deltas_offset_au_dela_taille_fichier_repart_de_zero(self):
        """Offset > taille fichier -> repart de 0 sans exception."""
        line = {"type": "assistant", "requestId": "req1", "message": {"model": "m", "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}}}
        path = self._write_transcript([line])
        file_size = os.path.getsize(path)
        deltas, new_offset = self.telemetry.claude_usage_deltas(path, file_size + 100)
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["model"], "m")

    def test_claude_usage_deltas_fichier_inexistant_retourne_liste_vide_offset_inchange(self):
        """Fichier inexistant -> ([], offset) sans exception."""
        path = os.path.join(self.tempdir.name, "inexistant.jsonl")
        deltas, offset = self.telemetry.claude_usage_deltas(path, 123)
        self.assertEqual(deltas, [])
        self.assertEqual(offset, 123)

    def test_claude_usage_deltas_sans_usage_ou_valeurs_non_numeriques_zeros(self):
        """Message sans usage ou valeurs non numériques -> delta avec 0 pour champs manquants/invalides."""
        line1 = {"type": "assistant", "requestId": "req1", "message": {"model": "m1"}}  # pas d'usage
        line2 = {"type": "assistant", "requestId": "req2", "message": {"model": "m2", "usage": {"input_tokens": "abc", "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}}}
        path = self._write_transcript([line1, line2])
        deltas, _ = self.telemetry.claude_usage_deltas(path, 0)
        self.assertEqual(len(deltas), 2)
        self.assertEqual(deltas[0]["input_tokens"], 0)
        self.assertEqual(deltas[0]["output_tokens"], 0)
        self.assertEqual(deltas[0]["cache_creation_input_tokens"], 0)
        self.assertEqual(deltas[0]["cache_read_input_tokens"], 0)
        self.assertEqual(deltas[1]["input_tokens"], 0)  # "abc" -> 0
        self.assertEqual(deltas[1]["output_tokens"], 2)

    def test_claude_usage_deltas_sans_model_inconnu(self):
        """Message sans model -> delta avec model 'inconnu'."""
        line = {"type": "assistant", "requestId": "req1", "message": {"usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}}}
        path = self._write_transcript([line])
        deltas, _ = self.telemetry.claude_usage_deltas(path, 0)
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["model"], "inconnu")

    def test_claude_usage_deltas_offset_arithmetique_precise(self):
        """Offset retourné == offset initial + octets des lignes complètes consommées."""
        line1 = {"type": "assistant", "requestId": "req1", "message": {"model": "m1", "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}}}
        line2 = {"type": "assistant", "requestId": "req2", "message": {"model": "m2", "usage": {"input_tokens": 5, "output_tokens": 6, "cache_creation_input_tokens": 7, "cache_read_input_tokens": 8}}}
        path = os.path.join(self.tempdir.name, "transcript.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(line1) + "\n")
            f.write(json.dumps(line2) + "\n")
        # Calcule la taille exacte des deux lignes complètes
        with open(path, "rb") as f:
            data = f.read()
        expected_new_offset = len(data)
        deltas, new_offset = self.telemetry.claude_usage_deltas(path, 0)
        self.assertEqual(new_offset, expected_new_offset)
        self.assertEqual(len(deltas), 2)

    # --- log_claude_usage (intégration) ---
    def test_log_claude_usage_integration_deux_tours_ecrit_deux_lignes(self):
        """Intégration : 2 tours assistant -> 2 lignes kind=claude_usage dans telemetry.jsonl."""
        project_dir = self.tempdir.name
        telemetry_dir = os.path.join(project_dir, "telemetry_data")
        os.makedirs(telemetry_dir, exist_ok=True)
        transcript_path = os.path.join(project_dir, "transcript.jsonl")
        
        line1 = {
            "type": "assistant",
            "requestId": "req1",
            "message": {
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40}
            }
        }
        line2 = {
            "type": "assistant",
            "requestId": "req2",
            "message": {
                "model": "claude-opus-4",
                "usage": {"input_tokens": 5, "output_tokens": 15, "cache_creation_input_tokens": 25, "cache_read_input_tokens": 35}
            }
        }
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(line1) + "\n")
            f.write(json.dumps(line2) + "\n")
        
        with patch.dict(os.environ, {"RADAR_TELEMETRY_DIR": telemetry_dir, "CLAUDE_PROJECT_DIR": project_dir}):
            event = {
                "hook_event_name": "Stop",
                "transcript_path": transcript_path,
                "session_id": "sess-abc"
            }
            self.telemetry.log_claude_usage(event, project_dir)
        
        telemetry_file = os.path.join(telemetry_dir, "telemetry.jsonl")
        self.assertTrue(os.path.exists(telemetry_file))
        with open(telemetry_file, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f.read().strip().split("\n")]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertEqual(line["kind"], "claude_usage")
            self.assertEqual(line["session"], "sess-abc")
            self.assertIn("ts", line)
        self.assertEqual(lines[0]["model"], "claude-sonnet-5")
        self.assertEqual(lines[0]["input_tokens"], 10)
        self.assertEqual(lines[1]["model"], "claude-opus-4")
        self.assertEqual(lines[1]["input_tokens"], 5)

    def test_log_claude_usage_idempotent_deuxieme_appel_sans_nouveau_transcript(self):
        """Deuxième appel sans ajout au transcript -> aucune nouvelle ligne (idempotence)."""
        project_dir = self.tempdir.name
        telemetry_dir = os.path.join(project_dir, "telemetry_data")
        os.makedirs(telemetry_dir, exist_ok=True)
        transcript_path = os.path.join(project_dir, "transcript.jsonl")
        
        line = {
            "type": "assistant",
            "requestId": "req1",
            "message": {
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40}
            }
        }
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        
        with patch.dict(os.environ, {"RADAR_TELEMETRY_DIR": telemetry_dir, "CLAUDE_PROJECT_DIR": project_dir}):
            event = {
                "hook_event_name": "Stop",
                "transcript_path": transcript_path,
                "session_id": "sess-abc"
            }
            self.telemetry.log_claude_usage(event, project_dir)
            self.telemetry.log_claude_usage(event, project_dir)  # deuxième appel
        
        telemetry_file = os.path.join(telemetry_dir, "telemetry.jsonl")
        with open(telemetry_file, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f.read().strip().split("\n")]
        self.assertEqual(len(lines), 1)  # une seule ligne écrite

    def test_log_claude_usage_evenement_non_stop_ne_fait_rien(self):
        """hook_event_name autre que Stop/SessionEnd -> ne fait rien, pas de fichier d'état."""
        project_dir = self.tempdir.name
        telemetry_dir = os.path.join(project_dir, "telemetry_data")
        os.makedirs(telemetry_dir, exist_ok=True)
        transcript_path = os.path.join(project_dir, "transcript.jsonl")
        
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "requestId": "req1", "message": {"model": "m", "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}}}) + "\n")
        
        with patch.dict(os.environ, {"RADAR_TELEMETRY_DIR": telemetry_dir, "CLAUDE_PROJECT_DIR": project_dir}):
            event = {
                "hook_event_name": "PostToolUse",
                "transcript_path": transcript_path,
                "session_id": "sess-abc"
            }
            self.telemetry.log_claude_usage(event, project_dir)
        
        # Vérifie qu'aucun fichier d'état n'a été créé
        state_dir = os.path.join(telemetry_dir, ".claude_usage_state")
        self.assertFalse(os.path.exists(state_dir))
        # Vérifie qu'aucun telemetry.jsonl n'a été créé
        telemetry_file = os.path.join(telemetry_dir, "telemetry.jsonl")
        self.assertFalse(os.path.exists(telemetry_file))

    def test_log_claude_usage_sans_transcript_path_ou_session_id_ne_fait_rien(self):
        """Événement sans transcript_path ou sans session_id -> ne fait rien, pas d'exception."""
        project_dir = self.tempdir.name
        telemetry_dir = os.path.join(project_dir, "telemetry_data")
        os.makedirs(telemetry_dir, exist_ok=True)
        
        with patch.dict(os.environ, {"RADAR_TELEMETRY_DIR": telemetry_dir, "CLAUDE_PROJECT_DIR": project_dir}):
            # Sans transcript_path
            event1 = {"hook_event_name": "Stop", "session_id": "sess-abc"}
            self.telemetry.log_claude_usage(event1, project_dir)
            # Sans session_id
            event2 = {"hook_event_name": "Stop", "transcript_path": "/tmp/x.jsonl"}
            self.telemetry.log_claude_usage(event2, project_dir)
            # Les deux vides
            event3 = {"hook_event_name": "Stop", "transcript_path": "", "session_id": ""}
            self.telemetry.log_claude_usage(event3, project_dir)
        
        # Aucun fichier créé
        state_dir = os.path.join(telemetry_dir, ".claude_usage_state")
        self.assertFalse(os.path.exists(state_dir))
        telemetry_file = os.path.join(telemetry_dir, "telemetry.jsonl")
        self.assertFalse(os.path.exists(telemetry_file))


if __name__ == "__main__":
    unittest.main()
