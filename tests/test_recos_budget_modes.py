import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from unittest import mock

from radar_jobs import recos


class TestsBudgetRecos(unittest.TestCase):
    """Vérifie le budget quotidien partagé par les deux modes RECOS."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.budget_path = os.path.join(self.tmp.name, "budget.json")
        self.candidats_path = os.path.join(self.tmp.name, "candidats.json")
        self.playlist_path = os.path.join(self.tmp.name, "playlist.json")
        self.candidats_decouverte_path = os.path.join(
            self.tmp.name, "candidats_decouverte.json"
        )
        self.playlist_decouverte_path = os.path.join(
            self.tmp.name, "playlist_decouverte.json"
        )

        self.patcheurs = [
            mock.patch.object(
                recos, "RECOS_SEARCH_BUDGET_PATH", self.budget_path
            ),
            mock.patch.object(
                recos, "RECOS_CANDIDATES_PATH", self.candidats_path
            ),
            mock.patch.object(
                recos, "RECOS_PLAYLIST_PATH", self.playlist_path
            ),
            mock.patch.object(
                recos,
                "RECOS_CANDIDATES_DECOUVERTE_PATH",
                self.candidats_decouverte_path,
            ),
            mock.patch.object(
                recos,
                "RECOS_PLAYLIST_DECOUVERTE_PATH",
                self.playlist_decouverte_path,
            ),
        ]
        for patcheur in self.patcheurs:
            patcheur.start()
            self.addCleanup(patcheur.stop)

        self.maintenant = datetime.now(recos.YT_QUOTA_TZ)
        patch_datetime = mock.patch.object(recos, "datetime")
        self.mock_datetime = patch_datetime.start()
        self.addCleanup(patch_datetime.stop)
        self.mock_datetime.now.return_value = self.maintenant

        self.etat = {
            self.candidats_path: [],
            self.playlist_path: [],
            self.candidats_decouverte_path: [],
            self.playlist_decouverte_path: [],
        }
        self.ecrire_fichier(self.candidats_path, [])
        self.ecrire_fichier(self.playlist_path, [])
        self.ecrire_fichier(self.candidats_decouverte_path, [])
        self.ecrire_fichier(self.playlist_decouverte_path, [])

        def charger(chemin, defaut):
            return deepcopy(self.etat.get(chemin, defaut))

        def enregistrer(chemin, donnees):
            donnees_copie = deepcopy(donnees)
            self.etat[chemin] = donnees_copie
            self.ecrire_fichier(chemin, donnees_copie)

        patch_load = mock.patch.object(recos, "load_json", side_effect=charger)
        self.mock_load_json = patch_load.start()
        self.addCleanup(patch_load.stop)

        patch_save = mock.patch.object(recos, "save_json", side_effect=enregistrer)
        self.mock_save_json = patch_save.start()
        self.addCleanup(patch_save.stop)

    def ecrire_fichier(self, chemin, donnees):
        with open(chemin, "w", encoding="utf-8") as fichier:
            json.dump(donnees, fichier, ensure_ascii=False)

    def ecrire_budget(self, donnees):
        self.etat[self.budget_path] = deepcopy(donnees)
        self.ecrire_fichier(self.budget_path, donnees)

    def aujourdhui(self):
        return self.maintenant.date().isoformat()

    def test_fichier_absent_initialise_le_budget(self):
        """Un fichier absent remet le budget à zéro et l'allocation vaut 80."""
        self.assertFalse(os.path.exists(self.budget_path))
        self.assertEqual(recos._recos_budget_today(), (0, {}))
        self.assertEqual(recos._recos_searches_used_today(), 0)
        self.assertEqual(recos._recos_searches_allowance("approfondir"), 80)

    def test_ancien_format_sans_by_mode_est_relu(self):
        """L'ancien format journalier reste compatible avec la répartition vide."""
        self.ecrire_budget({"date": self.aujourdhui(), "count": 30})

        self.assertEqual(recos._recos_budget_today(), (30, {}))
        self.assertEqual(recos._recos_searches_used_today(), 30)

    def test_compteur_d_un_autre_jour_est_remis_a_zero(self):
        """Un compteur datant d'un autre jour ne compte pas pour la journée courante."""
        self.ecrire_budget({
            "date": (self.maintenant - timedelta(days=1)).date().isoformat(),
            "count": 30,
        })

        self.assertEqual(recos._recos_budget_today(), (0, {}))
        self.assertEqual(recos._recos_searches_used_today(), 0)

    def test_enregistrement_repartit_le_compteur_par_mode(self):
        """Chaque recherche incrémente le total et la répartition du mode."""
        recos._recos_searches_record(5, "decouverte")
        recos._recos_searches_record(3, "approfondir")

        self.assertEqual(
            self.etat[self.budget_path],
            {
                "date": self.aujourdhui(),
                "count": 8,
                "by_mode": {
                    "decouverte": 5,
                    "approfondir": 3,
                },
            },
        )

    def test_enregistrement_zero_ne_modifie_le_fichier(self):
        """Un enregistrement nul ne déclenche aucune écriture."""
        recos._recos_searches_record(0)

        self.mock_save_json.assert_not_called()
        self.assertFalse(os.path.exists(self.budget_path))

    def test_allowance_limitee_par_la_reservation_du_mode(self):
        """Une file Découverte active conserve la moitié réservée à chaque mode."""
        self.ecrire_budget({
            "date": self.aujourdhui(),
            "count": 40,
            "by_mode": {"approfondir": 40},
        })
        self.etat[self.candidats_decouverte_path] = [{"id": 1}]

        self.assertEqual(recos._recos_searches_allowance("approfondir"), 0)

    def test_allowance_recupere_le_reliquat_du_mode_inactif(self):
        """Une file Découverte vide permet à Approfondir de récupérer son reliquat."""
        self.ecrire_budget({
            "date": self.aujourdhui(),
            "count": 40,
            "by_mode": {"approfondir": 40},
        })
        self.etat[self.candidats_decouverte_path] = []

        self.assertEqual(recos._recos_searches_allowance("approfondir"), 40)

    def test_allowance_est_plafonnee_par_le_compteur_total(self):
        """L'allocation ne dépasse jamais le budget total restant."""
        self.ecrire_budget({
            "date": self.aujourdhui(),
            "count": 78,
            "by_mode": {"approfondir": 10, "decouverte": 68},
        })
        self.etat[self.candidats_decouverte_path] = [{"id": 1}]

        self.assertEqual(recos._recos_searches_allowance("approfondir"), 2)

    def test_allowance_ne_devient_pas_negative(self):
        """Un budget déjà consommé au-delà du plafond produit zéro."""
        self.ecrire_budget({
            "date": self.aujourdhui(),
            "count": 85,
            "by_mode": {"approfondir": 85},
        })
        self.etat[self.candidats_decouverte_path] = [{"id": 1}]

        self.assertEqual(recos._recos_searches_allowance("approfondir"), 0)

    def test_mode_paths_lit_les_constantes_a_le_nom_moment(self):
        """La résolution des chemins utilise les constantes patchées à l'appel."""
        self.assertEqual(
            recos._mode_paths("decouverte"),
            (
                self.candidats_decouverte_path,
                self.playlist_decouverte_path,
            ),
        )
        self.assertEqual(
            recos._mode_paths("xyz"),
            (self.candidats_path, self.playlist_path),
        )
