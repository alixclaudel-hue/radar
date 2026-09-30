"""Tests unitaires pour :mod:`scripts.bench.scenarios`.

Ces tests sont entièrement hors ligne : aucun appel réseau, aucun appel au
courtier, aucun accès disque métier. Chaque cas fabrique la « sortie modèle »
attendue puis exerce uniquement la fonction ``check`` du scénario concerné.
"""

from __future__ import annotations

import json
import unittest

from scripts.bench import scenarios
from scripts.bench.scenarios import get_scenarios


# ---------------------------------------------------------------------------
# Sorties fabriquées
# ---------------------------------------------------------------------------

# JSON strict attendu par le scénario « json_contrat ».
_CONTRAT_VALIDE = {
    "ville": "Lyon",
    "date_debut": "2026-10-12",
    "date_fin": "2026-10-15",
    "adultes": 2,
    "enfants": 1,
    "budget_max": 450,
    "petit_dejeuner": True,
}

# Worker asynchrone conforme : compile, contient ``run_worker`` et
# ``Semaphore``, préserve l'ordre et renvoie les exceptions en résultat.
_CODE_NOMINAL_VALIDE = (
    "import asyncio\n"
    "\n"
    "\n"
    "async def run_worker(urls, fetch, concurrency=3):\n"
    "    sem = asyncio.Semaphore(concurrency)\n"
    "\n"
    "    async def _one(u):\n"
    "        async with sem:\n"
    "            try:\n"
    "                return await fetch(u)\n"
    "            except Exception as exc:\n"
    "                return exc\n"
    "\n"
    "    return await asyncio.gather(*(_one(u) for u in urls))\n"
)

# Code qui ne compile pas mais qui contient tout de même ``run_worker`` et
# ``Semaphore`` : le score doit rester strictement inférieur à 0,5.
_CODE_NOMINAL_CASSE = (
    "import asyncio\n"
    "async def run_worker(urls, fetch, concurrency=3):\n"
    "    sem = asyncio.Semaphore(concurrency)\n"
    "    return [\n"
)

# Version corrigée de ``moyenne``.
_CODE_REPAIR_VALIDE = (
    "def moyenne(xs):\n"
    "    total = sum(xs)\n"
    "    return total / len(xs)\n"
)

# Carte HTML+CSS conforme à la charte : trois couleurs douces, coins arrondis.
_HTML_UI_VALIDE = (
    '<div class="card">\n'
    "  <style>\n"
    "    .card {\n"
    "      background: #FDF8F3;\n"
    "      color: #5B4636;\n"
    "      border: 1px solid #E8DCC8;\n"
    "      border-radius: 12px;\n"
    "      padding: 16px;\n"
    "    }\n"
    "  </style>\n"
    "  <h2>Titre</h2>\n"
    "  <p>Artiste - score 0.9</p>\n"
    "</div>\n"
)


# ---------------------------------------------------------------------------
# json_contrat
# ---------------------------------------------------------------------------

class TestJsonContrat(unittest.TestCase):
    """Vérifie le contrat JSON strict."""

    def test_sortie_parfaite_score_un(self):
        """Un JSON exactement conforme obtient 1.0."""
        check = scenarios._check_json_contrat(json.dumps(_CONTRAT_VALIDE))
        self.assertTrue(check.ok)
        self.assertEqual(check.score, 1.0)

    def test_texte_autour_du_json_score_inferieur_a_un(self):
        """Du texte autour du JSON casse la lecture : score < 1."""
        texte = "Voici le résultat :\n" + json.dumps(_CONTRAT_VALIDE) + "\nMerci."
        check = scenarios._check_json_contrat(texte)
        self.assertFalse(check.ok)
        self.assertLess(check.score, 1.0)

    def test_mauvaise_valeur_adultes_score_inferieur_a_un(self):
        """Bien formé mais valeur erronée : score < 1."""
        donnees = dict(_CONTRAT_VALIDE)
        donnees["adultes"] = 5
        check = scenarios._check_json_contrat(json.dumps(donnees))
        self.assertFalse(check.ok)
        self.assertLess(check.score, 1.0)

    def test_chaine_vide_score_zero(self):
        """Une chaîne vide n'est pas un JSON : score 0."""
        check = scenarios._check_json_contrat("")
        self.assertFalse(check.ok)
        self.assertEqual(check.score, 0.0)


# ---------------------------------------------------------------------------
# search_route
# ---------------------------------------------------------------------------

class TestSearchRoute(unittest.TestCase):
    """Vérifie le routage de recherche."""

    def test_bon_chemin_score_un(self):
        """Le chemin attendu obtient 1.0."""
        texte = json.dumps({
            "path": "scripts/hooks/delegation_gate.py",
            "justification": "garde-fou des lectures longues",
        })
        check = scenarios._check_search_route(texte)
        self.assertTrue(check.ok)
        self.assertEqual(check.score, 1.0)

    def test_chemin_hors_liste_score_max_un_tiers(self):
        """Un chemin absent de la liste plafonne à 1/3."""
        texte = json.dumps({
            "path": "scripts/inexistant.py",
            "justification": "hors sujet",
        })
        check = scenarios._check_search_route(texte)
        self.assertFalse(check.ok)
        self.assertLessEqual(check.score, 1.0 / 3.0)

    def test_chemin_de_la_liste_mais_faux_score_deux_tiers(self):
        """Un chemin valide mais erroné obtient 2/3."""
        texte = json.dumps({
            "path": "scripts/hooks/telemetry.py",
            "justification": "mauvais choix",
        })
        check = scenarios._check_search_route(texte)
        self.assertFalse(check.ok)
        self.assertAlmostEqual(check.score, 2.0 / 3.0, places=6)


