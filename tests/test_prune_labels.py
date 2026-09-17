"""Job `prune_labels` (crate_jobs.py) -- chantier C du pt 57 (CLAUDE.md),
brief révisé par la session VPS le 17/09 : retirer de `label_categories`
(Cœur+Aimé) les labels hors du goût courant (`taste_categories`/`Ctx.wmap`),
sauf label possédé ou écouté (garde-fous non désactivables).

Règle : retiré si (known < max_known) OU (pct < min_pct), sauf possédé
(collection.label_counts > 0) ou écouté (corpus_label_scores > 0). `known`/
`tot` viennent de `discogs_dump.label_style_counts`, repli `Ctx.profile`
(pas de vrai dump dans ce test -- `dd.available()` est False en environnement
de test, donc le repli profil est exercé nommément, comme en prod tant que
le dump n'a pas de données pour un label donné).

Simulation par défaut (apply=False, config inchangée) ; apply=True copie
la config vers un .bak-<horodatage> puis retire les entrées (suppression
sèche, décision utilisateur 17/09) et écrit un rapport JSON.

Lancer : python3 -m unittest tests.test_prune_labels -v
"""
import glob
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

import crate_jobs  # noqa: E402
from radar_web.radar import paths, store  # noqa: E402


class FakeJob:
    def __init__(self):
        self.st = {}
        self.ticks = []
        self.finished = None
        self.error = None

    def stopped(self):
        return False

    def tick(self, last="", inc=1, total=None):
        self.ticks.append(last)

    def finish(self, message="", error=None):
        self.finished = message
        self.error = error


class PruneLabelsTestCase(unittest.TestCase):
    def setUp(self):
        self.uid = paths.DEFAULT_UID
        P = paths.user_paths(self.uid)

        cfg = store.load_config(self.uid)
        cfg["taste_categories"] = {"1": ["House"], "2": []}
        cfg["label_categories"] = {
            "1": ["Label A (gardé, connu)", "Label E (retiré, pct bas)"],
            "2": ["Label B (retiré, inconnu)", "Label C (possédé, épargné)",
                  "Label D (écouté, épargné)"],
        }
        store.save_config(cfg, self.uid)

        store.save(P.profile, {
            "label a (gardé, connu)": {"style_counts": {"House": 10, "Techno": 90}},
            "label b (retiré, inconnu)": {"style_counts": {"Techno": 100}},
            "label c (possédé, épargné)": {"style_counts": {"Jazz": 50}},
            "label d (écouté, épargné)": {"style_counts": {"Jazz": 50}},
            "label e (retiré, pct bas)": {"style_counts": {"House": 5, "Techno": 195}},
        })
        store.save(P.collection, {"label_counts": {"label c (possédé, épargné)": 1}})
        store.save(P.corpus, [{"label": "Label D (écouté, épargné)", "artist": "X",
                                "title": "Y", "source": "youtube"}])

        # invalide les caches process-local (store.load_cached / read_config)
        # entre 2 tests -- même fichier de config réutilisé d'un test à l'autre.
        store._LOAD_CACHE.clear()
        store._CONFIG_CACHE.clear()

    def tearDown(self):
        for p in glob.glob(f"{crate_jobs.CONFIG_PATH}.bak-*"):
            os.remove(p)
        for p in glob.glob(os.path.join(crate_jobs.USER_DIR, "prune_labels_report_*.json")):
            os.remove(p)

    def _run(self, **params):
        job = FakeJob()
        crate_jobs.job_prune_labels(job, params)
        return job

    def test_simulation_ne_modifie_pas_la_config(self):
        job = self._run(max_known=5, min_pct=5)
        self.assertIsNone(job.error)
        self.assertIn("SIMULATION", job.finished)
        cfg = store.load_config(self.uid)
        names = {n for cid in ("1", "2") for n in cfg["label_categories"][cid]}
        self.assertEqual(len(names), 5, "simulation : rien ne doit disparaître de la config")

    def test_labels_retires_et_gardes(self):
        job = self._run(max_known=5, min_pct=5)
        # reconstruit les décisions depuis les logs plutôt que de dupliquer la formule
        dropped = {t.split(" : ")[0] for t in job.ticks if t.endswith("— écarté")}
        self.assertIn("Label B (retiré, inconnu)", dropped)   # known=0 < 5
        self.assertIn("Label E (retiré, pct bas)", dropped)   # known=5 mais pct=2.5% < 5%
        self.assertNotIn("Label A (gardé, connu)", dropped)   # known=10, pct=10%
        self.assertNotIn("Label C (possédé, épargné)", dropped)  # spared malgré known=0
        self.assertNotIn("Label D (écouté, épargné)", dropped)   # spared malgré known=0

    def test_apply_retire_de_la_config_avec_backup_et_rapport(self):
        job = self._run(max_known=5, min_pct=5, apply=1)
        self.assertIn("APPLIQUÉ", job.finished)

        cfg = store.load_config(self.uid)
        remaining = {n for cid in ("1", "2") for n in cfg["label_categories"][cid]}
        self.assertEqual(remaining, {"Label A (gardé, connu)", "Label C (possédé, épargné)",
                                      "Label D (écouté, épargné)"})

        backups = glob.glob(f"{crate_jobs.CONFIG_PATH}.bak-*")
        self.assertEqual(len(backups), 1)
        backup_cfg = crate_jobs.load_json(backups[0], {})
        old_names = {n for cid in ("1", "2") for n in backup_cfg["label_categories"][cid]}
        self.assertEqual(len(old_names), 5, "la sauvegarde doit contenir la config AVANT retrait")

        reports = glob.glob(os.path.join(crate_jobs.USER_DIR, "prune_labels_report_*.json"))
        self.assertEqual(len(reports), 1)
        report = crate_jobs.load_json(reports[0], {})
        removed_names = {r["name"] for r in report["removed"]}
        self.assertEqual(removed_names, {"Label B (retiré, inconnu)", "Label E (retiré, pct bas)"})
        self.assertEqual(report["kept_count"], 3)

    def test_garde_fous_possede_ecoute_non_desactivables_par_seuil(self):
        # seuils extrêmes (tout devrait sauter) : les 2 labels spared résistent quand même
        job = self._run(max_known=1000, min_pct=100, apply=0)
        dropped = {t.split(" : ")[0] for t in job.ticks if t.endswith("— écarté")}
        self.assertNotIn("Label C (possédé, épargné)", dropped)
        self.assertNotIn("Label D (écouté, épargné)", dropped)

    def test_aucun_label_suivi(self):
        cfg = store.load_config(self.uid)
        cfg["label_categories"] = {"1": [], "2": []}
        store.save_config(cfg, self.uid)
        store._CONFIG_CACHE.clear()
        job = self._run(max_known=5, min_pct=5)
        self.assertIn("rien à élaguer", job.finished)


if __name__ == "__main__":
    unittest.main()
