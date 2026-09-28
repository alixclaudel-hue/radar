"""Tests unitaires pour le garde-fou de délégation (`delegation_gate.py`).

Ce garde-fou impose la délégation préalable à un modèle externe pour certaines
actions critiques (commits git, premier jet de code ou de tests). Sa justesse
conditionne toute la politique de délégation du projet : s'il est trop permissif,
des actions non révisées passent en production ; s'il est trop strict ou
erratique, le flux de travail de Claude est paralysé.

Depuis le correctif C-4, le SEUL relais conforme est `ai_broker.py` : un appel
direct à `ai_query.py` écrit un reçu `via="direct"` qui ne satisfait plus le
gate. Le module est chargé directement depuis `scripts/hooks/delegation_gate.py`
(l'alias mort `gemini_gate.py` a été supprimé — correctif C-7).
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Chargement dynamique du module scripts/hooks/delegation_gate.py
_RACINE_PROJET = Path(__file__).resolve().parent.parent
_CHEMIN_MODULE = _RACINE_PROJET / "scripts" / "hooks" / "delegation_gate.py"

_SPEC = importlib.util.spec_from_file_location("gemini_gate", _CHEMIN_MODULE)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Impossible de charger le module depuis {_CHEMIN_MODULE}")
gemini_gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gemini_gate)


class TestGardeFouGemini(unittest.TestCase):
    """Suite de validations unitaires pour les fonctions de gemini_gate."""

    def test_mode_requis_commit_reel(self):
        """Cas 1 : required_modes renvoie ('pr',) pour une vraie commande git commit."""
        commande = "git commit -m 'feat: ajout du filtre'"
        modes = gemini_gate.required_modes("Bash", {"command": commande})
        self.assertEqual(modes, ("pr",))

    def test_mode_requis_commit_entre_guillemets_ignore(self):
        """Cas 2 : required_modes renvoie None si 'git commit' est uniquement cité entre quotes."""
        commande = "echo 'git commit -m test' && echo \"git commit\""
        modes = gemini_gate.required_modes("Bash", {"command": commande})
        self.assertIsNone(modes)

    def test_mode_requis_ecriture_fichier_test(self):
        """Cas 3 : required_modes renvoie ('test', 'code') pour l'écriture d'un fichier de test Python."""
        modes = gemini_gate.required_modes("Write", {"file_path": "tests/test_truc.py"})
        self.assertEqual(modes, ("test", "code"))

    def test_mode_requis_ecriture_fichier_non_python(self):
        """Cas 4 : required_modes renvoie None pour l'écriture d'un fichier non-.py (ex: markdown)."""
        modes = gemini_gate.required_modes("Write", {"file_path": "docs/note.md"})
        self.assertIsNone(modes)

    def test_mode_requis_edition_module_existant(self):
        """Cas 5 : required_modes renvoie None pour un Edit sur un fichier Python existant."""
        with tempfile.TemporaryDirectory() as rep_temporaire:
            fichier_existant = Path(rep_temporaire) / "module_existant.py"
            fichier_existant.write_text("# code existant\n", encoding="utf-8")

            modes = gemini_gate.required_modes(
                "Edit", {"file_path": str(fichier_existant)}
            )
            self.assertIsNone(modes)

    def test_mode_requis_outil_inconnu(self):
        """Cas 6 : required_modes renvoie None si l'outil invoqué n'est pas surveillé."""
        modes = gemini_gate.required_modes("InspectCode", {"file_path": "scripts/run.py"})
        self.assertIsNone(modes)

    def test_chargement_recus_fichier_inexistant(self):
        """Cas 7 : load_receipts renvoie une liste vide quand le fichier n'existe pas."""
        with tempfile.TemporaryDirectory() as rep_temporaire:
            chemin_inexistant = Path(rep_temporaire) / "introuvable.jsonl"
            recus = gemini_gate.load_receipts(chemin_inexistant)
            self.assertEqual(recus, [])

    def test_chargement_recus_ignore_lignes_corrompues_et_vides(self):
        """Cas 8 : load_receipts ignore les lignes invalides ou vides mais extrait les JSON valides."""
        with tempfile.TemporaryDirectory() as rep_temporaire:
            chemin_jsonl = Path(rep_temporaire) / "recus.jsonl"
            contenu = (
                "\n"
                '{"mode": "pr", "ts": 1000.0, "status": "ok"}\n'
                "   \n"
                "CECI_N_EST_PAS_DU_JSON\n"
                '{"mode": "test", "ts": 2000.0, "status": "error"}\n'
                "{invalide: json}\n"
            )
            chemin_jsonl.write_text(contenu, encoding="utf-8")

            recus = gemini_gate.load_receipts(chemin_jsonl)
            attendus = [
                {"mode": "pr", "ts": 1000.0, "status": "ok"},
                {"mode": "test", "ts": 2000.0, "status": "error"},
            ]
            self.assertEqual(recus, attendus)

    def test_recu_recent_valide_dans_ttl(self):
        """Cas 9 : has_recent_receipt renvoie True pour un reçu valide du bon mode dans le TTL."""
        maintenant = 10000.0
        ttl = 3600.0
        recus = [{"mode": "pr", "ts": maintenant - 500.0, "status": "ok"}]
        resultat = gemini_gate.has_recent_receipt(recus, ("pr",), maintenant, ttl)
        self.assertTrue(resultat)

    def test_recu_recent_expire_hors_ttl(self):
        """Cas 10 : has_recent_receipt renvoie False pour un reçu du bon mode plus ancien que le TTL."""
        maintenant = 10000.0
        ttl = 3600.0
        recus = [{"mode": "pr", "ts": maintenant - 4000.0, "status": "ok"}]
        resultat = gemini_gate.has_recent_receipt(recus, ("pr",), maintenant, ttl)
        self.assertFalse(resultat)

    def test_recu_recent_autre_mode(self):
        """Cas 11 : has_recent_receipt renvoie False pour un reçu récent mais dont le mode ne correspond pas."""
        maintenant = 10000.0
        ttl = 3600.0
        recus = [{"mode": "test", "ts": maintenant - 100.0, "status": "ok"}]
        resultat = gemini_gate.has_recent_receipt(recus, ("pr",), maintenant, ttl)
        self.assertFalse(resultat)

    def test_recu_recent_avec_statut_erreur(self):
        """Cas 12 : has_recent_receipt renvoie True pour un reçu avec statut 'error' (délégation tentée)."""
        maintenant = 10000.0
        ttl = 3600.0
        recus = [{"mode": "code", "ts": maintenant - 200.0, "status": "error"}]
        resultat = gemini_gate.has_recent_receipt(recus, ("code", "test"), maintenant, ttl)
        self.assertTrue(resultat)


    # --- Tests F1 : seuil min_chars ---

    def test_recu_ok_sous_seuil_min_chars_rejete(self):
        """Un reçu ok récent avec prompt_chars < min_chars est ignoré."""
        maintenant = 10000.0
        recus = [{"mode": "code", "ts": maintenant - 100.0, "status": "ok", "prompt_chars": 5}]
        resultat = gemini_gate.has_recent_receipt(recus, ("code",), maintenant, 3600.0, min_chars=50)
        self.assertFalse(resultat)

    def test_recu_ok_au_dessus_seuil_accepte(self):
        """Un reçu ok récent avec prompt_chars >= min_chars passe."""
        maintenant = 10000.0
        recus = [{"mode": "code", "ts": maintenant - 100.0, "status": "ok", "prompt_chars": 500}]
        resultat = gemini_gate.has_recent_receipt(recus, ("code",), maintenant, 3600.0, min_chars=50)
        self.assertTrue(resultat)

    def test_recu_error_sous_seuil_accepte(self):
        """Un reçu error (Gemini down) passe même avec prompt_chars < min_chars."""
        maintenant = 10000.0
        recus = [{"mode": "code", "ts": maintenant - 100.0, "status": "error", "prompt_chars": 3}]
        resultat = gemini_gate.has_recent_receipt(recus, ("code",), maintenant, 3600.0, min_chars=50)
        self.assertTrue(resultat)

    def test_recu_ancien_sans_prompt_chars_accepte(self):
        """Un reçu ancien (sans champ prompt_chars) passe pour la rétrocompatibilité."""
        maintenant = 10000.0
        recus = [{"mode": "code", "ts": maintenant - 100.0, "status": "ok"}]
        resultat = gemini_gate.has_recent_receipt(recus, ("code",), maintenant, 3600.0, min_chars=50)
        self.assertTrue(resultat)

    def test_min_chars_zero_desactive_le_seuil(self):
        """Quand min_chars=0, même un prompt de 1 char passe (seuil désactivé)."""
        maintenant = 10000.0
        recus = [{"mode": "code", "ts": maintenant - 100.0, "status": "ok", "prompt_chars": 1}]
        resultat = gemini_gate.has_recent_receipt(recus, ("code",), maintenant, 3600.0, min_chars=0)
        self.assertTrue(resultat)

    def test_constante_gate_min_prompt_chars_existe(self):
        """La constante GATE_MIN_PROMPT_CHARS existe et vaut au moins 10."""
        self.assertTrue(hasattr(gemini_gate, "GATE_MIN_PROMPT_CHARS"))
        self.assertGreaterEqual(gemini_gate.GATE_MIN_PROMPT_CHARS, 10)