# ---------------------------------------------------------------------------
# contexte_pavage
# ---------------------------------------------------------------------------

class TestContextePavage(unittest.TestCase):
    """Vérifie le pavage du contexte sans mélange de mots-clés."""

    def test_analyses_correctes_score_un(self):
        """Trois analyses propres et disjointes obtiennent 1.0."""
        donnees = {"analyses": [
            {"path": "docs/alpha.md", "analyse": "cache disque purgé"},
            {"path": "docs/beta.md", "analyse": "quota YouTube limité"},
            {"path": "docs/gamma.md", "analyse": "rotation journaux archivés"},
        ]}
        check = scenarios._check_contexte_pavage(json.dumps(donnees))
        self.assertTrue(check.ok)
        self.assertEqual(check.score, 1.0)

    def test_analyse_melangeant_deux_mots_cles(self):
        """Une analyse qui mélange deux mots-clés fait chuter le score."""
        donnees = {"analyses": [
            {"path": "docs/alpha.md", "analyse": "cache et quota mélangés"},
            {"path": "docs/beta.md", "analyse": "quota YouTube limité"},
            {"path": "docs/gamma.md", "analyse": "rotation journaux archivés"},
        ]}
        check = scenarios._check_contexte_pavage(json.dumps(donnees))
        self.assertFalse(check.ok)
        self.assertLess(check.score, 1.0)


# ---------------------------------------------------------------------------
# code_nominal
# ---------------------------------------------------------------------------

class TestCodeNominal(unittest.TestCase):
    """Vérifie l'évaluation déterministe du worker asynchrone."""

    def test_code_correct_avec_semaphore_score_un(self):
        """Un worker conforme obtient 1.0."""
        check = scenarios._check_code_nominal(_CODE_NOMINAL_VALIDE)
        self.assertTrue(check.ok)
        self.assertEqual(check.score, 1.0)

    def test_code_qui_ne_compile_pas(self):
        """Un code non compilable reste sous 0,5."""
        check = scenarios._check_code_nominal(_CODE_NOMINAL_CASSE)
        self.assertFalse(check.ok)
        self.assertLess(check.score, 0.5)


# ---------------------------------------------------------------------------
# code_repair
# ---------------------------------------------------------------------------

class TestCodeRepair(unittest.TestCase):
    """Vérifie la réparation de code."""

    def test_code_corrige_score_un(self):
        """Le code corrigé obtient 1.0."""
        check = scenarios._check_code_repair(_CODE_REPAIR_VALIDE)
        self.assertTrue(check.ok)
        self.assertEqual(check.score, 1.0)

    def test_build_prompt_contient_le_message_syntaxerror(self):
        """Le prompt de réparation contient le message de la SyntaxError."""
        scenario = get_scenarios(["code_repair"])[0]
        prompt = scenario.build_prompt()
        self.assertIn("def moyenne(xs)", prompt)
        self.assertIn("Erreur :", prompt)
        self.assertIn("ligne", prompt)
        try:
            compile(scenarios._BROKEN_MOYENNE, "<broken>", "exec")
        except SyntaxError as exc:
            self.assertIn(str(exc), prompt)
        else:  # pragma: no cover - le code cassé doit toujours échouer
            self.fail("le code cassé de code_repair devrait lever une SyntaxError")


# ---------------------------------------------------------------------------
# code_ui
# ---------------------------------------------------------------------------

class TestCodeUi(unittest.TestCase):
    """Vérifie la carte HTML+CSS."""

    def test_html_conforme_score_un(self):
        """Une carte respectant la charte obtient 1.0."""
        check = scenarios._check_code_ui(_HTML_UI_VALIDE)
        self.assertTrue(check.ok)
        self.assertEqual(check.score, 1.0)

    def test_html_avec_noir_pur(self):
        """Le noir pur (#000000) est interdit : score < 1."""
        html = _HTML_UI_VALIDE.replace("#5B4636", "#000000")
        check = scenarios._check_code_ui(html)
        self.assertFalse(check.ok)
        self.assertLess(check.score, 1.0)


# ---------------------------------------------------------------------------
# get_scenarios
# ---------------------------------------------------------------------------

class TestGetScenarios(unittest.TestCase):
    """Vérifie la sélection des scénarios."""

    def test_id_inconnu_leve_value_error(self):
        """Un identifiant inconnu doit lever ``ValueError``."""
        with self.assertRaises(ValueError):
            get_scenarios(["scenario_qui_nexiste_pas"])

    def test_id_connu_renvoie_le_scenario(self):
        """Un identifiant connu renvoie le scénario demandé."""
        selection = get_scenarios(["search_route"])
        self.assertEqual(len(selection), 1)
        self.assertEqual(selection[0].id, "search_route")


if __name__ == "__main__":
    unittest.main()
