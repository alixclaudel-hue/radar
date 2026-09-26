"""Tests unitaires pour scripts/ai/locate.py — mode `search`.

Couvre en particulier le repli tokenisé (`extraire_termes` + `_score_repli`,
branché dans `chercher`) : sans lui, une requête en langage naturel ou une
requête avec alternatives ('a OR b', 'a|b') rend systématiquement zéro
résultat, même quand le fichier cherché existe — observé en session (mobile,
25/09) sur `ai_broker.py --mode search`.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.ai import locate  # noqa: E402


class ExtraireTermesTests(unittest.TestCase):
    """Tokenisation de la requête pour le repli OR."""

    def test_requete_vide_rend_liste_vide(self):
        self.assertEqual(locate.extraire_termes(""), [])
        self.assertEqual(locate.extraire_termes(None), [])

    def test_mot_unique_rend_ce_mot(self):
        self.assertEqual(locate.extraire_termes("community"), ["community"])

    def test_separateur_or_insensible_a_la_casse(self):
        self.assertEqual(
            locate.extraire_termes("num_have OR num_want or community"),
            ["num_have", "num_want", "community"],
        )

    def test_separateur_pipe_echappe_shell(self):
        """Le `\\|` d'un shell arrive tel quel (backslash + pipe) : le pipe seul sépare."""
        termes = locate.extraire_termes(
            r"def release_meta\|def _release_dict\|def search_run\|rating"
        )
        self.assertEqual(termes, ["def", "release_meta", "release_dict", "search_run", "rating"])

    def test_question_langage_naturel_filtre_mots_vides_et_courts(self):
        termes = locate.extraire_termes("Où est construit le dictionnaire d'une release ?")
        self.assertNotIn("où", [t.lower() for t in termes])
        self.assertNotIn("le", [t.lower() for t in termes])
        self.assertIn("construit", termes)
        self.assertIn("dictionnaire", termes)
        self.assertIn("release", termes)

    def test_deduplique_en_conservant_ordre_premiere_apparition(self):
        termes = locate.extraire_termes("release release RELEASE community")
        self.assertEqual(termes, ["release", "community"])

    def test_ponctuation_de_bordure_retiree(self):
        termes = locate.extraire_termes("(title), (year), thumb.")
        self.assertEqual(termes, ["title", "year", "thumb"])

    def test_termes_de_moins_de_trois_caracteres_ignores(self):
        termes = locate.extraire_termes("un id ok community")
        self.assertEqual(termes, ["community"])


class ChercherRepliTests(unittest.TestCase):
    """`chercher()` : le repli ne s'active que si la requête littérale échoue."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = self.tmpdir.name
        self._write("release_meta.py", "def release_meta():\n    return community.rating\n")
        self._write("unrelated.py", "def other():\n    pass\n")

    def _write(self, name, content):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def test_requete_litterale_qui_matche_ne_declenche_pas_le_repli(self):
        resultat = locate.chercher("release_meta.py", roots=[self.root])
        self.assertTrue(resultat["hits"])
        self.assertNotIn("fallback", resultat)

    def test_requete_or_sans_correspondance_litterale_utilise_le_repli(self):
        resultat = locate.chercher(
            "num_have OR num_want OR community", roots=[self.root]
        )
        self.assertTrue(resultat["hits"])
        self.assertTrue(resultat.get("fallback"))
        self.assertEqual(resultat["hits"][0]["path"], locate._nettoyer(
            os.path.join(self.root, "release_meta.py")
        ))

    def test_requete_langage_naturel_utilise_le_repli(self):
        resultat = locate.chercher(
            "Où est construit le dictionnaire community rating ?", roots=[self.root]
        )
        self.assertTrue(resultat.get("fallback"))
        chemins = [hit["path"] for hit in resultat["hits"]]
        self.assertIn(
            locate._nettoyer(os.path.join(self.root, "release_meta.py")), chemins
        )

    def test_requete_sans_aucune_correspondance_meme_au_repli(self):
        resultat = locate.chercher("zzz_totalement_inexistant_zzz", roots=[self.root])
        self.assertEqual(resultat["hits"], [])

    def test_repli_non_relance_si_terme_unique_identique_a_la_requete(self):
        """Pas de second parcours quand la tokenisation ne change rien à faire."""
        resultat = locate.chercher("zzz_totalement_inexistant_zzz", roots=[self.root])
        self.assertNotIn("fallback", resultat)

    def test_repli_respecte_troncature_si_deja_tronque(self):
        resultat = locate.chercher(
            "num_have OR community", roots=[self.root], max_fichiers=1
        )
        self.assertTrue(resultat["truncated"])
        self.assertNotIn("fallback", resultat)


if __name__ == "__main__":
    unittest.main()
