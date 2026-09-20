"""Tests unitaires pour scripts/ai_query.py (passerelle Gemini) -- zéro appel
réseau, urllib.request.urlopen toujours mocké.

Lancer : python3 -m unittest tests.test_ai_query -v
"""
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.ai_query import DEFAULT_MODEL, main, query_gemini  # noqa: E402


def _fake_urlopen_cm(payload):
    """CM qui imite `with urllib.request.urlopen(...) as resp:` et renvoie `payload`."""
    cm = MagicMock()
    cm.read.return_value = json.dumps(payload).encode("utf-8")
    cm.__enter__.return_value = cm
    return cm


class QueryGeminiTests(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_sans_cle_locale_url_ne_contient_pas_key(self, mock_urlopen):
        """Sans clé, la requête part quand même (sans `?key=`) : c'est la
        session cloud (proxy réseau + identifiant `x-goog-api-key` configuré
        côté environnement) qui s'authentifie au niveau transport, invisible
        d'ici -- cf. `.claude/skills/ask-gemini/SKILL.md`."""
        mock_urlopen.return_value = _fake_urlopen_cm({"candidates": []})
        with patch.dict("os.environ", {}, clear=True):
            query_gemini("Bonjour", api_key=None)
        req = mock_urlopen.call_args[0][0]
        self.assertNotIn("key=", req.full_url)
        self.assertTrue(req.full_url.endswith(":generateContent"))

    @patch("urllib.request.urlopen")
    def test_sans_cle_ni_identifiant_reseau_erreur_gemini_explicite(self, mock_urlopen):
        body = json.dumps(
            {"error": {"message": "API key not valid. Please pass a valid API key."}}
        ).encode("utf-8")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://x", 400, "Bad Request", None, io.BytesIO(body))
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                query_gemini("Bonjour", api_key=None)
        self.assertIn("API key not valid", str(ctx.exception))
        self.assertIn("identifiant réseau", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_query_gemini_success(self, mock_urlopen):
        fake_response = {
            "candidates": [
                {"content": {"parts": [{"text": "Résultat analysé avec succès."}]}}
            ]
        }
        mock_urlopen.return_value = _fake_urlopen_cm(fake_response)

        res = query_gemini("Analyse ces logs", api_key="fake-key")
        self.assertEqual(res, "Résultat analysé avec succès.")

        req = mock_urlopen.call_args[0][0]
        self.assertIn(DEFAULT_MODEL, req.full_url)
        self.assertIn("fake-key", req.full_url)

    @patch("urllib.request.urlopen")
    def test_empty_candidates_returns_empty_string(self, mock_urlopen):
        mock_urlopen.return_value = _fake_urlopen_cm({"candidates": []})
        res = query_gemini("Test", api_key="fake-key")
        self.assertEqual(res, "")

    @patch("urllib.request.urlopen")
    def test_multi_part_response_concatenee_et_strippee(self, mock_urlopen):
        fake_response = {
            "candidates": [{"content": {"parts": [
                {"text": "  Bonjour "}, {"text": "le monde  "},
            ]}}]
        }
        mock_urlopen.return_value = _fake_urlopen_cm(fake_response)
        self.assertEqual(query_gemini("x", api_key="fake-key"), "Bonjour le monde")

    @patch("urllib.request.urlopen")
    def test_system_instruction_ajoutee_au_payload(self, mock_urlopen):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _fake_urlopen_cm({"candidates": []})

        mock_urlopen.side_effect = fake_urlopen
        query_gemini("Question", system_instruction="Réponds en français.", api_key="fake-key")
        self.assertEqual(
            captured["body"]["systemInstruction"]["parts"][0]["text"],
            "Réponds en français.",
        )

    @patch("urllib.request.urlopen")
    def test_system_instruction_absente_si_vide(self, mock_urlopen):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _fake_urlopen_cm({"candidates": []})

        mock_urlopen.side_effect = fake_urlopen
        query_gemini("Question", api_key="fake-key")
        self.assertNotIn("systemInstruction", captured["body"])

    @patch("urllib.request.urlopen")
    def test_http_error_avec_message_json(self, mock_urlopen):
        body = json.dumps({"error": {"message": "clé API invalide"}}).encode("utf-8")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://x", 400, "Bad Request", None, io.BytesIO(body))
        with self.assertRaises(RuntimeError) as ctx:
            query_gemini("x", api_key="fake-key")
        self.assertIn("400", str(ctx.exception))
        self.assertIn("clé API invalide", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_http_error_sans_corps_json(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://x", 503, "Service Unavailable", None, io.BytesIO(b"upstream down"))
        with self.assertRaises(RuntimeError) as ctx:
            query_gemini("x", api_key="fake-key")
        self.assertIn("upstream down", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_url_error_reseau(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("nom de service inconnu")
        with self.assertRaises(RuntimeError) as ctx:
            query_gemini("x", api_key="fake-key")
        self.assertIn("nom de service inconnu", str(ctx.exception))


class MainTests(unittest.TestCase):
    """`main()` orchestre CLI/fichier/stdin -- `query_gemini` toujours mocké,
    aucun de ces tests ne doit pouvoir toucher le réseau."""

    def _run(self, argv, stdin_text=None, stdin_isatty=True):
        stdin = io.StringIO(stdin_text or "")
        stdin.isatty = lambda: stdin_isatty
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["ai_query.py"] + argv), \
                patch.object(sys, "stdin", stdin), \
                patch.object(sys, "stdout", stdout), \
                patch.object(sys, "stderr", stderr):
            code = main()
        return code, stdout.getvalue(), stderr.getvalue()

    @patch("scripts.ai_query.query_gemini", return_value="ok")
    def test_prompt_cli_simple(self, mock_query):
        code, out, err = self._run(["Résume ceci"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "ok")
        self.assertEqual(mock_query.call_args.kwargs["prompt"], "Résume ceci")

    def test_aucune_entree_retourne_1(self):
        code, out, err = self._run([], stdin_isatty=True)
        self.assertEqual(code, 1)
        self.assertIn("aucun texte fourni", err)

    def test_fichier_introuvable_retourne_1(self):
        code, out, err = self._run(["x", "--file", "/inexistant/x.log"])
        self.assertEqual(code, 1)
        self.assertIn("introuvable", err)

    @patch("scripts.ai_query.query_gemini", return_value="ok")
    def test_fichier_ajoute_au_prompt(self, mock_query):
        import tempfile
        tmp_content = "ERROR boom"
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(tmp_content)
            path = f.name
        try:
            code, out, err = self._run(["Analyse", "--file", path])
        finally:
            os.remove(path)
        self.assertEqual(code, 0)
        self.assertIn(tmp_content, mock_query.call_args.kwargs["prompt"])

    @patch("scripts.ai_query.query_gemini", return_value="ok")
    def test_stdin_flag_force_la_lecture(self, mock_query):
        code, out, err = self._run(["Analyse", "--stdin"], stdin_text="ligne de log",
                                    stdin_isatty=False)
        self.assertEqual(code, 0)
        self.assertIn("ligne de log", mock_query.call_args.kwargs["prompt"])

    @patch("scripts.ai_query.query_gemini", return_value="ok")
    def test_stdin_lue_automatiquement_si_pipe_sans_prompt_ni_fichier(self, mock_query):
        code, out, err = self._run([], stdin_text="contenu piped", stdin_isatty=False)
        self.assertEqual(code, 0)
        self.assertIn("contenu piped", mock_query.call_args.kwargs["prompt"])

    @patch("scripts.ai_query.query_gemini", side_effect=RuntimeError("Erreur API Gemini (429) : quota"))
    def test_erreur_query_gemini_retourne_1(self, mock_query):
        code, out, err = self._run(["x"])
        self.assertEqual(code, 1)
        self.assertIn("quota", err)


if __name__ == "__main__":
    unittest.main()