class ResolutionCheminRecuTests(unittest.TestCase):
    """`receipts_path_for()` doit résoudre exactement comme `ai_query.py::
    receipts_dir()` -- un écart entre les deux bloquerait le gate en
    permanence dès que l'écriture change de cible sans que la lecture suive
    (piège corrigé : cf. CLAUDE.md, régression du 24/09 sur la télémétrie)."""

    def test_radar_telemetry_dir_prioritaire_sur_project_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RADAR_TELEMETRY_DIR": tmpdir}, clear=True):
                chemin = gemini_gate.receipts_path_for("/autre/projet/sans/rapport")
                self.assertEqual(chemin, Path(tmpdir) / "gemini-receipts.jsonl")

    def test_repli_project_dir_si_variable_absente(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {}, clear=True):
                chemin = gemini_gate.receipts_path_for(tmpdir)
                self.assertEqual(chemin, Path(tmpdir) / ".claude" / "gemini-receipts.jsonl")

    def test_gate_lit_le_recu_ecrit_via_radar_telemetry_dir(self):
        """Bout en bout : un reçu déposé dans RADAR_TELEMETRY_DIR (comme le
        ferait `ai_query.py::write_receipt`) est bien vu par le gate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RADAR_TELEMETRY_DIR": tmpdir}, clear=True):
                recu = {"ts": time.time(), "mode": "code", "status": "ok",
                        "prompt_chars": 100}
                chemin = gemini_gate.receipts_path_for(os.environ.get("CLAUDE_PROJECT_DIR", "."))
                chemin.parent.mkdir(parents=True, exist_ok=True)
                with chemin.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(recu) + "\n")

                recus = gemini_gate.load_receipts(chemin)
                self.assertTrue(
                    gemini_gate.has_recent_receipt(
                        recus, ("code", "test"), time.time(), 3600.0, min_chars=50)
                )


class SignalementPaiementNonJustifieTests(unittest.TestCase):
    """Le signalement du paiement non justifié doit être exact — aucun faux
    positif sur un appel gratuit, un mode `reasoning` ou une escalade déclarée —
    et IDEMPOTENT : un hook PreToolUse reçoit un événement à chaque appel d'outil,
    donc réécrire la même ligne à chaque passage noierait la télémétrie et
    rendrait le compteur du tableau de bord inexploitable."""

    def _recu(self, **champs):
        base = {"ts": 1000.0, "mode": "code", "status": "ok", "prompt_chars": 500}
        base.update(champs)
        return base

    def test_recu_payant_hors_reasoning_est_signale(self):
        recus = [self._recu(paid=True, provider="deepseek", model="deepseek-chat")]
        trouves = gemini_gate.paid_without_reason(recus, 1100.0, 3600.0)
        self.assertEqual(len(trouves), 1)
        self.assertEqual(trouves[0]["provider"], "deepseek")

    def test_recu_payant_en_reasoning_justifie(self):
        recus = [self._recu(paid=True, mode="reasoning")]
        self.assertEqual(gemini_gate.paid_without_reason(recus, 1100.0, 3600.0), [])

    def test_recu_payant_avec_justification_explicite_ignore(self):
        recus = [self._recu(paid=True, justified=True)]
        self.assertEqual(gemini_gate.paid_without_reason(recus, 1100.0, 3600.0), [])

    def test_recu_escalade_tracee_ignore(self):
        recus = [self._recu(paid=True, escalated=True)]
        self.assertEqual(gemini_gate.paid_without_reason(recus, 1100.0, 3600.0), [])

    def test_recu_tier_payant_sans_champ_paid_est_signale(self):
        """Rétrocompatibilité : `tier: paid` vaut paiement même sans `paid`."""
        recus = [self._recu(tier="paid")]
        self.assertEqual(len(gemini_gate.paid_without_reason(recus, 1100.0, 3600.0)), 1)

    def test_recu_gratuit_jamais_signale(self):
        recus = [self._recu(paid=False), self._recu(paid=False, tier="free")]
        self.assertEqual(gemini_gate.paid_without_reason(recus, 1100.0, 3600.0), [])

    def test_recu_payant_expire_ignore(self):
        recus = [self._recu(paid=True, ts=1000.0)]
        self.assertEqual(gemini_gate.paid_without_reason(recus, 9000.0, 3600.0), [])

    def test_signal_ecrit_une_ligne_et_une_seule(self):
        recus = [self._recu(paid=True, provider="deepseek", model="deepseek-chat")]
        with tempfile.TemporaryDirectory() as tmpdir:
            tel = os.path.join(tmpdir, "telemetry.jsonl")
            vu = os.path.join(tmpdir, "vu.json")

            premiers = gemini_gate.signal_unjustified_paid(
                recus, 1100.0, 3600.0, telemetry_path=tel, seen_path=vu)
            self.assertEqual(len(premiers), 1)

            seconds = gemini_gate.signal_unjustified_paid(
                recus, 1100.0, 3600.0, telemetry_path=tel, seen_path=vu)
            self.assertEqual(seconds, [])

            with open(tel, "r", encoding="utf-8") as f:
                lignes = [json.loads(ligne) for ligne in f if ligne.strip()]
            self.assertEqual(len(lignes), 1)
            self.assertEqual(lignes[0]["kind"], gemini_gate.SIGNAL_KIND)
            self.assertEqual(lignes[0]["subkind"], "paid_unjustified")
            self.assertEqual(lignes[0]["provider"], "deepseek")

    def test_signal_ne_fait_rien_sans_paiement_suspect(self):
        recus = [self._recu(paid=False)]
        with tempfile.TemporaryDirectory() as tmpdir:
            tel = os.path.join(tmpdir, "telemetry.jsonl")
            self.assertEqual(
                gemini_gate.signal_unjustified_paid(
                    recus, 1100.0, 3600.0,
                    telemetry_path=tel, seen_path=os.path.join(tmpdir, "vu.json")),
                [],
            )
            self.assertFalse(os.path.exists(tel))

    def test_delegation_gate_api_publique(self):
        """Le hook vit dans `delegation_gate.py` et expose son API publique."""
        chemin = _RACINE_PROJET / "scripts" / "hooks" / "delegation_gate.py"
        self.assertTrue(chemin.is_file())
        self.assertTrue(hasattr(gemini_gate, "paid_without_reason"))
        self.assertTrue(hasattr(gemini_gate, "signal_unjustified_paid"))
        self.assertTrue(hasattr(gemini_gate, "required_modes"))
        self.assertTrue(hasattr(gemini_gate, "a_recu_direct_recent"))


class RequiredModesLectureTests(unittest.TestCase):
    """`required_modes()` sur `Read`/`Grep` — gate de pré-digestion (mode `context`)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _write_file(self, name: str, lines: int) -> str:
        path = Path(self.tmpdir.name) / name
        path.write_text("\n".join(f"ligne {i}" for i in range(1, lines + 1)), encoding="utf-8")
        return str(path)

    def test_read_petit_fichier_sous_seuil_retourne_none(self):
        f = self._write_file("petit.txt", 50)
        self.assertIsNone(gemini_gate.required_modes("Read", {"file_path": f}))

    def test_read_gros_fichier_au_dela_seuil_retourne_context(self):
        f = self._write_file("gros.txt", 100)
        self.assertEqual(gemini_gate.required_modes("Read", {"file_path": f}), ("context",))

    def test_read_limit_chirurgical_sur_gros_fichier_retourne_none(self):
        f = self._write_file("gros.txt", 100)
        modes = gemini_gate.required_modes("Read", {"file_path": f, "limit": 20})
        self.assertIsNone(modes)

    def test_read_limit_trop_large_sur_gros_fichier_retourne_context(self):
        f = self._write_file("gros.txt", 100)
        modes = gemini_gate.required_modes("Read", {"file_path": f, "limit": 50})
        self.assertEqual(modes, ("context",))

    def test_read_limit_zero_ne_borne_rien_retourne_context(self):
        """0 n'est pas une lecture chirurgicale : ça ne borne rien."""
        f = self._write_file("gros.txt", 100)
        modes = gemini_gate.required_modes("Read", {"file_path": f, "limit": 0})
        self.assertEqual(modes, ("context",))

    def test_read_limit_negatif_ne_borne_rien_retourne_context(self):
        f = self._write_file("gros.txt", 100)
        modes = gemini_gate.required_modes("Read", {"file_path": f, "limit": -5})
        self.assertEqual(modes, ("context",))

    def test_read_fichier_introuvable_retourne_none(self):
        """Jamais de blocage sur une erreur : `compte_lignes_seuil` rend None."""
        modes = gemini_gate.required_modes("Read", {"file_path": "/chemin/inexistant/foo.txt"})
        self.assertIsNone(modes)

    def test_grep_toujours_gate_quelle_que_soit_la_taille(self):
        self.assertEqual(gemini_gate.required_modes("Grep", {"pattern": "foo"}),
                         ("context", "search"))
        self.assertEqual(gemini_gate.required_modes("Grep", {"pattern": "bar", "path": "/tmp"}),
                         ("context", "search"))

    def test_read_seuils_personnalises_via_env(self):
        f = self._write_file("moyen.txt", 50)
        with patch.dict(os.environ, {"RADAR_READ_GATE_MIN_LINES": "40",
                                     "RADAR_READ_GATE_SURGICAL_LINES": "10"}):
            self.assertEqual(gemini_gate.required_modes("Read", {"file_path": f}), ("context",))
            self.assertIsNone(gemini_gate.required_modes("Read", {"file_path": f, "limit": 5}))
            self.assertEqual(
                gemini_gate.required_modes("Read", {"file_path": f, "limit": 15}), ("context",))

    def test_read_chemin_relatif_resolu_contre_project_dir(self):
        """Un chemin relatif compte les lignes du bon fichier via `CLAUDE_PROJECT_DIR`."""
        self._write_file("relatif.txt", 100)
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.tmpdir.name}):
            modes = gemini_gate.required_modes("Read", {"file_path": "relatif.txt"})
        self.assertEqual(modes, ("context",))

    def test_compte_lignes_seuil_compte_exact_sous_seuil(self):
        f = self._write_file("cinq.txt", 5)
        self.assertEqual(gemini_gate.compte_lignes_seuil(f, 80), 5)

    def test_compte_lignes_seuil_sentinelle_au_dela_du_seuil(self):
        f = self._write_file("cent.txt", 100)
        self.assertEqual(gemini_gate.compte_lignes_seuil(f, 80), 81)

    def test_compte_lignes_seuil_chemin_inexistant_retourne_none(self):
        self.assertIsNone(gemini_gate.compte_lignes_seuil("/inexistant.txt", 80))


