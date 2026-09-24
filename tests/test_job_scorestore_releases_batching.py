"""Correctif famine scorestore du 24/09/2026 (cf. CLAUDE.md) : job_scorestore_releases
scorait TOUS les labels suivis d'un coup, monopolisant le worker plusieurs minutes
pour un utilisateur avec beaucoup de labels (ex. 561, mesuré en prod ~17-23 min
d'affilée). Vérifie :

(1) un lancement ne traite qu'un lot de SCORESTORE_RELEASES_BATCH_PER_RUN labels au
    plus (pas la totalité) ;
(2) la reprise par rotation (curseur cumulatif persisté dans scorestore_meta) couvre
    bien tous les labels sur plusieurs lancements, sans en sauter aucun ;
(3) le job se rechaîne LUI-MÊME (priority=0) tant que le tour de labels n'est pas
    complet, et ne chaîne scorestore_tracks qu'une fois le tour terminé ;
(4) les sites de chaînage automatique (scorestore_tracks, scorestore_releases)
    passent bien priority=0 à jobs.launch() — sans quoi ils héritent du défaut
    interactif (1) et doublent à tort les jobs de fond d'autres utilisateurs déjà en
    file (bug confirmé par les logs de prod : scorestore_tracks passait toujours
    devant les scorestore_releases d'autres comptes déjà en attente) ;
(5) cas limites : aucun label suivi, curseur corrompu en base.

Base scorestore SQLite synthétique dans un dossier temporaire, Ctx et discogs_dump
patchés : aucun accès au vrai /data, aucun calcul de scoring réel, aucun appel
réseau.

Lancer : python3 -m unittest tests.test_job_scorestore_releases_batching -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crate_jobs  # noqa: E402
from radar_web.radar import discogs_dump, scorestore  # noqa: E402
from radar_web.radar import jobs as job_queue  # noqa: E402
from radar_web.radar import scoring  # noqa: E402

from tests.test_job_publish_recos import FakeJob  # noqa: E402


def _row(release_id, label):
    return {"id": release_id, "artist": "Artist", "title": f"Title {release_id}",
            "label": label, "year": 2020, "styles": "House"}


class FakeCtx:
    """Ctx factice : score fixe, sans producer_graph.json ni calcul d'affinité réel."""

    def __init__(self, uid=None):
        pass

    def album_score(self, meta):
        return 50, {}


class ScorestoreReleasesBatchingTestCase(unittest.TestCase):
    LABELS = [f"label{i:02d}" for i in range(4)]  # multiple exact du lot (2) -> 2 tours nets

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "scorestore.sqlite3")
        self._searched = []
        self.cfg = {"label_categories": {"1": self.LABELS[:2], "2": self.LABELS[2:]},
                    "scoring": {"scorestore": {"releases_batch_per_run": 2}}}

        patchers = [
            mock.patch.object(crate_jobs, "cfg_load", side_effect=lambda: self.cfg),
            mock.patch.object(scorestore, "db_path", lambda uid: self.db),
            mock.patch.object(discogs_dump, "available", return_value=True),
            mock.patch.object(discogs_dump, "search_local", side_effect=self._fake_search_local),
            mock.patch.object(scoring, "Ctx", FakeCtx),
            mock.patch.object(job_queue, "load_queue", return_value=[]),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        launch_patcher = mock.patch.object(job_queue, "launch")
        self.launch = launch_patcher.start()
        self.addCleanup(launch_patcher.stop)

    def _fake_search_local(self, label_keys, limit, vinyl_only):
        lk = label_keys[0]
        self._searched.append(lk)
        return [_row(int(lk[-2:]), lk)]  # release_id INTEGER PRIMARY KEY (scorestore.py:51)

    def _run(self):
        job = FakeJob()
        crate_jobs.job_scorestore_releases(job, {})
        return job

    def _cursor(self):
        con = scorestore.open_db("owner")
        try:
            return scorestore.read_run_meta(con).get("label_cursor")
        finally:
            con.close()

    def test_un_lancement_ne_traite_qu_un_lot(self):
        job = self._run()
        self.assertEqual(len(job.ticks), 2)  # lot=2 -> 2 labels -> 2 rows scorées
        self.assertIn("2/4", job.finished)
        self.assertIn("Lot partiel", job.finished)

    def test_le_lot_tronque_se_rechaine_lui_meme_en_priorite_fond(self):
        self._run()
        names = [c.args[0] for c in self.launch.call_args_list]
        self.assertEqual(names, ["scorestore_releases"])
        self.assertEqual(self.launch.call_args.kwargs.get("priority"), 0)
        self.assertEqual(self._cursor(), "2")

    def test_reprise_par_rotation_couvre_tous_les_labels_sans_en_sauter(self):
        self._run()
        self._run()
        self.assertEqual(sorted(self._searched), sorted(self.LABELS))
        self.assertEqual(len(self._searched), len(self.LABELS))  # aucune répétition (4 = 2x2, multiple exact)

    def test_tour_complet_chaine_scorestore_tracks_en_priorite_fond(self):
        self._run()
        self._run()
        names = [c.args[0] for c in self.launch.call_args_list]
        self.assertEqual(names, ["scorestore_releases", "scorestore_tracks"])
        self.assertEqual(self.launch.call_args.kwargs.get("priority"), 0)

    def test_troisieme_lancement_repart_du_debut_du_tour_suivant(self):
        self._run()
        self._run()
        self._searched.clear()
        self._run()
        self.assertEqual(self._searched, self.LABELS[:2])

    def test_aucun_label_suivi_chaine_directement_scorestore_tracks(self):
        self.cfg = {"label_categories": {"1": [], "2": []}}
        job = self._run()
        names = [c.args[0] for c in self.launch.call_args_list]
        self.assertEqual(names, ["scorestore_tracks"])
        self.assertEqual(job.finished, "Aucun label suivi (Cœur ou Aimé) — rien à noter.")

    def test_curseur_corrompu_en_base_ne_plante_pas_et_repart_de_zero(self):
        con = scorestore.open_db("owner")
        scorestore.set_run_meta(con, {"label_cursor": "pas-un-entier"})
        con.close()
        job = self._run()  # ne lève pas malgré le curseur illisible
        self.assertIn("2/4", job.finished)
        self.assertEqual(self._searched, self.LABELS[:2])  # repart bien de l'index 0


class ChainedJobsPriorityTestCase(unittest.TestCase):
    """Les jobs chaînés automatiquement (pas un clic utilisateur) doivent passer
    priority=0 explicite à jobs.launch() — sinon ils héritent du défaut interactif
    (jobs.py: launch(..., priority=1)) et doublent à tort les jobs de fond d'autres
    utilisateurs déjà en file (bug confirmé par les logs de prod du 24/09)."""

    def setUp(self):
        load_queue_patcher = mock.patch.object(job_queue, "load_queue", return_value=[])
        load_queue_patcher.start()
        self.addCleanup(load_queue_patcher.stop)
        launch_patcher = mock.patch.object(job_queue, "launch")
        self.launch = launch_patcher.start()
        self.addCleanup(launch_patcher.stop)

    def test_chain_scorestore_tracks_priority_fond(self):
        crate_jobs._chain_scorestore_tracks()
        self.launch.assert_called_once_with(
            "scorestore_tracks", {}, uid=crate_jobs.RADAR_UID, priority=0)

    def test_chain_scorestore_releases_priority_fond(self):
        crate_jobs._chain_scorestore_releases()
        self.launch.assert_called_once_with(
            "scorestore_releases", {}, uid=crate_jobs.RADAR_UID, priority=0)


if __name__ == "__main__":
    unittest.main()
