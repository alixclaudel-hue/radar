"""Tests unitaires pour le module radar_jobs.recos_diversity.

La suite est entièrement hors ligne : le module testé est purement
calculatoire — aucun appel réseau, aucun accès disque, aucune lecture
d'horloge. Les seuls patchs présents servent à vérifier cette absence
d'effets de bord ; aucun secret ni service externe n'est requis.
"""

import builtins
import socket
import unittest
from unittest import mock

from radar_jobs.recos_diversity import DiversityTracker, dominant_reason, norm_key


class NormKeyTests(unittest.TestCase):
    """Normalisation des clés artiste et label."""

    def test_casse_et_espaces_multiples_equivalents(self):
        # Casse différente et espaces multiples : même clé normalisée.
        self.assertEqual(norm_key('Daft Punk'), 'daft punk')
        self.assertEqual(norm_key('  DAFT   PUNK  '), 'daft punk')
        self.assertEqual(norm_key('Daft   Punk'), norm_key('daft punk'))
        self.assertEqual(norm_key('Daft\tPunk\n'), 'daft punk')

    def test_none_et_chaine_vide(self):
        # None et chaînes vides se normalisent vers la chaîne vide.
        self.assertEqual(norm_key(None), '')
        self.assertEqual(norm_key(''), '')
        self.assertEqual(norm_key('    '), '')


class QuotaArtisteTests(unittest.TestCase):
    """Quota par artiste (max_per_artist)."""

    def test_troisieme_piste_du_meme_artiste_refusee(self):
        tracker = DiversityTracker(max_per_artist=2, max_per_label=99)
        self.assertTrue(tracker.allows('Daft Punk', 'L1'))
        tracker.add('Daft Punk', 'L1')
        self.assertTrue(tracker.allows('Daft Punk', 'L2'))
        tracker.add('Daft Punk', 'L2')
        # 3e piste du même artiste : refusée (quota de 2 atteint).
        self.assertFalse(tracker.allows('Daft Punk', 'L3'))
        # Un autre artiste reste accepté.
        self.assertTrue(tracker.allows('Air', 'L3'))

    def test_existants_comptes_des_init(self):
        existing = [
            {'artist': 'Daft Punk', 'label': 'Label A'},
            {'artist': 'daft   punk', 'label': 'label a'},
        ]
        tracker = DiversityTracker(existing, max_per_artist=2, max_per_label=3)
        # L'artiste a déjà 2 occurrences : quota atteint dès l'initialisation.
        self.assertFalse(tracker.allows('DAFT PUNK', 'Autre'))

        tracker_label = DiversityTracker(existing, max_per_artist=5, max_per_label=2)
        # Le label a déjà 2 occurrences : quota de 2 atteint.
        self.assertFalse(tracker_label.allows('Autre', ' LABEL   A '))

    def test_existants_sans_label(self):
        # Un élément existant sans clé 'label' ne doit pas planter.
        tracker = DiversityTracker([{'artist': 'A'}], max_per_artist=1, max_per_label=1)
        self.assertFalse(tracker.allows('a', 'Nouveau'))

    def test_artiste_vide_jamais_limite(self):
        tracker = DiversityTracker(max_per_artist=1, max_per_label=1)
        for _ in range(5):
            self.assertTrue(tracker.allows('', 'nom label'))
            self.assertTrue(tracker.allows(None, 'autre label'))
            self.assertTrue(tracker.allows('   ', 'encore'))
            tracker.add('', '')
            tracker.add(None, None)
            tracker.add('   ', '\t')
        # Après plusieurs ajouts, un artiste vide reste toujours accepté.
        self.assertTrue(tracker.allows('', 'libre'))


class QuotaLabelTests(unittest.TestCase):
    """Quota par label (max_per_label)."""

    def test_quatrieme_piste_du_meme_label_refusee(self):
        tracker = DiversityTracker(max_per_artist=99, max_per_label=3)
        for i in range(3):
            artiste = 'Artiste %d' % i
            self.assertTrue(tracker.allows(artiste, 'Label X'))
            tracker.add(artiste, 'Label X')
        # 4e piste du même label : refusée (quota de 3 atteint).
        self.assertFalse(tracker.allows('Artiste 9', 'Label X'))
        # Un autre label reste accepté.
        self.assertTrue(tracker.allows('Artiste 9', 'Label Y'))

    def test_label_passe_en_liste(self):
        tracker = DiversityTracker(max_per_artist=99, max_per_label=1)
        tracker.add('Artiste A', ['  Label X  ', 'label y'])
        # Chaque label de la liste est compté séparément.
        self.assertFalse(tracker.allows('Artiste B', ['LABEL   X']))
        self.assertFalse(tracker.allows('Artiste B', 'label y'))
        self.assertTrue(tracker.allows('Artiste B', ['Label Z']))

    def test_label_en_liste_des_init(self):
        existing = [{'artist': 'A', 'label': ['Label X', 'Label Y']}]
        tracker = DiversityTracker(existing, max_per_artist=99, max_per_label=1)
        self.assertFalse(tracker.allows('B', 'label x'))
        self.assertFalse(tracker.allows('B', ['  label   y ']))
        self.assertTrue(tracker.allows('B', 'Label Z'))

    def test_label_vide_jamais_limite(self):
        tracker = DiversityTracker(max_per_artist=99, max_per_label=1)
        for i in range(5):
            artiste = 'Artiste %d' % i
            self.assertTrue(tracker.allows(artiste, ''))
            self.assertTrue(tracker.allows(artiste, None))
            self.assertTrue(tracker.allows(artiste, ['', '   ']))
            tracker.add(artiste, '')
            tracker.add(artiste, ['', None, '  '])
        # Après plusieurs ajouts de labels vides, rien n'est limité.
        self.assertTrue(tracker.allows('Artiste final', ''))

    def test_label_non_iterable_est_ignore(self):
        # Un label ni chaîne ni itérable ne lève pas d'exception.
        tracker = DiversityTracker(max_per_artist=99, max_per_label=1)
        self.assertTrue(tracker.allows('Artiste', 42))
        tracker.add('Artiste', 42)
        self.assertTrue(tracker.allows('Artiste', 42))