class RequiredModesGitDiffTests(unittest.TestCase):
    """`required_modes()` sur `git diff` brut — la lecture directe d'un diff par
    Claude, contournant la délégation, observée en session (git diff -- f1 f2
    lu tel quel au lieu d'être relayé à `ai_broker.py --mode pr/diag`)."""

    def test_git_diff_brut_non_relaye_retourne_pr_diag_context(self):
        commande = "git diff -- radar_web/app.py radar_web/templates/partials/release_meta.html"
        modes = gemini_gate.required_modes("Bash", {"command": commande})
        self.assertEqual(modes, ("pr", "diag", "context"))

    def test_git_diff_pipe_vers_ai_broker_retourne_none(self):
        commande = "git diff origin/main | python3 scripts/ai_broker.py --mode pr --stdin"
        modes = gemini_gate.required_modes("Bash", {"command": commande})
        self.assertIsNone(modes)

    def test_git_diff_pipe_vers_ai_query_reste_gate(self):
        """C-4 : `ai_query.py` n'est plus un relais conforme — le diff brut reste gaté."""
        commande = "git diff origin/main | python3 scripts/ai_query.py --mode diag --stdin"
        modes = gemini_gate.required_modes("Bash", {"command": commande})
        self.assertEqual(modes, ("pr", "diag", "context"))

    def test_appel_direct_ai_query_est_gate(self):
        """C-4 : appeler `ai_query.py` en direct exige un reçu broker récent."""
        commande = "python3 scripts/ai_query.py --mode code --stdin < spec.md"
        modes = gemini_gate.required_modes("Bash", {"command": commande})
        self.assertEqual(
            modes, ("code", "test", "pr", "diag", "context", "search", "reasoning"))

    def test_ai_query_mentionne_entre_guillemets_ignore(self):
        """Une simple mention entre quotes ne déclenche pas le gate direct."""
        commande = "echo 'lance python3 scripts/ai_query.py --mode code' && ls"
        self.assertIsNone(gemini_gate.required_modes("Bash", {"command": commande}))

    def test_git_diff_pipe_vers_tail_reste_gate(self):
        """Piper vers `tail`/`cat` fait toujours atterrir le diff brut chez Claude."""
        commande = "git diff origin/main | tail -100"
        modes = gemini_gate.required_modes("Bash", {"command": commande})
        self.assertEqual(modes, ("pr", "diag", "context"))

    def test_git_diff_sans_pipe_du_tout_gate(self):
        modes = gemini_gate.required_modes("Bash", {"command": "git diff --stat"})
        self.assertEqual(modes, ("pr", "diag", "context"))

    def test_git_diff_mentionne_entre_guillemets_ignore(self):
        commande = "echo 'faire un git diff avant de committer' && ls"
        modes = gemini_gate.required_modes("Bash", {"command": commande})
        self.assertIsNone(modes)

    def test_git_status_non_concerne(self):
        self.assertIsNone(gemini_gate.required_modes("Bash", {"command": "git status"}))

    def test_git_commit_reste_prioritaire_sur_diff(self):
        """Un commit avec 'diff' dans le message ne bascule pas sur la branche diff."""
        commande = "git commit -m 'fix: corrige le diff affiché'"
        modes = gemini_gate.required_modes("Bash", {"command": commande})
        self.assertEqual(modes, ("pr",))


