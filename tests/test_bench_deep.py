import unittest
import json
import re
from unittest.mock import patch, MagicMock
from scripts.bench.deep import get_epreuve, EPREUVES

class TestBenchDeep(unittest.TestCase):

    def test_get_epreuve_nominal(self):
        epreuve = get_epreuve('debug_multi_bugs')
        self.assertEqual(epreuve.id, 'debug_multi_bugs')

    def test_get_epreuve_erreur(self):
        with self.assertRaises(ValueError):
            get_epreuve('inconnue')

    def test_debug_multi_bugs_build_prompt(self):
        epreuve = get_epreuve('debug_multi_bugs')
        p1 = epreuve.build_prompt(1)
        p2 = epreuve.build_prompt(2)
        p3 = epreuve.build_prompt(3)
        self.assertNotEqual(p1, p2)
        self.assertNotEqual(p2, p3)
        self.assertNotEqual(p1, p3)
        with self.assertRaises(ValueError):
            epreuve.build_prompt(4)

    def test_debug_multi_bugs_niveau_invalide(self):
        epreuve = get_epreuve('debug_multi_bugs')
        with self.assertRaises(ValueError):
            epreuve.build_prompt(4)

    def test_debug_multi_bugs_score_sans_code(self):
        epreuve = get_epreuve('debug_multi_bugs')
        score = epreuve.score('texte seul sans code python', 1)
        self.assertEqual(score.execution, 0.0)

    def test_debug_multi_bugs_score_reference(self):
        epreuve = get_epreuve('debug_multi_bugs')
        ref_code = '```python\ndef planifier(taches, capacite):\n    if capacite <= 0:\n        raise ValueError("capacité invalide")\n    jours, courant, charge = [], [], 0\n    for nom, duree in taches:\n        if duree > capacite:\n            raise ValueError(f"tâche trop longue : {nom}")\n        if charge + duree > capacite:\n            jours.append(courant)\n            courant, charge = [], 0\n        courant.append(nom)\n        charge += duree\n    if courant:\n        jours.append(courant)\n    return jours\n```'
        for niveau in (1, 2, 3):
            score = epreuve.score(ref_code, niveau)
            self.assertEqual(score.execution, 1.0)
            self.assertGreaterEqual(score.conformite, 0.95)

    def test_diagnostic_log_build_prompt(self):
        epreuve = get_epreuve('diagnostic_log')
        prompt = epreuve.build_prompt(1)
        self.assertIn('cause_racine', prompt)

    def test_diagnostic_log_score_parfait(self):
        epreuve = get_epreuve('diagnostic_log')
        niveau = 1
        prompt = epreuve.build_prompt(niveau)
        preuves = [int(l[:2]) for l in prompt.split(chr(10)) if re.match(r'\d\d\| ', l) and ('429' in l or 'cooldown' in l)]
        payload = {
            "cause_racine": "quota 429 puis cooldown",
            "preuves": preuves,
            "correctif": "espacer les appels et limiter le parallélisme",
            "risque": "plus lent"
        }
        reponse_parfaite = json.dumps(payload)
        score = epreuve.score(reponse_parfaite, niveau)
        self.assertEqual(score.conformite, 1.0)
        self.assertGreaterEqual(score.execution, 0.95)

    def test_diagnostic_log_score_erreur_juge(self):
        epreuve = get_epreuve('diagnostic_log')
        reponse_err = '{"cause_racine": "503 du juge", "preuves": [], "correctif": "", "risque": ""}'
        score = epreuve.score(reponse_err, 3)
        self.assertLess(score.execution, 0.6)

    def test_diagnostic_log_extraction_tolerante(self):
        epreuve = get_epreuve('diagnostic_log')
        niveau = 1
        prompt = epreuve.build_prompt(niveau)
        preuves = [int(l[:2]) for l in prompt.split(chr(10)) if re.match(r'\d\d\| ', l) and ('429' in l or 'cooldown' in l)]
        payload = {
            "cause_racine": "quota 429 puis cooldown",
            "preuves": preuves,
            "correctif": "espacer les appels et limiter le parallélisme",
            "risque": "plus lent"
        }
        reponse_bruit = 'Voici : ' + json.dumps(payload) + ' fin'
        score = epreuve.score(reponse_bruit, niveau)
        self.assertLess(score.conformite, 1.0)
        self.assertGreaterEqual(score.execution, 0.9)

if __name__ == '__main__':
    unittest.main()
