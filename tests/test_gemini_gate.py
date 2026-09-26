"""Tests unitaires pour le garde-fou gemini_gate.py.

Ce garde-fou impose la délégation préalable à Gemini pour certaines actions
critiques (commits git, premier jet de code ou de tests). Sa justesse conditionne
toute la politique de délégation du projet : s'il est trop permissif, des actions
non révisées passent en production ; s'il est trop strict ou erratique, le flux de
travail de Claude est paralysé.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Chargement dynamique du module scripts/hooks/gemini_gate.py
_RACINE_PROJET = Path(__file__).resolve().parent.parent
_CHEMIN_MODULE = _RACINE_PROJET / "scripts" / "hooks" / "gemini_gate.py"

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

    def test_delegation_gate_existe_et_alias_complet(self):
        """Le hook vit dans `delegation_gate.py` ; `gemini_gate` reste la façade."""
        chemin = _RACINE_PROJET / "scripts" / "hooks" / "delegation_gate.py"
        self.assertTrue(chemin.is_file())
        self.assertTrue(hasattr(gemini_gate, "paid_without_reason"))
        self.assertTrue(hasattr(gemini_gate, "signal_unjustified_paid"))
        self.assertTrue(hasattr(gemini_gate, "required_modes"))


if __name__ == "__main__":
    unittest.main()