class RecuViaConformeTests(unittest.TestCase):
    """C-4 : un reçu écrit par un appel DIRECT à `ai_query.py` (via="direct") ne
    vaut pas délégation ; les reçus sans `via` (antérieurs) restent valides."""

    def test_recu_via_direct_ignore_quand_exige(self):
        maintenant = 10000.0
        recus = [{"mode": "code", "ts": maintenant - 10.0, "status": "ok",
                  "prompt_chars": 500, "via": "direct"}]
        self.assertFalse(gemini_gate.has_recent_receipt(
            recus, ("code",), maintenant, 3600.0, 50, exiger_via_conforme=True))

    def test_recu_via_broker_accepte_quand_exige(self):
        maintenant = 10000.0
        recus = [{"mode": "code", "ts": maintenant - 10.0, "status": "ok",
                  "prompt_chars": 500, "via": "broker"}]
        self.assertTrue(gemini_gate.has_recent_receipt(
            recus, ("code",), maintenant, 3600.0, 50, exiger_via_conforme=True))

    def test_recu_sans_via_reste_conforme(self):
        """Rétrocompatibilité : aucun champ `via` -> reçu historique accepté."""
        maintenant = 10000.0
        recus = [{"mode": "code", "ts": maintenant - 10.0, "status": "ok",
                  "prompt_chars": 500}]
        self.assertTrue(gemini_gate.has_recent_receipt(
            recus, ("code",), maintenant, 3600.0, 50, exiger_via_conforme=True))

    def test_filtre_via_inactif_par_defaut(self):
        """Sans exiger_via_conforme, un reçu direct compte encore (API inchangée)."""
        maintenant = 10000.0
        recus = [{"mode": "code", "ts": maintenant - 10.0, "status": "ok",
                  "prompt_chars": 500, "via": "direct"}]
        self.assertTrue(gemini_gate.has_recent_receipt(
            recus, ("code",), maintenant, 3600.0, 50))

    def test_a_recu_direct_recent_vrai_pour_direct_dans_ttl(self):
        maintenant = 10000.0
        recus = [{"mode": "code", "ts": maintenant - 10.0, "status": "ok",
                  "via": "direct"}]
        self.assertTrue(gemini_gate.a_recu_direct_recent(
            recus, ("code",), maintenant, 3600.0))

    def test_a_recu_direct_recent_faux_pour_broker(self):
        maintenant = 10000.0
        recus = [{"mode": "code", "ts": maintenant - 10.0, "status": "ok",
                  "via": "broker"}]
        self.assertFalse(gemini_gate.a_recu_direct_recent(
            recus, ("code",), maintenant, 3600.0))

    def test_a_recu_direct_recent_faux_hors_ttl(self):
        maintenant = 10000.0
        recus = [{"mode": "code", "ts": maintenant - 9000.0, "status": "ok",
                  "via": "direct"}]
        self.assertFalse(gemini_gate.a_recu_direct_recent(
            recus, ("code",), maintenant, 3600.0))


