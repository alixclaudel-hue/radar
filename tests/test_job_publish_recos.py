"""BUG 1/2/3 du diagnostic VPS 16/09 (relecture des PR #158/#159) dans
`crate_jobs.job_publish_recos` :

  - BUG 1 : une exception non classée (RuntimeError d'une erreur API non
    reconnue, aléa réseau) sortait de la fonction SANS jamais persister
    `recos_candidates.json`/`recos_search_budget.json` -- perdant l'état des
    candidats et des recherches déjà faites dans le run.
  - BUG 2 : `QuotaExhausted` incrémentait `searched` alors qu'un rejet
    quota/rate ne consomme aucune unité Google -- gonflait le budget
    quotidien d'une fausse unité par tick tant que le quota restait épuisé.
  - BUG 3 : un `RateLimited` sur un candidat ne coupait pas le run -- chaque
    candidat suivant rejouait sa propre séquence de backoff (1+2+4s) contre
    une clé déjà connue limitée dans CE run.

Isole `job_publish_recos` de tout fichier réel en patchant les constantes de
chemin du module (jamais `CRATE_DATA_DIR`/le vrai `/data`) et en simulant
`ytcache.search_video_diag` -- pas d'appel réseau, pas de token.

Lancer : python3 -m unittest tests.test_job_publish_recos -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crate_jobs  # noqa: E402
from radar_web.radar import ytcache  # noqa: E402


class FakeJob:
    """Duck-type minimal de `crate_jobs.Job` : job_publish_recos ne touche
    jamais le disque directement via `job`, seulement via ses méthodes."""

    def __init__(self):
        self.st = {}
        self.messages = []
        self.ticks = []
        self.finished = None
        self._stopped = False

    def stopped(self):
        return self._stopped

    def tick(self, last="", inc=1, total=None):
        self.ticks.append(last)

    def msg(self, m):
        self.messages.append(m)

    def finish(self, message="", error=None):
        self.finished = message


class JobPublishRecosTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = self._tmp.name
        self.paths = {
            "RECOS_CANDIDATES_PATH": os.path.join(tmp, "recos_candidates.json"),
            "RECOS_HISTORY_PATH": os.path.join(tmp, "recos_playlist_history.json"),
            "RECOS_PLAYLIST_PATH": os.path.join(tmp, "recos_playlist.json"),
            "RECOS_SEARCH_BUDGET_PATH": os.path.join(tmp, "recos_search_budget.json"),
        }
        patchers = [mock.patch.object(crate_jobs, name, path)
                    for name, path in self.paths.items()]
        patchers.append(mock.patch.object(crate_jobs, "cfg_load", return_value={}))
        patchers.append(mock.patch.object(ytcache, "youtube_keys", return_value=["k"]))
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _candidates(self, n):
        return [{"artist": f"Artist {i}", "title": f"Track {i}", "label": "Label"}
                for i in range(n)]

    def _budget_count(self):
        d = crate_jobs.load_json(self.paths["RECOS_SEARCH_BUDGET_PATH"], {})
        return int(d.get("count", 0))

    def _remaining(self):
        return crate_jobs.load_json(self.paths["RECOS_CANDIDATES_PATH"], [])

    def test_bug2_quota_exhausted_ne_decompte_pas_le_budget(self):
        crate_jobs.save_json(self.paths["RECOS_CANDIDATES_PATH"], self._candidates(1))
        with mock.patch.object(ytcache, "search_video_diag",
                                side_effect=ytcache.QuotaExhausted("épuisé")):
            job = FakeJob()
            crate_jobs.job_publish_recos(job, {})
        self.assertEqual(self._budget_count(), 0)
        self.assertEqual(len(self._remaining()), 1)
        self.assertIn("Quota YouTube", job.messages[-1])

    def test_bug3_ratelimited_arrete_le_run_sans_rejouer_chaque_candidat(self):
        crate_jobs.save_json(self.paths["RECOS_CANDIDATES_PATH"], self._candidates(5))
        calls = []

        def fake_search(*a, **kw):
            calls.append(1)
            raise ytcache.RateLimited("limite de débit")

        with mock.patch.object(ytcache, "search_video_diag", side_effect=fake_search):
            job = FakeJob()
            crate_jobs.job_publish_recos(job, {})
        # 1 seul candidat réellement tenté malgré 5 en file : les 4 suivants
        # sont renvoyés en `remaining` sans nouvel appel à search_video_diag.
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self._remaining()), 5)
        self.assertEqual(self._budget_count(), 0)
        self.assertIn("limite le débit", job.finished)

    def test_bug1_exception_non_classee_persiste_quand_meme_letat_du_run(self):
        crate_jobs.save_json(self.paths["RECOS_CANDIDATES_PATH"], self._candidates(3))
        calls = []

        def fake_search(*a, **kw):
            calls.append(1)
            if len(calls) < 3:
                return None, "aucun résultat"
            # 3e candidat : erreur non classée (ex. clé invalide -> RuntimeError
            # de ytcache.request, jamais rattrapée par job_publish_recos avant
            # le correctif du 16/09).
            raise RuntimeError("YouTube 403: forbidden")

        with mock.patch.object(ytcache, "search_video_diag", side_effect=fake_search):
            job = FakeJob()
            with self.assertRaises(RuntimeError):
                crate_jobs.job_publish_recos(job, {})
        # Les 2 premiers candidats (échec "aucun résultat", remis en file avec
        # `attempts` incrémenté) doivent être persistés malgré l'exception sur
        # le 3e -- avant le correctif, tout `remaining`/le budget étaient
        # perdus car la fonction sortait sans passer par les save_json finaux.
        remaining = self._remaining()
        self.assertEqual(len(remaining), 2)
        self.assertTrue(all(c.get("attempts") == 1 for c in remaining))
        # 2 recherches ont bien été comptabilisées (les 2 qui ont abouti à une
        # réponse, même négative) malgré l'exception sur la 3e.
        self.assertEqual(self._budget_count(), 2)

    def test_bug1_requestexception_reseau_ne_perd_pas_le_candidat(self):
        import requests
        crate_jobs.save_json(self.paths["RECOS_CANDIDATES_PATH"], self._candidates(1))
        with mock.patch.object(ytcache, "search_video_diag",
                                side_effect=requests.Timeout("timed out")):
            job = FakeJob()
            crate_jobs.job_publish_recos(job, {})  # ne doit PAS lever
        self.assertEqual(len(self._remaining()), 1)
        self.assertEqual(self._budget_count(), 0)


if __name__ == "__main__":
    unittest.main()
