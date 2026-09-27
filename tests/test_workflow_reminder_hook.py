import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

HOOK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "workflow_reminder.py"

spec = importlib.util.spec_from_file_location("workflow_reminder", HOOK_PATH)
if spec is None or spec.loader is None:
    raise ImportError(f"Impossible de charger le module depuis {HOOK_PATH}")
workflow_reminder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow_reminder)


class TestWorkflowReminderMain(unittest.TestCase):
    """Tests unitaires pour la fonction main() du hook workflow_reminder."""

    def _executer_main(self, stdin_texte: str) -> tuple[int, str]:
        stdin_io = io.StringIO(stdin_texte)
        stdout_io = io.StringIO()
        with patch("sys.stdin", stdin_io):
            with contextlib.redirect_stdout(stdout_io):
                code_retour = workflow_reminder.main()
        return code_retour, stdout_io.getvalue()

    def test_prompt_simple_question_aucun_rappel(self):
        entree = json.dumps({"prompt": "c'est quoi le mode context ?"})
        code, sortie = self._executer_main(entree)
        self.assertEqual(code, 0)
        self.assertEqual(sortie, "")

    def test_prompt_avec_verbe_action_declenche_rappel(self):
        entree = json.dumps({"prompt": "corrige le bug de cascade"})
        code, sortie = self._executer_main(entree)
        self.assertEqual(code, 0)
        self.assertIn("workflow", sortie.lower())

    def test_prompt_long_sans_verbe_action_declenche_rappel(self):
        texte_long = "Observation passive de l'état global du système distribué sans action spécifique immédiate."
        self.assertGreater(len(texte_long), 60)
        entree = json.dumps({"prompt": texte_long})
        code, sortie = self._executer_main(entree)
        self.assertEqual(code, 0)
        self.assertIn("workflow", sortie.lower())

    def test_question_longue_sans_action_aucun_rappel(self):
        question_longue = "Pourquoi le système continue-t-il de répercuter les erreurs même après redémarrage complet ?"
        self.assertGreater(len(question_longue), 60)
        code, sortie = self._executer_main(json.dumps({"prompt": question_longue}))
        self.assertEqual(code, 0)
        self.assertEqual(sortie, "")

    def test_stdin_vide_retour_zero_sans_sortie(self):
        code, sortie = self._executer_main("")
        self.assertEqual(code, 0)
        self.assertEqual(sortie, "")

    def test_stdin_espaces_seuls_retour_zero_sans_sortie(self):
        code, sortie = self._executer_main("   \n  ")
        self.assertEqual(code, 0)
        self.assertEqual(sortie, "")

    def test_stdin_json_invalide_retour_zero_sans_sortie(self):
        for flux_invalide in ("ceci n'est pas un json valide", "{ceci n'est pas un json valide: 123"):
            with self.subTest(entree=flux_invalide):
                code, sortie = self._executer_main(flux_invalide)
                self.assertEqual(code, 0)
                self.assertEqual(sortie, "")

    def test_stdin_payload_non_dictionnaire_retour_zero(self):
        for payload in (["corrige", "le", "bug"], 0):
            with self.subTest(payload=payload):
                code, sortie = self._executer_main(json.dumps(payload))
                self.assertEqual(code, 0)
                self.assertEqual(sortie, "")

    def test_prompt_absent_ou_none_retour_zero(self):
        code, sortie = self._executer_main(json.dumps({"autre_cle": "valeur"}))
        self.assertEqual(code, 0)
        self.assertEqual(sortie, "")

        code, sortie = self._executer_main(json.dumps({"prompt": None}))
        self.assertEqual(code, 0)
        self.assertEqual(sortie, "")

    def test_prompt_chaine_vide_ou_espaces_retour_zero(self):
        for prompt in ("", "   ", "\t \n "):
            with self.subTest(prompt=repr(prompt)):
                code, sortie = self._executer_main(json.dumps({"prompt": prompt}))
                self.assertEqual(code, 0)
                self.assertEqual(sortie, "")

    def test_prompt_non_chaine_retour_zero(self):
        code, sortie = self._executer_main(json.dumps({"prompt": 12345}))
        self.assertEqual(code, 0)
        self.assertEqual(sortie, "")


if __name__ == "__main__":
    unittest.main()