class MainGateViaTests(unittest.TestCase):
    """Bout en bout sur `main()` : un reçu direct ne débloque pas une écriture de
    module, et le message renvoie vers `ai_broker.py`."""

    def _run(self, event, receipts):
        tmpdir = tempfile.mkdtemp()
        try:
            recu_path = Path(tmpdir) / "gemini-receipts.jsonl"
            recu_path.write_text(
                "".join(json.dumps(r) + "\n" for r in receipts), encoding="utf-8")
            err = io.StringIO()
            with patch.dict(os.environ, {"RADAR_TELEMETRY_DIR": tmpdir}, clear=False):
                with patch("sys.stdin", io.StringIO(json.dumps(event))), \
                        patch("sys.stderr", err):
                    code = gemini_gate.main()
            return code, err.getvalue()
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_ecriture_module_bloquee_si_seul_recu_direct(self):
        event = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/nouveau_module.py"}}
        recus = [{"ts": time.time(), "mode": "code", "status": "ok",
                  "prompt_chars": 500, "via": "direct"}]
        code, _ = self._run(event, recus)
        self.assertEqual(code, 2)

    def test_ecriture_module_autorisee_si_recu_broker(self):
        event = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/nouveau_module.py"}}
        recus = [{"ts": time.time(), "mode": "code", "status": "ok",
                  "prompt_chars": 500, "via": "broker"}]
        code, _ = self._run(event, recus)
        self.assertEqual(code, 0)

    def test_message_renvoie_vers_ai_broker_quand_recu_direct(self):
        event = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/nouveau_module.py"}}
        recus = [{"ts": time.time(), "mode": "code", "status": "ok",
                  "prompt_chars": 500, "via": "direct"}]
        _, err = self._run(event, recus)
        self.assertIn("ai_broker.py", err)
        self.assertIn('via="direct"', err)


class MarqueurRequeteGateTests(unittest.TestCase):
    """C-2 : le gate relit le marqueur de requête écrit par `workflow_reminder.py`
    et le signale quand la requête courante est jugée multi-étapes."""

    def _run(self, event, marker=None):
        tmpdir = tempfile.mkdtemp()
        try:
            (Path(tmpdir) / "gemini-receipts.jsonl").write_text("", encoding="utf-8")
            if marker is not None:
                dossier = Path(tmpdir) / ".claude"
                dossier.mkdir(parents=True, exist_ok=True)
                (dossier / "current_request.json").write_text(
                    json.dumps(marker), encoding="utf-8")
            err = io.StringIO()
            env = {"RADAR_TELEMETRY_DIR": tmpdir, "CLAUDE_PROJECT_DIR": tmpdir}
            with patch.dict(os.environ, env, clear=False):
                with patch("sys.stdin", io.StringIO(json.dumps(event))), \
                        patch("sys.stderr", err):
                    code = gemini_gate.main()
            return code, err.getvalue()
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_message_signale_requete_non_triviale(self):
        event = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/nouveau.py"}}
        code, err = self._run(event, marker={"non_trivial": True,
                                             "prompt_head": "implémente le cache"})
        self.assertEqual(code, 2)
        self.assertIn("current_request.json", err)
        self.assertIn("plusieurs étapes", err)

    def test_message_sans_hint_pour_requete_triviale(self):
        event = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/nouveau.py"}}
        code, err = self._run(event, marker={"non_trivial": False})
        self.assertEqual(code, 2)
        self.assertNotIn("plusieurs étapes", err)

    def test_message_sans_hint_si_marqueur_absent(self):
        event = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/nouveau.py"}}
        code, err = self._run(event, marker=None)
        self.assertEqual(code, 2)
        self.assertNotIn("plusieurs étapes", err)

    def test_load_current_request_absent_renvoie_dict_vide(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(gemini_gate.load_current_request(tmp), {})

    def test_load_current_request_corrompu_renvoie_dict_vide(self):
        with tempfile.TemporaryDirectory() as tmp:
            dossier = Path(tmp) / ".claude"
            dossier.mkdir()
            (dossier / "current_request.json").write_text("{pas du json", encoding="utf-8")
            self.assertEqual(gemini_gate.load_current_request(tmp), {})

    def test_current_request_path_convention(self):
        self.assertEqual(str(gemini_gate.current_request_path("/p")),
                         os.path.join("/p", ".claude", gemini_gate.CURRENT_REQUEST_NAME))


class OuvrierHelpersTests(unittest.TestCase):
    """C-1 : briques pures du déclencheur « tâche multi-outils → ouvrier »."""

    def test_est_ecriture_python(self):
        self.assertTrue(gemini_gate.est_ecriture_python("Write", {"file_path": "a/b.py"}))
        self.assertTrue(gemini_gate.est_ecriture_python("Edit", {"file_path": "a/b.py"}))
        self.assertFalse(gemini_gate.est_ecriture_python("Write", {"file_path": "a/b.md"}))
        self.assertFalse(gemini_gate.est_ecriture_python("Read", {"file_path": "a/b.py"}))
        self.assertFalse(gemini_gate.est_ecriture_python("Write", {}))

    def test_raison_multi_outils(self):
        r = gemini_gate.raison_delegation_ecriture(
            "Write", {"file_path": "a.py"}, appels_outils=8, min_appels=8, requete={})
        self.assertEqual(r, (gemini_gate.WORKER_MODES, "multi_outils"))

    def test_raison_multi_etapes(self):
        r = gemini_gate.raison_delegation_ecriture(
            "Edit", {"file_path": "a.py"}, appels_outils=0, min_appels=8,
            requete={"non_trivial": True})
        self.assertEqual(r, (gemini_gate.DELEGATION_MODES, "requete_multi_etapes"))

    def test_raison_none_hors_python(self):
        self.assertIsNone(gemini_gate.raison_delegation_ecriture(
            "Write", {"file_path": "note.md"}, appels_outils=99, min_appels=8,
            requete={"non_trivial": True}))

    def test_raison_none_sous_seuil_et_requete_simple(self):
        self.assertIsNone(gemini_gate.raison_delegation_ecriture(
            "Write", {"file_path": "a.py"}, appels_outils=7, min_appels=8, requete={}))

    def test_compte_appels_outils_filtre_session_et_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            chemin = Path(tmp) / "telemetry.jsonl"
            chemin.write_text(
                json.dumps({"kind": "tool", "session": "s1", "ts": 100.0}) + "\n"
                + json.dumps({"kind": "prompt", "session": "s1", "ts": 101.0}) + "\n"
                + json.dumps({"kind": "tool", "session": "s2", "ts": 102.0}) + "\n"
                + json.dumps({"kind": "tool", "session": "s1", "ts": 50.0}) + "\n"
                + "PAS_DU_JSON\n",
                encoding="utf-8",
            )
            self.assertEqual(gemini_gate.compte_appels_outils(chemin, "s1"), 2)
            self.assertEqual(gemini_gate.compte_appels_outils(chemin, "s1", 60.0), 1)
            self.assertEqual(gemini_gate.compte_appels_outils(chemin, "s3"), 0)
            self.assertEqual(gemini_gate.compte_appels_outils(None, "s1"), 0)
            self.assertEqual(gemini_gate.compte_appels_outils(chemin, ""), 0)

    def test_a_recu_depuis_borne_et_via(self):
        recus = [
            {"mode": "worker", "ts": 200.0, "via": "worker"},
            {"mode": "worker", "ts": 50.0, "via": "worker"},
            {"mode": "worker", "ts": 300.0, "via": "direct"},
        ]
        self.assertTrue(gemini_gate.a_recu_depuis(recus, ("worker",), 100.0))
        self.assertFalse(gemini_gate.a_recu_depuis(recus, ("worker",), 250.0))
        self.assertFalse(gemini_gate.a_recu_depuis(recus, ("code",), 0.0))

    def test_debut_requete_prend_le_plus_recent(self):
        now, ttl = 10000.0, 3600.0
        # Marqueur postérieur à la fenêtre TTL : c'est lui qui borne.
        self.assertEqual(gemini_gate._debut_requete({"ts": 9000.0}, now, ttl), 9000.0)
        # Sans marqueur, ou marqueur plus vieux que le TTL : la fenêtre TTL borne.
        self.assertEqual(gemini_gate._debut_requete({}, now, ttl), 6400.0)
        self.assertEqual(gemini_gate._debut_requete({"ts": 100.0}, now, ttl), 6400.0)

    def test_telemetry_path_for_respecte_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"RADAR_TELEMETRY_DIR": tmp}, clear=False):
                self.assertEqual(
                    str(gemini_gate.telemetry_path_for("/projet")),
                    os.path.join(tmp, "telemetry.jsonl"),
                )


