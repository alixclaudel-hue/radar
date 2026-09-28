import contextlib
import importlib.util
import io
import json
import os
import tempfile
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
    """Tests unitaires pour la fonction main() du hook workflow_reminder.

    Chaque test tourne dans un `CLAUDE_PROJECT_DIR` temporaire : depuis C-2 le
    hook écrit un marqueur `.claude/current_request.json`, et on ne veut ni
    polluer un vrai checkout ni dépendre d'un marqueur préexistant."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._projet = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _executer_main(self, stdin_texte: str, env: dict | None = None) -> tuple[int, str]:
        stdin_io = io.StringIO(stdin_texte)
        stdout_io = io.StringIO()
        environnement = {"CLAUDE_PROJECT_DIR": self._projet}
        if env:
            environnement.update(env)
        with patch("sys.stdin", stdin_io), \
                patch.dict(os.environ, environnement, clear=True):
            with contextlib.redirect_stdout(stdout_io):
                code_retour = workflow_reminder.main()
        return code_retour, stdout_io.getvalue()

    def _marqueur_chemin(self) -> Path:
        return Path(self._projet) / ".claude" / "current_request.json"

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


class TestMarqueurRequete(unittest.TestCase):
    """C-2 : le hook écrit un marqueur `.claude/current_request.json` relu par
    `delegation_gate`, ce qui rend vérifiable le jugement « requête multi-étapes »."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._projet = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _marqueur_chemin(self) -> Path:
        return Path(self._projet) / ".claude" / "current_request.json"

    def _executer(self, payload: dict) -> int:
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self._projet}, clear=True):
            with contextlib.redirect_stdout(io.StringIO()):
                return workflow_reminder.main()

    def test_ecrit_le_marqueur_pour_requete_non_triviale(self):
        code = self._executer({"prompt": "implémente le cache de reco",
                               "session_id": "s-42"})
        self.assertEqual(code, 0)
        chemin = self._marqueur_chemin()
        self.assertTrue(chemin.is_file(), "le marqueur doit être écrit")
        data = json.loads(chemin.read_text(encoding="utf-8"))
        self.assertTrue(data["non_trivial"])
        self.assertEqual(data["session_id"], "s-42")
        self.assertEqual(data["prompt_chars"], len("implémente le cache de reco"))
        self.assertEqual(data["prompt_head"], "implémente le cache de reco")
        self.assertIn("ts", data)
        self.assertGreater(data["ts"], 0)

    def test_requete_simple_ecrase_le_marqueur_en_non_trivial_faux(self):
        # Une requête non triviale...
        self._executer({"prompt": "corrige le bug de cascade"})
        self.assertTrue(json.loads(
            self._marqueur_chemin().read_text(encoding="utf-8"))["non_trivial"])
        # ...puis une requête simple : le marqueur doit être écrasé, sinon le
        # gate croirait encore la requête en cours multi-étapes.
        self._executer({"prompt": "c'est quoi X ?"})
        self.assertFalse(json.loads(
            self._marqueur_chemin().read_text(encoding="utf-8"))["non_trivial"])

    def test_prompt_vide_n_ecrit_aucun_marqueur(self):
        self._executer({"prompt": "   "})
        self.assertFalse(self._marqueur_chemin().exists())

    def test_prompt_head_est_borne(self):
        long_prompt = "analyse " + ("x" * 500)
        self._executer({"prompt": long_prompt})
        data = json.loads(self._marqueur_chemin().read_text(encoding="utf-8"))
        self.assertEqual(len(data["prompt_head"]), workflow_reminder.PROMPT_HEAD)
        self.assertEqual(data["prompt_chars"], len(long_prompt))

    def test_chemin_partage_avec_le_gate(self):
        """Le chemin vient de `delegation_gate.current_request_path` : source
        unique, sinon l'écriture et la lecture finiraient par diverger."""
        gate = workflow_reminder._charger_gate()
        self.assertIsNotNone(gate, "le module voisin delegation_gate doit être chargeable")
        self.assertEqual(gate.current_request_path(self._projet),
                         self._marqueur_chemin())

    def test_le_gate_relit_le_marqueur(self):
        self._executer({"prompt": "implémente le cache de reco", "session_id": "s-7"})
        gate = workflow_reminder._charger_gate()
        marqueur = gate.load_current_request(self._projet)
        self.assertTrue(marqueur["non_trivial"])
        self.assertEqual(marqueur["session_id"], "s-7")

    def test_marquer_requete_sans_racine_projet_renvoie_none(self):
        """Sans racine de projet connue, on n'écrit rien et on ne lève pas."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(workflow_reminder.marquer_requete(
                {}, "implémente X", non_trivial=True, project_dir=""))

    def test_marquer_requete_ne_leve_jamais(self):
        with patch("os.replace", side_effect=OSError("disque plein")):
            with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self._projet}, clear=True):
                try:
                    resultat = workflow_reminder.marquer_requete(
                        {}, "implémente X", non_trivial=True)
                except Exception as e:  # noqa: BLE001 -- exactement ce qu'on vérifie
                    self.fail(f"marquer_requete a laissé fuir une exception : {e}")
        self.assertIsNone(resultat)


if __name__ == "__main__":
    unittest.main()
