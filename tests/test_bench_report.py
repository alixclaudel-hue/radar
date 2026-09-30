import unittest
from scripts.bench.report import note_item, note_vitesse, aggregate

class TestBenchReport(unittest.TestCase):
    def test_note_item_nominal(self):
        note, non_note = note_item(conformite=1.0, execution=1.0, pertinence=10.0, erreur=False)
        self.assertEqual(note, 100.0)
        self.assertFalse(non_note)

    def test_note_item_partiel(self):
        note, non_note = note_item(conformite=0.5, execution=0.5, pertinence=5.0, erreur=False)
        self.assertEqual(note, 50.0)
        self.assertFalse(non_note)

    def test_note_item_pertinence_none(self):
        note, non_note = note_item(conformite=1.0, execution=1.0, pertinence=None, erreur=False)
        self.assertTrue(non_note)
        self.assertGreater(note, 0.0)

    def test_note_item_erreur(self):
        note, non_note = note_item(conformite=1.0, execution=1.0, pertinence=10.0, erreur=True)
        self.assertEqual(note, 0.0)
        self.assertFalse(non_note)

    def test_note_vitesse_t_egal_tmin(self):
        val = note_vitesse(t=5.0, tmin=5.0, tmax=10.0)
        self.assertEqual(val, 20.0)

    def test_note_vitesse_t_egal_tmax(self):
        val = note_vitesse(t=10.0, tmin=5.0, tmax=10.0)
        self.assertEqual(val, 0.0)

    def test_note_vitesse_tmin_egal_tmax(self):
        val = note_vitesse(t=5.0, tmin=5.0, tmax=5.0)
        self.assertEqual(val, 20.0)

    def test_note_vitesse_intermediaire(self):
        val = note_vitesse(t=10.0, tmin=1.0, tmax=100.0)
        self.assertEqual(val, 10.0)

    def test_aggregate_classement(self):
        items = [
            {"id": "1", "model": "rapide_bon", "conformite": 1.0, "execution": 1.0, "latence_s": 2.0, "rc": 0},
            {"id": "2", "model": "lent_mauvais", "conformite": 0.1, "execution": 0.1, "latence_s": 10.0, "rc": 0}
        ]
        grades = {
            "1": {"pertinence": 10.0},
            "2": {"pertinence": 1.0}
        }
        model_summary, processed_items = aggregate(items, grades)
        self.assertEqual(model_summary[0]["modele"], "rapide_bon")
        self.assertEqual(model_summary[1]["modele"], "lent_mauvais")

    def test_aggregate_liste_vide(self):
        model_summary, processed_items = aggregate([], {})
        self.assertEqual(model_summary, [])
        self.assertEqual(processed_items, [])

if __name__ == "__main__":
    unittest.main()