class OuvrierGateTests(unittest.TestCase):
    """C-1 : bout en bout sur `main()`. Une session qui enchaîne `>= 8` appels
    d'outils sur la requête ne peut plus écrire de `.py` sans reçu `worker`."""

    def _run(self, event, *, receipts=(), outils=(), marker=None,
             session="sess-1", fichiers=()):
        tmpdir = tempfile.mkdtemp()
        try:
            for nom in fichiers:
                p = Path(tmpdir) / nom
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("# existant\n", encoding="utf-8")
            (Path(tmpdir) / "gemini-receipts.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in receipts), encoding="utf-8")
            if outils:
                (Path(tmpdir) / "telemetry.jsonl").write_text(
                    "".join(json.dumps(o) + "\n" for o in outils), encoding="utf-8")
            if marker is not None:
                dossier = Path(tmpdir) / ".claude"
                dossier.mkdir(parents=True, exist_ok=True)
                (dossier / "current_request.json").write_text(
                    json.dumps(marker), encoding="utf-8")
            evt = json.loads(json.dumps(event).replace("__TMP__", tmpdir))
            if session is not None:
                evt.setdefault("session_id", session)
            err = io.StringIO()
            env = {"RADAR_TELEMETRY_DIR": tmpdir, "CLAUDE_PROJECT_DIR": tmpdir}
            with patch.dict(os.environ, env, clear=False):
                with patch("sys.stdin", io.StringIO(json.dumps(evt))), \
                        patch("sys.stderr", err):
                    code = gemini_gate.main()
            return code, err.getvalue()
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _outils(n, session="sess-1"):
        # ts légèrement passé : un marqueur écrit « juste après » les borne bien.
        return [{"kind": "tool", "session": session, "ts": time.time() - 1.0}
                for _ in range(n)]

    def _ecriture(self, chemin="/tmp/nouveau.py"):
        return {"tool_name": "Write", "tool_input": {"file_path": chemin}}

    def test_hot_session_bloque_ecriture_sans_worker(self):
        code, err = self._run(self._ecriture(), outils=self._outils(8))
        self.assertEqual(code, 2)
        self.assertIn("ai_worker.py", err)
        self.assertIn("worker", err)

    def test_sept_appels_sous_seuil_ne_bloquent_pas(self):
        event = {"tool_name": "Edit", "tool_input": {
            "file_path": "__TMP__/module.py", "old_string": "a", "new_string": "b"}}
        code, _ = self._run(event, outils=self._outils(7), fichiers=["module.py"])
        self.assertEqual(code, 0)

    def test_appels_d_une_autre_session_ignores(self):
        event = {"tool_name": "Edit", "tool_input": {
            "file_path": "__TMP__/module.py", "old_string": "a", "new_string": "b"}}
        code, _ = self._run(event, outils=self._outils(8, session="autre"),
                            fichiers=["module.py"])
        self.assertEqual(code, 0)

    def test_hot_session_debloquee_par_recu_worker(self):
        recus = [{"mode": "worker", "ts": time.time(), "status": "termine",
                  "via": "worker", "prompt_chars": 500}]
        code, _ = self._run(self._ecriture(), receipts=recus, outils=self._outils(8))
        self.assertEqual(code, 0)

    def test_recu_worker_direct_ne_debloque_pas(self):
        recus = [{"mode": "worker", "ts": time.time(), "status": "termine",
                  "via": "direct", "prompt_chars": 500}]
        code, _ = self._run(self._ecriture(), receipts=recus, outils=self._outils(8))
        self.assertEqual(code, 2)

    def test_recu_worker_anterieur_a_la_requete_ne_compte_pas(self):
        marker = {"ts": time.time(), "non_trivial": True}
        recus = [{"mode": "worker", "ts": time.time() - 100.0, "status": "termine",
                  "via": "worker", "prompt_chars": 500}]
        code, _ = self._run(self._ecriture(), receipts=recus,
                            outils=self._outils(8), marker=marker)
        self.assertEqual(code, 2)

    def test_requete_multi_etapes_bloquee_sans_recu(self):
        marker = {"ts": time.time(), "non_trivial": True}
        code, err = self._run(self._ecriture(), marker=marker)
        self.assertEqual(code, 2)
        self.assertIn("current_request.json", err)
        self.assertIn("plusieurs étapes", err)

    def test_requete_multi_etapes_debloquee_par_recu_code(self):
        marker = {"ts": time.time() - 10.0, "non_trivial": True}
        recus = [{"mode": "code", "ts": time.time(), "status": "ok",
                  "via": "broker", "prompt_chars": 500}]
        code, _ = self._run(self._ecriture(), receipts=recus, marker=marker)
        self.assertEqual(code, 0)


def _lignes(n, prefixe="ligne"):
    """Bloc de `n` lignes distinctes, pour dépasser un seuil de façon lisible."""
    return "\n".join(f"{prefixe} {i}" for i in range(n))


class EditionLargeHelpersTests(unittest.TestCase):
    """C-3 : `required_modes()` gate un `Edit` MASSIF sur un `.py` existant, mais
    laisse passer la correction chirurgicale (passe-droit historique d'`Edit`)."""

    def _module(self, rep, nom="module.py"):
        p = Path(rep) / nom
        p.write_text("# code existant\n", encoding="utf-8")
        return p

    def test_compte_lignes_edition_somme_old_et_new(self):
        n = gemini_gate.compte_lignes_edition(
            {"old_string": _lignes(3), "new_string": _lignes(2)})
        self.assertEqual(n, 5)

    def test_compte_lignes_edition_champs_absents_vaut_zero(self):
        self.assertEqual(gemini_gate.compte_lignes_edition({}), 0)

    def test_edit_massif_module_existant_exige_code(self):
        with tempfile.TemporaryDirectory() as rep:
            p = self._module(rep)
            modes = gemini_gate.required_modes("Edit", {
                "file_path": str(p),
                "old_string": _lignes(30),
                "new_string": _lignes(15),
            })
            self.assertEqual(modes, ("code", "test"))

    def test_edit_chirurgical_module_existant_laisse_passer(self):
        with tempfile.TemporaryDirectory() as rep:
            p = self._module(rep)
            modes = gemini_gate.required_modes("Edit", {
                "file_path": str(p),
                "old_string": _lignes(3),
                "new_string": _lignes(2),
            })
            self.assertIsNone(modes)

    def test_edit_a_la_limite_exacte_du_seuil_laisse_passer(self):
        # Exactement le seuil (40 lignes) : la condition est « > seuil » → passe.
        with tempfile.TemporaryDirectory() as rep:
            p = self._module(rep)
            modes = gemini_gate.required_modes("Edit", {
                "file_path": str(p),
                "old_string": _lignes(20),
                "new_string": _lignes(20),
            })
            self.assertIsNone(modes)

    def test_insertion_pure_sans_old_string_compte_new(self):
        with tempfile.TemporaryDirectory() as rep:
            p = self._module(rep)
            modes = gemini_gate.required_modes("Edit", {
                "file_path": str(p),
                "new_string": _lignes(60),
            })
            self.assertEqual(modes, ("code", "test"))

    def test_edit_massif_fichier_non_python_laisse_passer(self):
        with tempfile.TemporaryDirectory() as rep:
            p = Path(rep) / "note.md"
            p.write_text("# note\n", encoding="utf-8")
            modes = gemini_gate.required_modes("Edit", {
                "file_path": str(p),
                "old_string": _lignes(30),
                "new_string": _lignes(30),
            })
            self.assertIsNone(modes)

    def test_edit_massif_fichier_test_reste_mode_test(self):
        with tempfile.TemporaryDirectory() as rep:
            p = Path(rep) / "tests" / "test_x.py"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# test\n", encoding="utf-8")
            modes = gemini_gate.required_modes("Edit", {
                "file_path": str(p),
                "old_string": _lignes(30),
                "new_string": _lignes(30),
            })
            self.assertEqual(modes, ("test", "code"))

    def test_edit_fichier_inexistant_laisse_passer(self):
        with tempfile.TemporaryDirectory() as rep:
            p = Path(rep) / "absent.py"
            modes = gemini_gate.required_modes("Edit", {
                "file_path": str(p),
                "old_string": _lignes(30),
                "new_string": _lignes(30),
            })
            self.assertIsNone(modes)

    def test_seuil_zero_desactive_le_declencheur(self):
        with tempfile.TemporaryDirectory() as rep:
            p = self._module(rep)
            entree = {"file_path": str(p),
                      "old_string": _lignes(30), "new_string": _lignes(30)}
            with patch.dict(os.environ, {"RADAR_EDIT_GATE_MIN_LINES": "0"}):
                self.assertIsNone(gemini_gate.required_modes("Edit", entree))

    def test_seuil_configurable_releve(self):
        with tempfile.TemporaryDirectory() as rep:
            p = self._module(rep)
            entree = {"file_path": str(p),
                      "old_string": _lignes(30), "new_string": _lignes(15)}
            with patch.dict(os.environ, {"RADAR_EDIT_GATE_MIN_LINES": "100"}):
                self.assertIsNone(gemini_gate.required_modes("Edit", entree))


class EditionLargeGateTests(unittest.TestCase):
    """C-3 de bout en bout : le gate bloque un `Edit` massif sans reçu, laisse
    passer la correction chirurgicale, et accepte un reçu `code` broker récent."""

    def _run(self, event, *, receipts=(), fichiers=()):
        tmpdir = tempfile.mkdtemp()
        try:
            for nom in fichiers:
                p = Path(tmpdir) / nom
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("# existant\n", encoding="utf-8")
            (Path(tmpdir) / "gemini-receipts.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in receipts), encoding="utf-8")
            evt = json.loads(json.dumps(event).replace("__TMP__", tmpdir))
            evt.setdefault("session_id", "sess-1")
            err = io.StringIO()
            env = {"RADAR_TELEMETRY_DIR": tmpdir, "CLAUDE_PROJECT_DIR": tmpdir}
            with patch.dict(os.environ, env, clear=False):
                with patch("sys.stdin", io.StringIO(json.dumps(evt))), \
                        patch("sys.stderr", err):
                    code = gemini_gate.main()
            return code, err.getvalue()
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _edit(self, vieux, nouveau):
        return {"tool_name": "Edit", "tool_input": {
            "file_path": "__TMP__/module.py",
            "old_string": _lignes(vieux),
            "new_string": _lignes(nouveau),
        }}

    def test_edit_massif_sans_recu_bloque(self):
        code, err = self._run(self._edit(30, 15), fichiers=["module.py"])
        self.assertEqual(code, 2)
        self.assertIn("ai_broker.py", err)

    def test_edit_chirurgical_sans_recu_passe(self):
        code, _ = self._run(self._edit(3, 2), fichiers=["module.py"])
        self.assertEqual(code, 0)

    def test_edit_massif_debloque_par_recu_code(self):
        recus = [{"mode": "code", "ts": time.time(), "status": "ok",
                  "via": "broker", "prompt_chars": 500}]
        code, _ = self._run(self._edit(30, 15), receipts=recus,
                            fichiers=["module.py"])
        self.assertEqual(code, 0)

    def test_recu_direct_ne_debloque_pas_edit_massif(self):
        recus = [{"mode": "code", "ts": time.time(), "status": "ok",
                  "via": "direct", "prompt_chars": 500}]
        code, _ = self._run(self._edit(30, 15), receipts=recus,
                            fichiers=["module.py"])
        self.assertEqual(code, 2)


class BalayageHelpersTests(unittest.TestCase):
    """C-6 : détection du balayage et exigence d'un reçu `search`/`explore-leger`."""

    def test_motif_balayage_detecte_formulations(self):
        for txt in ("où est la config ?", "où sont les hooks ?",
                    "trouve tous les appels", "liste les routes",
                    "recense les modules", "fais un inventaire du dossier",
                    "scanne le dépôt"):
            self.assertTrue(gemini_gate._motif_balayage(txt), txt)

    def test_motif_balayage_ignore_texte_ordinaire(self):
        self.assertFalse(gemini_gate._motif_balayage("implémente le cache LRU"))
        self.assertFalse(gemini_gate._motif_balayage(""))

    def test_raison_balayage_grep_avec_motif(self):
        r = gemini_gate.raison_delegation_balayage(
            "Grep", ("context", "search"), {"prompt_head": "où est l'auth ?"})
        self.assertEqual(r, (gemini_gate.SEARCH_MODES, "balayage_a_deleguer"))

    def test_raison_balayage_grep_sans_motif(self):
        self.assertIsNone(gemini_gate.raison_delegation_balayage(
            "Grep", ("context", "search"), {"prompt_head": "corrige le bug"}))

    def test_raison_balayage_read_large_avec_motif(self):
        r = gemini_gate.raison_delegation_balayage(
            "Read", ("context",), {"prompt_head": "trouve toutes les routes"})
        self.assertIsNotNone(r)

    def test_raison_balayage_read_chirurgicale_ignoree(self):
        # `modes_requis is None` : lecture ciblée → pas de contrainte C-6.
        self.assertIsNone(gemini_gate.raison_delegation_balayage(
            "Read", None, {"prompt_head": "trouve tous les appels"}))

    def test_raison_balayage_autre_outil_ignore(self):
        self.assertIsNone(gemini_gate.raison_delegation_balayage(
            "Bash", ("pr",), {"prompt_head": "liste les fichiers"}))

    def test_recu_recherche_accepte_mode_search(self):
        self.assertTrue(gemini_gate.a_recu_recherche(
            [{"mode": "search", "ts": 100.0, "via": "broker"}], 50.0))

    def test_recu_recherche_accepte_trace_explore_leger(self):
        self.assertTrue(gemini_gate.a_recu_recherche(
            [{"mode": "context", "ts": 100.0, "source": "explore-leger"}], 50.0))

    def test_recu_recherche_rejette_direct(self):
        self.assertFalse(gemini_gate.a_recu_recherche(
            [{"mode": "search", "ts": 100.0, "via": "direct"}], 50.0))

    def test_recu_recherche_rejette_avant_la_requete(self):
        self.assertFalse(gemini_gate.a_recu_recherche(
            [{"mode": "search", "ts": 10.0, "via": "broker"}], 50.0))


class BalayageGateTests(unittest.TestCase):
    """C-6 de bout en bout : un `Grep` sur requête de balayage exige `search`."""

    def _run(self, event, *, receipts=(), marker=None):
        tmpdir = tempfile.mkdtemp()
        try:
            (Path(tmpdir) / "gemini-receipts.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in receipts), encoding="utf-8")
            if marker is not None:
                dossier = Path(tmpdir) / ".claude"
                dossier.mkdir(parents=True, exist_ok=True)
                (dossier / "current_request.json").write_text(
                    json.dumps(marker), encoding="utf-8")
            evt = json.loads(json.dumps(event).replace("__TMP__", tmpdir))
            evt.setdefault("session_id", "sess-1")
            err = io.StringIO()
            env = {"RADAR_TELEMETRY_DIR": tmpdir, "CLAUDE_PROJECT_DIR": tmpdir}
            with patch.dict(os.environ, env, clear=False):
                with patch("sys.stdin", io.StringIO(json.dumps(evt))), \
                        patch("sys.stderr", err):
                    code = gemini_gate.main()
            return code, err.getvalue()
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _grep():
        return {"tool_name": "Grep", "tool_input": {"pattern": "foo"}}

    def test_grep_balayage_sans_recu_bloque(self):
        code, err = self._run(self._grep(), marker={
            "ts": time.time(), "prompt_head": "où est la config ?"})
        self.assertEqual(code, 2)
        self.assertIn("search", err)
        self.assertIn("explore-leger", err)

    def test_grep_balayage_debloque_par_recu_search(self):
        recus = [{"mode": "search", "ts": time.time(), "status": "ok",
                  "via": "broker", "prompt_chars": 500}]
        code, _ = self._run(self._grep(), receipts=recus, marker={
            "ts": time.time() - 10.0, "prompt_head": "trouve tous les appels"})
        self.assertEqual(code, 0)

    def test_recu_context_ne_suffit_pas_pour_un_balayage(self):
        recus = [{"mode": "context", "ts": time.time(), "status": "ok",
                  "via": "broker", "prompt_chars": 500}]
        code, _ = self._run(self._grep(), receipts=recus, marker={
            "ts": time.time() - 10.0, "prompt_head": "liste les routes"})
        self.assertEqual(code, 2)

    def test_grep_hors_balayage_garde_le_regime_historique(self):
        # Pas de motif : un reçu `context` suffit, comme avant C-6.
        recus = [{"mode": "context", "ts": time.time(), "status": "ok",
                  "via": "broker", "prompt_chars": 500}]
        code, _ = self._run(self._grep(), receipts=recus, marker={
            "ts": time.time() - 10.0, "prompt_head": "corrige le cache"})
        self.assertEqual(code, 0)

    def test_trace_explore_leger_debloque_le_balayage(self):
        recus = [{"mode": "context", "ts": time.time(), "status": "ok",
                  "source": "explore-leger", "prompt_chars": 500}]
        code, _ = self._run(self._grep(), receipts=recus, marker={
            "ts": time.time() - 10.0, "prompt_head": "où sont les hooks ?"})
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
