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


if __name__ == "__main__":
    unittest.main()
