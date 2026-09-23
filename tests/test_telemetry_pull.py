import importlib.util
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Chargement dynamique du module scripts/telemetry_pull.py sans être un paquet
_module_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts", "telemetry_pull.py")
)
_spec = importlib.util.spec_from_file_location("telemetry_pull", _module_path)
telemetry_pull = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(telemetry_pull)


class TestTelemetryPull(unittest.TestCase):
    """Suite de tests pour le module telemetry_pull."""

    def setUp(self):
        """Configuration initiale pour chaque test."""
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.local_path = os.path.join(self.tmp_dir.name, "test.jsonl")

    def tearDown(self):
        """Nettoyage après chaque test."""
        self.tmp_dir.cleanup()

    def test_ts_cas_nominal(self):
        """_ts renvoie la valeur flottante pour un JSON valide avec un horodatage numérique."""
        ligne = '{"ts": 1234567890.12, "msg": "ok"}'
        self.assertEqual(telemetry_pull._ts(ligne), 1234567890.12)

    def test_ts_ligne_vide(self):
        """_ts renvoie None sur une ligne vide ou blanche."""
        self.assertIsNone(telemetry_pull._ts(""))
        self.assertIsNone(telemetry_pull._ts("   "))

    def test_ts_json_invalide(self):
        """_ts renvoie None sur un JSON invalide."""
        self.assertIsNone(telemetry_pull._ts('{"ts": 12345,'))

    def test_ts_sans_champ_ts(self):
        """_ts renvoie None sur un objet JSON sans clé 'ts'."""
        self.assertIsNone(telemetry_pull._ts('{"msg": "pas de ts"}'))

    def test_ts_non_numerique(self):
        """_ts renvoie None si la valeur de 'ts' n'est pas convertible en float."""
        self.assertIsNone(telemetry_pull._ts('{"ts": "non-numerique"}'))
        self.assertIsNone(telemetry_pull._ts('{"ts": null}'))

    def test_max_ts_fichier_absent(self):
        """max_ts renvoie None si le fichier n'existe pas."""
        chemin_inexistant = os.path.join(self.tmp_dir.name, "inexistant.jsonl")
        self.assertIsNone(telemetry_pull.max_ts(chemin_inexistant))

    def test_max_ts_fichier_vide(self):
        """max_ts renvoie None sur un fichier vide."""
        open(self.local_path, "w", encoding="utf-8").close()
        self.assertIsNone(telemetry_pull.max_ts(self.local_path))

    def test_max_ts_non_trie(self):
        """max_ts renvoie le plus grand ts même si les lignes ne sont pas triées."""
        contenu = '{"ts": 100}\n{"ts": 300}\n{"ts": 200}\n'
        with open(self.local_path, "w", encoding="utf-8") as f:
            f.write(contenu)
        self.assertEqual(telemetry_pull.max_ts(self.local_path), 300.0)

    def test_merge_fichier_absent(self):
        """merge sur un fichier local absent écrit toutes les lignes entrantes triées par ts croissant."""
        entrant = '{"ts": 200}\n{"ts": 100}\n{"ts": 150}\n'
        nb = telemetry_pull.merge(self.local_path, entrant)
        self.assertEqual(nb, 3)

        with open(self.local_path, "r", encoding="utf-8") as f:
            lignes = f.readlines()

        self.assertEqual(len(lignes), 3)
        self.assertIn("100", lignes[0])
        self.assertIn("150", lignes[1])
        self.assertIn("200", lignes[2])

    def test_merge_filtre_et_compte(self):
        """merge n'ajoute que les lignes de ts strictement supérieur au maximum local et renvoie leur nombre."""
        # Initialisation avec un max_ts à 100
        with open(self.local_path, "w", encoding="utf-8") as f:
            f.write('{"ts": 100}\n')

        entrant = '{"ts": 50}\n{"ts": 100}\n{"ts": 150}\n{"ts": 200}\n'
        nb = telemetry_pull.merge(self.local_path, entrant)
        self.assertEqual(nb, 2)  # Seuls 150 et 200 sont strictement > 100

        with open(self.local_path, "r", encoding="utf-8") as f:
            lignes = f.readlines()
        self.assertEqual(len(lignes), 3)

    def test_merge_idempotence_doublons(self):
        """merge appelé deux fois avec le même texte entrant n'ajoute rien la seconde fois (anti-doublons)."""
        entrant = '{"ts": 100}\n{"ts": 200}\n'
        nb1 = telemetry_pull.merge(self.local_path, entrant)
        self.assertEqual(nb1, 2)

        # Deuxième appel avec le même texte entrant
        nb2 = telemetry_pull.merge(self.local_path, entrant)
        self.assertEqual(nb2, 0)

        with open(self.local_path, "r", encoding="utf-8") as f:
            lignes = f.readlines()
        self.assertEqual(len(lignes), 2)

    def test_merge_sans_saut_de_ligne_final(self):
        """merge sur un fichier local dont la dernière ligne n'a pas de saut de ligne final ne colle pas deux enregistrements ensemble."""
        # Fichiers local sans newline à la fin
        with open(self.local_path, "w", encoding="utf-8") as f:
            f.write('{"ts": 100}')

        entrant = '{"ts": 200}\n'
        nb = telemetry_pull.merge(self.local_path, entrant)
        self.assertEqual(nb, 1)

        with open(self.local_path, "r", encoding="utf-8") as f:
            contenu = f.read()

        # On s'assure qu'un saut de ligne a été inséré proprement entre l'ancien et le nouveau
        self.assertEqual(contenu, '{"ts": 100}\n{"ts": 200}\n')

    def test_merge_texte_entrant_vide(self):
        """merge renvoie 0 sur texte entrant vide ou blanc sans modifier le fichier."""
        with open(self.local_path, "w", encoding="utf-8") as f:
            f.write('{"ts": 100}\n')

        self.assertEqual(telemetry_pull.merge(self.local_path, ""), 0)
        self.assertEqual(telemetry_pull.merge(self.local_path, "   \n   "), 0)

    def test_merge_nettoyage_fichiers_temporaires(self):
        """merge ne laisse aucun fichier temporaire dans le dossier de destination."""
        entrant = '{"ts": 100}\n'
        telemetry_pull.merge(self.local_path, entrant)

        dossier = os.path.dirname(self.local_path)
        fichiers = os.listdir(dossier)

        # Seul le fichier local final doit être présent
        self.assertEqual(fichiers, [os.path.basename(self.local_path)])

    @patch("subprocess.run")
    def test_read_branch_file_echec(self, mock_run):
        """read_branch_file renvoie une chaîne vide quand git show échoue."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "erreur git"
        mock_run.return_value = mock_result

        resultat = telemetry_pull.read_branch_file("/repo", "ref", "telemetry.jsonl")
        self.assertEqual(resultat, "")
        mock_run.assert_called_once()

    def test_pull_sans_fetch(self):
        """pull avec fetch=False n'appelle pas run et fusionne bien les deux fichiers."""
        # Le module est chargé dynamiquement : il n'est pas dans sys.modules,
        # donc patch.object et non une cible désignée par son nom.
        with patch.object(telemetry_pull, "run") as mock_run, \
                patch.object(telemetry_pull, "read_branch_file",
                             return_value='{"ts": 500}\n'):
            resultat = telemetry_pull.pull(
                repo_dir="/repo",
                dest_dir=self.tmp_dir.name,
                branch="telemetry",
                names=["telemetry.jsonl", "gemini-receipts.jsonl"],
                fetch=False
            )
            mock_run.assert_not_called()

        self.assertEqual(resultat, {"telemetry.jsonl": 1,
                                    "gemini-receipts.jsonl": 1})
        for name in ("telemetry.jsonl", "gemini-receipts.jsonl"):
            with open(os.path.join(self.tmp_dir.name, name),
                      encoding="utf-8") as f:
                self.assertIn("500", f.read())


if __name__ == "__main__":
    unittest.main()
