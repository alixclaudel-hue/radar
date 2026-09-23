"""Tests unitaires pour scripts/ai_query.py (passerelle Gemini) -- zéro appel
réseau, urllib.request.urlopen toujours mocké.

Lancer : python3 -m unittest tests.test_ai_query -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.ai_query import (  # noqa: E402
    CODE_MODES,
    DEFAULT_MODEL,
    FALLBACK_STATUSES,
    SYSTEM_PROMPTS,
    TIER_CASCADES,
    GeminiHTTPError,
    _key_from_dotenv,
    extract_raw_code,
    is_fallback_status,
    main,
    query_gemini,
    query_with_fallback,
    resolve_tier,
)


def _fake_urlopen_cm(payload):
    """CM qui imite `with urllib.request.urlopen(...) as resp:` et renvoie `payload`."""
    cm = MagicMock()
    cm.read.return_value = json.dumps(payload).encode("utf-8")
    cm.__enter__.return_value = cm
    return cm


def _http_error(status, body=b"{}"):
    return urllib.error.HTTPError("http://x", status, "err", None, io.BytesIO(body))


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
    def test_sans_cle_aucune_exception_levee_avant_l_appel(self, mock_urlopen):
        """Garde-fou de non-régression : exiger GEMINI_API_KEY casserait la
        session cloud, qui n'en a jamais (authentification au niveau proxy)."""
        mock_urlopen.return_value = _fake_urlopen_cm(
            {"candidates": [{"content": {"parts": [{"text": "PONG"}]}}]})
        with patch.dict("os.environ", {}, clear=True):
            res, model, usage = query_with_fallback("Bonjour", tier="fast", api_key=None)
        self.assertEqual(res, "PONG")
        self.assertEqual(model, TIER_CASCADES["fast"][0])

    @patch("urllib.request.urlopen")
    def test_sans_cle_ni_identifiant_reseau_erreur_gemini_explicite(self, mock_urlopen):
        body = json.dumps(
            {"error": {"message": "API key not valid. Please pass a valid API key."}}
        ).encode("utf-8")
        mock_urlopen.side_effect = _http_error(400, body)
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                query_gemini("Bonjour", api_key=None)
        self.assertIn("API key not valid", str(ctx.exception))
        self.assertIn("identifiant réseau", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_indice_authentification_absent_sur_un_404(self, mock_urlopen):
        """Un modèle inconnu n'est pas un problème de clé : accoler l'indice
        d'authentification enverrait sur une fausse piste."""
        body = json.dumps({"error": {"message": "models/x is not found"}}).encode("utf-8")
        mock_urlopen.side_effect = _http_error(404, body)
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(GeminiHTTPError) as ctx:
                query_gemini("Bonjour", api_key=None, model="gemini-inexistant")
        self.assertNotIn("identifiant réseau", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_query_gemini_success(self, mock_urlopen):
        fake_response = {
            "candidates": [
                {"content": {"parts": [{"text": "Résultat analysé avec succès."}]}}
            ]
        }
        mock_urlopen.return_value = _fake_urlopen_cm(fake_response)

        res, usage = query_gemini("Analyse ces logs", api_key="fake-key")
        self.assertEqual(res, "Résultat analysé avec succès.")

        req = mock_urlopen.call_args[0][0]
        self.assertIn(DEFAULT_MODEL, req.full_url)
        self.assertIn("fake-key", req.full_url)

    @patch("urllib.request.urlopen")
    def test_empty_candidates_returns_empty_string(self, mock_urlopen):
        mock_urlopen.return_value = _fake_urlopen_cm({"candidates": []})
        res, usage = query_gemini("Test", api_key="fake-key")
        self.assertEqual(res, "")

    @patch("urllib.request.urlopen")
    def test_multi_part_response_concatenee_et_strippee(self, mock_urlopen):
        fake_response = {
            "candidates": [{"content": {"parts": [
                {"text": "  Bonjour "}, {"text": "le monde  "},
            ]}}]
        }
        mock_urlopen.return_value = _fake_urlopen_cm(fake_response)
        text, usage = query_gemini("x", api_key="fake-key")
        self.assertEqual(text, "Bonjour le monde")

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
        mock_urlopen.side_effect = _http_error(400, body)
        with self.assertRaises(RuntimeError) as ctx:
            query_gemini("x", api_key="fake-key")
        self.assertIn("400", str(ctx.exception))
        self.assertIn("clé API invalide", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_http_error_sans_corps_json(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(503, b"upstream down")
        with self.assertRaises(RuntimeError) as ctx:
            query_gemini("x", api_key="fake-key")
        self.assertIn("upstream down", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_http_error_expose_le_statut(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(429, b"quota")
        with self.assertRaises(GeminiHTTPError) as ctx:
            query_gemini("x", api_key="fake-key")
        self.assertEqual(ctx.exception.status, 429)

    @patch("urllib.request.urlopen")
    def test_url_error_reseau(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("nom de service inconnu")
        with self.assertRaises(RuntimeError) as ctx:
            query_gemini("x", api_key="fake-key")
        self.assertIn("nom de service inconnu", str(ctx.exception))


class CascadeTests(unittest.TestCase):
    """Repli d'un modèle sur le suivant : le cœur du workflow d'économie."""

    def setUp(self):
        # la cascade trace ses bascules sur stderr : utile en usage réel,
        # parasite au milieu de la sortie de la suite de tests
        patcheur = patch.object(sys, "stderr", io.StringIO())
        self.stderr = patcheur.start()
        self.addCleanup(patcheur.stop)

    def test_cascades_sans_doublon_interne(self):
        for tier, models in TIER_CASCADES.items():
            self.assertEqual(len(models), len(set(models)),
                             f"doublon dans la cascade '{tier}'")
            self.assertTrue(models, f"cascade '{tier}' vide")

    def test_default_model_est_la_tete_du_tier_heavy(self):
        self.assertEqual(DEFAULT_MODEL, TIER_CASCADES["heavy"][0])

    @patch("scripts.ai_query.query_gemini")
    def test_succes_du_premier_modele_sans_repli(self, mock_q):
        mock_q.return_value = ("Synthèse réussie", {})
        res, model, usage = query_with_fallback("Analyse logs", tier="fast", api_key="fake-key")
        self.assertEqual(res, "Synthèse réussie")
        self.assertEqual(model, TIER_CASCADES["fast"][0])
        self.assertEqual(mock_q.call_count, 1)

    @patch("scripts.ai_query.query_gemini")
    def test_repli_sur_429_quota_epuise(self, mock_q):
        mock_q.side_effect = [GeminiHTTPError(429, "quota épuisé"), ("Résultat secours", {})]
        res, model, usage = query_with_fallback("Test", tier="fast", api_key="fake-key")
        self.assertEqual(res, "Résultat secours")
        self.assertEqual(model, TIER_CASCADES["fast"][1])
        self.assertEqual(mock_q.call_count, 2)
        # la bascule doit rester visible : une dégradation silencieuse de
        # modèle fausserait l'interprétation d'une réponse de moindre qualité
        self.assertIn("429", self.stderr.getvalue())

    @patch("scripts.ai_query.query_gemini")
    def test_repli_sur_404_modele_retire(self, mock_q):
        """Un modèle renommé ou déprécié répond 404 (vérifié en réel sur
        `gemini-3-flash`) : la cascade doit l'enjamber, pas s'arrêter là."""
        mock_q.side_effect = [GeminiHTTPError(404, "model not found"), ("OK", {})]
        res, model, usage = query_with_fallback("Test", tier="heavy", api_key="fake-key")
        self.assertEqual(res, "OK")
        self.assertEqual(model, TIER_CASCADES["heavy"][1])

    @patch("scripts.ai_query.query_gemini")
    def test_pas_de_repli_sur_403_authentification(self, mock_q):
        """Une authentification refusée échouerait à l'identique sur les autres
        modèles : inutile de la masquer derrière trois appels de plus."""
        mock_q.side_effect = GeminiHTTPError(403, "permission denied")
        with self.assertRaises(GeminiHTTPError) as ctx:
            query_with_fallback("Test", tier="heavy", api_key="fake-key")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(mock_q.call_count, 1)

    @patch("scripts.ai_query.query_gemini")
    def test_cascade_entierement_epuisee_leve_une_erreur_explicite(self, mock_q):
        mock_q.side_effect = GeminiHTTPError(429, "quota épuisé")
        with self.assertRaises(RuntimeError) as ctx:
            query_with_fallback("Test", tier="fast", api_key="fake-key")
        self.assertIn("cascade 'fast'", str(ctx.exception))
        self.assertEqual(mock_q.call_count, len(TIER_CASCADES["fast"]))

    @patch("scripts.ai_query.query_gemini")
    def test_modele_explicite_court_circuite_la_cascade(self, mock_q):
        mock_q.return_value = ("ok", {})
        res, model, usage = query_with_fallback(
            "Test", tier="heavy", explicit_model="gemini-3.6-flash", api_key="fake-key")
        self.assertEqual(model, "gemini-3.6-flash")
        self.assertEqual(mock_q.call_args.kwargs["model"], "gemini-3.6-flash")

    @patch("scripts.ai_query.query_gemini")
    def test_modele_explicite_ne_replie_pas_sur_429(self, mock_q):
        """L'appelant a demandé CE modèle : lui en substituer un autre en
        silence fausserait toute mesure comparative."""
        mock_q.side_effect = GeminiHTTPError(429, "quota")
        with self.assertRaises(GeminiHTTPError):
            query_with_fallback("Test", explicit_model="gemini-3.6-flash", api_key="k")
        self.assertEqual(mock_q.call_count, 1)

    @patch("scripts.ai_query.query_gemini")
    def test_tier_inconnu_retombe_sur_fast(self, mock_q):
        mock_q.return_value = ("ok", {})
        _, model, _ = query_with_fallback("Test", tier="inexistant", api_key="fake-key")
        self.assertEqual(model, TIER_CASCADES["fast"][0])

    def test_statuts_de_repli_excluent_l_authentification(self):
        """Rejouer un refus d'authentification sur les modèles suivants ne ferait
        que retarder la même erreur, en la rendant moins lisible."""
        for status in (400, 401, 403):
            with self.subTest(status=status):
                self.assertFalse(is_fallback_status(status))

    def test_statuts_de_repli_couvrent_quota_modele_mort_et_5xx(self):
        for status in (404, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(is_fallback_status(status))

    @patch("scripts.ai_query.query_gemini")
    def test_repli_sur_502_aleas_amont(self, mock_q):
        """Observé en conditions réelles le 21/09/2026 : la tête de la cascade
        heavy a répondu 502 alors que les autres modèles répondaient."""
        mock_q.side_effect = [GeminiHTTPError(502, "upstream request failed"), ("OK", {})]
        res, model, usage = query_with_fallback("Test", tier="heavy", api_key="fake-key")
        self.assertEqual(res, "OK")
        self.assertEqual(model, TIER_CASCADES["heavy"][1])

    @patch("scripts.ai_query.query_gemini")
    def test_repli_sur_panne_reseau(self, mock_q):
        """Une panne réseau (`URLError`, remontée en `RuntimeError` nu par
        `query_gemini`, pas en `GeminiHTTPError`) doit aussi faire basculer
        sur le modèle suivant avec la même requête — avant ce correctif elle
        échappait au `except GeminiHTTPError` et arrêtait toute la cascade."""
        mock_q.side_effect = [
            RuntimeError("Erreur réseau Gemini : timeout"),
            ("OK", {}),
        ]
        res, model, usage = query_with_fallback("Test", tier="fast", api_key="fake-key")
        self.assertEqual(res, "OK")
        self.assertEqual(model, TIER_CASCADES["fast"][1])
        self.assertEqual(mock_q.call_count, 2)

    @patch("scripts.ai_query.query_gemini")
    def test_usage_fallbacks_vaut_zero_si_premier_modele_repond(self, mock_q):
        mock_q.return_value = ("ok", {})
        _, _, usage = query_with_fallback("Test", tier="fast", api_key="fake-key")
        self.assertEqual(usage["fallbacks"], 0)

    @patch("scripts.ai_query.query_gemini")
    def test_usage_fallbacks_vaut_deux_apres_deux_echecs_429(self, mock_q):
        mock_q.side_effect = [
            GeminiHTTPError(429, "quota"),
            GeminiHTTPError(429, "quota"),
            ("ok", {})
        ]
        _, model, usage = query_with_fallback("Test", tier="heavy", api_key="fake-key")
        self.assertEqual(usage["fallbacks"], 2)
        self.assertEqual(model, TIER_CASCADES["heavy"][2])


class TierRoutingTests(unittest.TestCase):

    def test_code_et_test_partent_en_heavy(self):
        for mode in ("code", "test"):
            self.assertEqual(resolve_tier(mode), "heavy")

    def test_les_autres_modes_partent_en_fast(self):
        for mode in ("general", "diag", "summary", "pr"):
            self.assertEqual(resolve_tier(mode), "fast")

    def test_tier_explicite_prime_sur_le_mode(self):
        self.assertEqual(resolve_tier("code", "fast"), "fast")
        self.assertEqual(resolve_tier("diag", "heavy"), "heavy")

    def test_tous_les_modes_ont_un_prompt_systeme(self):
        for mode in ("general", "code", "test", "diag", "summary", "pr"):
            self.assertIn(mode, SYSTEM_PROMPTS)

    def test_mode_pr_reclame_les_quatre_sections(self):
        prompt = SYSTEM_PROMPTS["pr"]
        for section in ("TITRE COMMIT", "MESSAGE COMMIT", "CORPS DE PR", "MAJ CLAUDE.md"):
            self.assertIn(section, prompt)

    def test_mode_pr_interdit_le_format_conventional_commits(self):
        """Le dépôt commite en phrases françaises à l'impératif, pas en
        `feat(scope):` -- le prompt doit refléter la convention réelle."""
        self.assertIn("conventional-commits", SYSTEM_PROMPTS["pr"])

    def test_code_modes_couvre_code_et_test(self):
        self.assertEqual(CODE_MODES, frozenset({"code", "test"}))


class ExtractRawCodeTests(unittest.TestCase):

    def test_bloc_markdown_python(self):
        sample = "Exemple :\n```python\ndef test_fn():\n    return True\n```\nFin."
        self.assertEqual(extract_raw_code(sample), "def test_fn():\n    return True")

    def test_texte_sans_bloc_renvoye_tel_quel(self):
        sample = "def test_fn():\n    return True"
        self.assertEqual(extract_raw_code(sample), "def test_fn():\n    return True")

    def test_balise_de_langage_quelconque(self):
        """Un modèle répond parfois ```py ou ```bash : retomber sur le texte
        brut laisserait les clôtures markdown dans le fichier écrit."""
        for tag in ("py", "bash", "Python", ""):
            with self.subTest(tag=tag):
                sample = f"```{tag}\nx = 1\n```"
                self.assertEqual(extract_raw_code(sample), "x = 1")

    def test_plusieurs_blocs_concatenes(self):
        sample = "```python\nimport os\n```\nPuis :\n```python\nprint(os)\n```"
        self.assertEqual(extract_raw_code(sample), "import os\n\nprint(os)")

    def test_chaine_vide(self):
        self.assertEqual(extract_raw_code(""), "")

    def test_blocs_python_prioritaires_sur_les_autres_langages(self):
        sample = "```python\nimport os\n```\nPuis :\n```bash\npytest -q\n```"
        self.assertEqual(extract_raw_code(sample), "import os")

    def test_blocs_non_python_gardes_si_aucun_bloc_python(self):
        """La balise est souvent omise : sans bloc Python identifié, le contenu
        reste la meilleure réponse disponible."""
        self.assertEqual(extract_raw_code("```\nx = 1\n```"), "x = 1")

    def test_backticks_cites_en_prose_ne_faussent_pas_la_capture(self):
        """Une clôture n'en est une que si elle ouvre sa ligne, sinon la capture
        part de travers et renvoie une chaîne vide au lieu du vrai code."""
        sample = "Entoure ta réponse de ``` et ```.\n```python\nx = 1\n```"
        self.assertEqual(extract_raw_code(sample), "x = 1")

    def test_plusieurs_blocs_python_concatenes(self):
        sample = "```py\nimport os\n```\nPuis :\n```python\nprint(os)\n```"
        self.assertEqual(extract_raw_code(sample), "import os\n\nprint(os)")


class AuthHintTests(unittest.TestCase):
    """L'indice « aucune clé locale » ne doit sortir que sur un vrai refus
    d'authentification : en session cloud la clé est TOUJOURS absente du code,
    donc le seul critère `key is None` le collerait à n'importe quel 400."""

    @patch("urllib.request.urlopen")
    def _erreur(self, status, message, mock_urlopen):
        body = json.dumps({"error": {"message": message}}).encode("utf-8")
        mock_urlopen.side_effect = _http_error(status, body)
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(GeminiHTTPError) as ctx:
                query_gemini("x", api_key=None)
        return str(ctx.exception)

    def test_400_sur_requete_trop_grosse_sans_indice_trompeur(self):
        self.assertNotIn("identifiant réseau",
                         self._erreur(400, "Request payload size exceeds the limit"))

    def test_400_sur_cle_invalide_avec_indice(self):
        self.assertIn("identifiant réseau",
                      self._erreur(400, "API key not valid. Please pass a valid API key."))

    def test_403_avec_indice(self):
        self.assertIn("identifiant réseau", self._erreur(403, "permission denied"))

    @patch("urllib.request.urlopen")
    def test_aucun_indice_quand_une_cle_locale_est_fournie(self, mock_urlopen):
        body = json.dumps({"error": {"message": "API key not valid"}}).encode("utf-8")
        mock_urlopen.side_effect = _http_error(400, body)
        with self.assertRaises(GeminiHTTPError) as ctx:
            query_gemini("x", api_key="fake-key")
        self.assertNotIn("identifiant réseau", str(ctx.exception))


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

    @patch("scripts.ai_query.query_gemini", return_value=("ok", {}))
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

    @patch("scripts.ai_query.query_gemini", return_value=("ok", {}))
    def test_fichier_ajoute_au_prompt(self, mock_query):
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

    @patch("scripts.ai_query.query_gemini", return_value=("ok", {}))
    def test_option_file_repetable(self, mock_query):
        paths = []
        try:
            for contenu in ("PREMIER", "SECOND"):
                with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
                    f.write(contenu)
                    paths.append(f.name)
            argv = ["Analyse"]
            for p in paths:
                argv += ["-f", p]
            code, out, err = self._run(argv)
        finally:
            for p in paths:
                os.remove(p)
        self.assertEqual(code, 0)
        prompt = mock_query.call_args.kwargs["prompt"]
        self.assertIn("PREMIER", prompt)
        self.assertIn("SECOND", prompt)

    @patch("scripts.ai_query.query_gemini", return_value=("ok", {}))
    def test_stdin_flag_force_la_lecture(self, mock_query):
        code, out, err = self._run(["Analyse", "--stdin"], stdin_text="ligne de log",
                                    stdin_isatty=False)
        self.assertEqual(code, 0)
        self.assertIn("ligne de log", mock_query.call_args.kwargs["prompt"])

    @patch("scripts.ai_query.query_gemini", return_value=("ok", {}))
    def test_stdin_lue_automatiquement_si_pipe_sans_prompt_ni_fichier(self, mock_query):
        code, out, err = self._run([], stdin_text="contenu piped", stdin_isatty=False)
        self.assertEqual(code, 0)
        self.assertIn("contenu piped", mock_query.call_args.kwargs["prompt"])

    @patch("scripts.ai_query.query_gemini", side_effect=RuntimeError("Erreur API Gemini (429) : quota"))
    def test_erreur_query_gemini_retourne_1(self, mock_query):
        code, out, err = self._run(["x"])
        self.assertEqual(code, 1)
        self.assertIn("quota", err)

    @patch("scripts.ai_query.query_gemini", return_value=("ok", {}))
    def test_mode_diag_utilise_le_prompt_systeme_dedie(self, mock_query):
        code, out, err = self._run(["Analyse", "--mode", "diag"])
        self.assertEqual(code, 0)
        self.assertIn("CAUSE RACINE", mock_query.call_args.kwargs["system_instruction"])

    @patch("scripts.ai_query.query_gemini", return_value=("ok", {}))
    def test_mode_code_part_sur_la_cascade_heavy(self, mock_query):
        code, out, err = self._run(["Écris une fonction", "--mode", "code"])
        self.assertEqual(code, 0)
        self.assertEqual(mock_query.call_args.kwargs["model"], TIER_CASCADES["heavy"][0])

    @patch("scripts.ai_query.query_gemini", return_value=("ok", {}))
    def test_mode_diag_part_sur_la_cascade_fast(self, mock_query):
        code, out, err = self._run(["Analyse", "--mode", "diag"])
        self.assertEqual(mock_query.call_args.kwargs["model"], TIER_CASCADES["fast"][0])

    @patch("scripts.ai_query.query_gemini", return_value=("ok", {}))
    def test_option_system_remplace_le_prompt_du_mode(self, mock_query):
        code, out, err = self._run(["x", "--mode", "diag", "-s", "Sur mesure."])
        self.assertEqual(mock_query.call_args.kwargs["system_instruction"], "Sur mesure.")

    @patch("scripts.ai_query.query_gemini", return_value=("Voici :\n```python\nx = 1\n```", {}))
    def test_mode_code_extrait_le_bloc_sans_option(self, mock_query):
        code, out, err = self._run(["Écris", "--mode", "code"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "x = 1")

    @patch("scripts.ai_query.query_gemini", return_value=("```python\nx = 1\n```", {}))
    def test_raw_code_hors_mode_code(self, mock_query):
        code, out, err = self._run(["x", "--raw-code"])
        self.assertEqual(out.strip(), "x = 1")

    @patch("scripts.ai_query.query_gemini", return_value=("```python\ndef f(:\n```", {}))
    def test_check_syntax_refuse_un_code_casse(self, mock_query):
        code, out, err = self._run(["x", "--mode", "code", "--check-syntax"])
        self.assertEqual(code, 3)
        self.assertIn("ne compile pas", err)

    @patch("scripts.ai_query.query_gemini", return_value=("```python\ndef f():\n    return 1\n```", {}))
    def test_check_syntax_laisse_passer_un_code_valide(self, mock_query):
        code, out, err = self._run(["x", "--mode", "code", "--check-syntax"])
        self.assertEqual(code, 0)

    @patch("scripts.ai_query.query_gemini", return_value=("```python\nx = 1\n```", {}))
    def test_output_ecrit_le_fichier(self, mock_query):
        path = os.path.join(tempfile.mkdtemp(), "genere.py")
        code, out, err = self._run(["x", "--mode", "code", "-o", path])
        try:
            self.assertEqual(code, 0)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), "x = 1")
        finally:
            os.remove(path)

    @patch("scripts.ai_query.query_gemini", return_value=("", {}))
    def test_output_refuse_d_ecraser_avec_une_reponse_vide(self, mock_query):
        """Écraser un fichier existant par du néant est le pire échec
        silencieux possible pour un `-o` : mieux vaut un code de retour 1."""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write("# contenu précieux\n")
            path = f.name
        try:
            code, out, err = self._run(["x", "--mode", "code", "-o", path])
            self.assertEqual(code, 1)
            with open(path, encoding="utf-8") as f:
                self.assertIn("précieux", f.read())
        finally:
            os.remove(path)

    @patch("scripts.ai_query.query_gemini", return_value=("", {}))
    def test_reponse_vide_echoue_aussi_sans_output(self, mock_query):
        """Une redirection shell `> fichier.py` est équivalente à `-o` du point
        de vue appelant : sortir en 0 avec stdout vide y détruirait le fichier."""
        code, out, err = self._run(["x", "--mode", "code"])
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "")
        self.assertIn("réponse vide", err)

    @patch("scripts.ai_query.query_gemini",
           return_value=("```python\nimport os\n```\nLancer :\n```bash\npytest -q\n```", {}))
    def test_mode_code_ignore_le_bloc_bash_accompagnateur(self, mock_query):
        """Cas le plus courant de la recette n°2 du SKILL : le modèle ajoute un
        bloc shell « pour lancer les tests ». Le concaténer casse le fichier."""
        code, out, err = self._run(["x", "--mode", "code", "--check-syntax"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "import os")

    @patch("scripts.ai_query.query_gemini", return_value=("```python\ndef f(:\n```", {}))
    def test_check_syntax_sort_en_3_pas_en_2(self, mock_query):
        """argparse réserve déjà le 2 aux erreurs de ligne de commande."""
        code, out, err = self._run(["x", "--mode", "code", "--check-syntax"])
        self.assertEqual(code, 3)


class KeyFromDotenvTests(unittest.TestCase):
    """Repli de lecture de GEMINI_API_KEY dans le .env (lancement CLI hôte)."""

    def _dotenv(self, contenu: str) -> str:
        tmp = tempfile.NamedTemporaryFile(
            mode="w+", delete=False, encoding="utf-8"
        )
        tmp.write(contenu)
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.remove(tmp.name))
        return tmp.name

    def test_cle_simple(self):
        self.assertEqual(_key_from_dotenv(self._dotenv("GEMINI_API_KEY=abc123")), "abc123")

    def test_guillemets_doubles(self):
        self.assertEqual(_key_from_dotenv(self._dotenv('GEMINI_API_KEY="abc123"')), "abc123")

    def test_prefixe_export(self):
        self.assertEqual(_key_from_dotenv(self._dotenv("export GEMINI_API_KEY=abc123")), "abc123")

    def test_commentaires_et_lignes_vides(self):
        contenu = "# a\n\n# b\nGEMINI_API_KEY=abc123\n\n"
        self.assertEqual(_key_from_dotenv(self._dotenv(contenu)), "abc123")

    def test_autres_cles_ignorees(self):
        contenu = "OTHER=12345\nFOO=bar\nGEMINI_API_KEY=abc123\nANOTHER=xyz"
        self.assertEqual(_key_from_dotenv(self._dotenv(contenu)), "abc123")

    def test_cle_absente(self):
        self.assertIsNone(_key_from_dotenv(self._dotenv("OTHER=12345\nFOO=bar\n")))

    def test_valeur_vide(self):
        self.assertIsNone(_key_from_dotenv(self._dotenv("GEMINI_API_KEY=")))

    def test_fichier_inexistant(self):
        self.assertIsNone(_key_from_dotenv("/tmp/nexiste_pas_9999.env"))


if __name__ == "__main__":
    unittest.main()
