"""Notation des ouvriers : le cas « lit sans écrire » doit être pénalisé, un chantier
abouti bien noté, et le fichier de scores rester borné."""
import json
import os
import tempfile
import unittest

from scripts.ai import worker_score as ws


def _rapport(**kw):
    base = {"statut": "borne", "arret": "etapes", "fichiers_modifies": [], "passent": [],
            "restent_rouges": [], "refus": [], "erreurs": [],
            "fournisseur": "openrouter", "modele": "m1", "etapes": 12}
    base.update(kw)
    return base


class NoterTestCase(unittest.TestCase):
    def test_lit_sans_ecrire_plafonne_a_10(self):
        self.assertLessEqual(ws.noter(_rapport())["score"], 10)

    def test_chantier_abouti(self):
        n = ws.noter(_rapport(statut="termine", arret=None, fichiers_modifies=["a.py"],
                              passent=["py_compile a.py"]))
        self.assertEqual(n["score"], 85)

    def test_commande_rouge_retire_le_bonus(self):
        n = ws.noter(_rapport(statut="termine", arret=None, fichiers_modifies=["a.py"],
                              passent=["x"], restent_rouges=[{"commande": "y", "code": 1}]))
        self.assertEqual(n["score"], 75)

    def test_malus_erreurs_plafonne(self):
        n = ws.noter(_rapport(statut="termine", arret=None, fichiers_modifies=["a.py"],
                              erreurs=["e"] * 10))
        self.assertEqual(n["score"], 35)


class MoyennesTestCase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _noter(self, modele, score, n=1):
        for _ in range(n):
            ws.enregistrer(_rapport(modele=modele), {"score": score}, self.d)

    def test_un_seul_essai_ne_condamne_pas(self):
        self._noter("m1", 0)
        self.assertEqual(ws.mauvais(self.d), set())

    def test_modele_mauvais_apres_deux_echecs(self):
        self._noter("m1", 0, 2)
        self.assertEqual(ws.mauvais(self.d), {"openrouter:m1"})

    def test_meilleur_prefere_un_bon_modele_hors_exclus(self):
        self._noter("bon", 90, 2)
        self._noter("moyen", 55, 2)
        self.assertEqual(ws.meilleur(dossier=self.d), "openrouter:bon")
        self.assertEqual(ws.meilleur({"openrouter:bon"}, self.d), "openrouter:moyen")

    def test_aucun_bon_modele_laisse_le_routeur(self):
        self._noter("faible", 40, 3)
        self.assertIsNone(ws.meilleur(dossier=self.d))

    def test_fichier_borne_par_rotation(self):
        chemin = os.path.join(self.d, ws.NOM_FICHIER)
        ancien = ws.MAX_OCTETS
        ws.MAX_OCTETS = 2000
        try:
            self._noter("m1", 50, 200)
        finally:
            ws.MAX_OCTETS = ancien
        with open(chemin, encoding="utf-8") as f:
            self.assertLessEqual(len(f.readlines()), ws.GARDER_LIGNES)

    def test_dernier_modele_lu_dans_les_recus(self):
        with open(os.path.join(self.d, "gemini-receipts.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"source": "ai_broker", "ts": 100.0, "provider": "gemini",
                                "model": "g-2"}) + "\n")
        self.assertEqual(ws.dernier_modele_courtier(50.0, self.d), ("gemini", "g-2"))
        self.assertEqual(ws.dernier_modele_courtier(500.0, self.d), (None, None))


if __name__ == "__main__":
    unittest.main()