class RejectTests(unittest.TestCase):
    """Comptage des rejets."""

    def test_reject_incremente_rejected_count(self):
        tracker = DiversityTracker()
        self.assertEqual(tracker.rejected_count, 0)
        tracker.reject()
        self.assertEqual(tracker.rejected_count, 1)
        tracker.reject()
        tracker.reject()
        self.assertEqual(tracker.rejected_count, 3)

    def test_reject_n_influence_pas_les_compteurs(self):
        tracker = DiversityTracker(max_per_artist=1, max_per_label=1)
        tracker.reject()
        self.assertTrue(tracker.allows('Artiste', 'Label'))
        tracker.add('Artiste', 'Label')
        self.assertFalse(tracker.allows('Artiste', 'Label'))
        self.assertEqual(tracker.rejected_count, 1)


class DominantReasonTests(unittest.TestCase):
    """Détermination de la dimension dominante."""

    def test_chaque_dimension_dominante(self):
        self.assertEqual(dominant_reason(3.0, 1.0, 2.0), 'artist')
        self.assertEqual(dominant_reason(1.0, 3.0, 2.0), 'label')
        self.assertEqual(dominant_reason(1.0, 2.0, 3.0), 'style')

    def test_egalite_departagee_par_priorite(self):
        # À égalité, l'ordre métier artist > label > style s'applique.
        self.assertEqual(dominant_reason(2.0, 2.0, 1.0), 'artist')
        self.assertEqual(dominant_reason(1.0, 2.0, 2.0), 'label')
        self.assertEqual(dominant_reason(2.0, 2.0, 2.0), 'artist')

    def test_none_si_tout_none(self):
        self.assertIsNone(dominant_reason(None, None, None))

    def test_un_none_est_ignore(self):
        self.assertEqual(dominant_reason(1.0, None, None), 'artist')
        self.assertEqual(dominant_reason(None, 1.0, None), 'label')
        self.assertEqual(dominant_reason(None, None, 1.0), 'style')
        # Deux None ignorés : seule la dimension renseignée compte.
        self.assertEqual(dominant_reason(None, 0.4, 0.9), 'style')
        self.assertEqual(dominant_reason(None, 0.9, 0.4), 'label')

    def test_zero_et_valeurs_negatives(self):
        # Zéro est une valeur valide, pas une absence de valeur.
        self.assertEqual(dominant_reason(0.0, None, None), 'artist')
        self.assertEqual(dominant_reason(None, None, 0.0), 'style')
        self.assertEqual(dominant_reason(0.0, None, 0.0), 'artist')
        self.assertEqual(dominant_reason(-1.0, -2.0, -3.0), 'artist')


class AbsenceEffetsDeBordTests(unittest.TestCase):
    """Le module ne doit toucher ni au réseau, ni au disque, ni à l'horloge."""

    def test_aucun_acces_reseau_disque_ou_horloge(self):
        with mock.patch.object(socket, 'socket', side_effect=AssertionError('réseau interdit')), \
                mock.patch.object(builtins, 'open', side_effect=AssertionError('disque interdit')), \
                mock.patch('time.time', side_effect=AssertionError('horloge interdite')):
            tracker = DiversityTracker(
                [{'artist': 'A', 'label': 'L'}], max_per_artist=1, max_per_label=1
            )
            self.assertFalse(tracker.allows('A', 'L'))
            self.assertTrue(tracker.allows('B', 'M'))
            tracker.reject()
            self.assertEqual(tracker.rejected_count, 1)
            self.assertEqual(dominant_reason(0.1, None, 0.2), 'style')
            self.assertEqual(norm_key('  A   B '), 'a b')


if __name__ == '__main__':
    unittest.main()
