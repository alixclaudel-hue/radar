import json
import os
import tempfile
import unittest

from radar_ops import probe
from radar_ops.sampler import Sampler


class TestProbeEtSampler(unittest.TestCase):

    def test_dossier_jobs_inexistant_renvoie_listes_vides(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inexistant = os.path.join(tmpdir, "inexistant")
            self.assertEqual(probe.read_queue(inexistant), [])
            self.assertEqual(probe.read_statuses(inexistant), [])

    def test_queue_json_absent_renvoie_vide(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(probe.read_queue(tmpdir), [])

    def test_queue_json_corrompu_renvoie_vide(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = os.path.join(tmpdir, "queue.json")
            with open(queue_path, "w", encoding="utf-8") as f:
                f.write("{corrompu")
            self.assertEqual(probe.read_queue(tmpdir), [])

    def test_entree_running_passe_devant_queued(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = os.path.join(tmpdir, "queue.json")
            data = [
                {"id": 1, "state": "queued", "priority": 5, "ts": 100.0},
                {"id": 2, "state": "running", "priority": 1, "ts": 100.0},
            ]
            with open(queue_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            res = probe.read_queue(tmpdir)
            self.assertEqual(len(res), 2)
            self.assertEqual(res[0]["id"], 2)
            self.assertEqual(res[1]["id"], 1)

    def test_entre_deux_queued_priorite_superieure_passe_devant(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = os.path.join(tmpdir, "queue.json")
            data = [
                {"id": 1, "state": "queued", "priority": 0, "ts": 100.0},
                {"id": 2, "state": "queued", "priority": 1, "ts": 100.0},
            ]
            with open(queue_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            res = probe.read_queue(tmpdir)
            self.assertEqual(len(res), 2)
            self.assertEqual(res[0]["id"], 2)
            self.assertEqual(res[1]["id"], 1)

    def test_entree_sans_priorite_traitee_comme_priorite_1(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = os.path.join(tmpdir, "queue.json")
            data = [
                {"id": 1, "state": "queued", "priority": 0, "ts": 100.0},
                {"id": 2, "state": "queued", "ts": 100.0},  # Sans champ priority
            ]
            with open(queue_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            res = probe.read_queue(tmpdir)
            self.assertEqual(len(res), 2)
            self.assertEqual(res[0]["id"], 2)
            self.assertEqual(res[1]["id"], 1)

    def test_position_numerote_seulement_queued_et_vaut_none_pour_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = os.path.join(tmpdir, "queue.json")
            data = [
                {"id": 1, "state": "running", "ts": 100.0},
                {"id": 2, "state": "queued", "ts": 90.0},
                {"id": 3, "state": "queued", "ts": 80.0},
            ]
            with open(queue_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            res = probe.read_queue(tmpdir)
            self.assertEqual(res[0]["id"], 1)
            self.assertIsNone(res[0]["position"])
            # À priorité égale le worker sert le plus ancien d'abord : id 3 (ts 80)
            # avant id 2 (ts 90), pas l'ordre d'écriture dans le fichier.
            self.assertEqual(res[1]["id"], 3)
            self.assertEqual(res[1]["position"], 1)
            self.assertEqual(res[2]["id"], 2)
            self.assertEqual(res[2]["position"], 2)

    def test_read_statuses_trouve_plusieurs_fichiers_pour_meme_uid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            uid_dir = os.path.join(tmpdir, "user123")
            os.makedirs(uid_dir)
            for name in ["jobA", "jobB"]:
                p = os.path.join(uid_dir, f"{name}.status.json")
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"done": 10, "total": 100}, f)
            statuses = probe.read_statuses(tmpdir)
            self.assertEqual(len(statuses), 2)
            self.assertTrue(all(s["uid"] == "user123" for s in statuses))
            self.assertEqual({s["name"] for s in statuses}, {"jobA", "jobB"})

    def test_pct_vaut_none_quand_total_zero_ou_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            uid_dir = os.path.join(tmpdir, "user123")
            os.makedirs(uid_dir)
            
            p1 = os.path.join(uid_dir, "job_zero.status.json")
            with open(p1, "w", encoding="utf-8") as f:
                json.dump({"done": 10, "total": 0}, f)

            p2 = os.path.join(uid_dir, "job_absent.status.json")
            with open(p2, "w", encoding="utf-8") as f:
                json.dump({"done": 10}, f)

            statuses = probe.read_statuses(tmpdir)
            self.assertEqual(len(statuses), 2)
            for s in statuses:
                self.assertIsNone(s["pct"])

    def test_fichier_status_json_tronque_ignore_sans_exception(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            uid_dir = os.path.join(tmpdir, "user123")
            os.makedirs(uid_dir)
            p = os.path.join(uid_dir, "corrompu.status.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"done": 10,')  # JSON tronqué

            statuses = probe.read_statuses(tmpdir)
            self.assertEqual(statuses, [])

    @staticmethod
    def _st(done, total=100, uid="u1", name="scan"):
        return [{"uid": uid, "name": name, "done": done, "total": total}]

    def test_sampler_rate_renvoie_none_avec_moins_de_deux_points(self):
        s = Sampler()
        self.assertIsNone(s.rate("u1", "scan"))
        s.observe(self._st(10), now=0.0)
        self.assertIsNone(s.rate("u1", "scan"))

    def test_sampler_rate_calcule_le_debit_sur_la_fenetre(self):
        s = Sampler()
        s.observe(self._st(10), now=0.0)
        s.observe(self._st(30), now=10.0)
        self.assertEqual(s.rate("u1", "scan"), 2.0)

    def test_sampler_compteur_qui_regresse_purge_l_historique(self):
        s = Sampler()
        s.observe(self._st(10), now=0.0)
        s.observe(self._st(30), now=10.0)
        self.assertEqual(s.rate("u1", "scan"), 2.0)
        # Relance du même job : le compteur repart de plus bas. Sans purge, le
        # débit deviendrait négatif et le temps restant absurde.
        s.observe(self._st(5), now=15.0)
        self.assertIsNone(s.rate("u1", "scan"))
        s.observe(self._st(25), now=20.0)
        self.assertEqual(s.rate("u1", "scan"), 4.0)

    def test_sampler_suit_les_jobs_separement(self):
        s = Sampler()
        s.observe([{"uid": "u1", "name": "a", "done": 0, "total": 10},
                   {"uid": "u1", "name": "b", "done": 0, "total": 10}], now=0.0)
        s.observe([{"uid": "u1", "name": "a", "done": 10, "total": 10},
                   {"uid": "u1", "name": "b", "done": 2, "total": 10}], now=10.0)
        self.assertEqual(s.rate("u1", "a"), 1.0)
        self.assertEqual(s.rate("u1", "b"), 0.2)

    def test_sampler_ignore_un_statut_sans_compteur(self):
        s = Sampler()
        s.observe([{"uid": "u1", "name": "scan", "done": None, "total": 10}], now=0.0)
        s.observe([{"uid": "u1", "name": "scan", "done": None, "total": 10}], now=10.0)
        self.assertIsNone(s.rate("u1", "scan"))

    def test_sampler_eta_none_si_total_nul_ou_debit_nul(self):
        s = Sampler()
        self.assertIsNone(s.eta_s("u1", "scan", 0, 0))
        s.observe(self._st(10), now=0.0)
        s.observe(self._st(10), now=10.0)
        self.assertEqual(s.rate("u1", "scan"), 0.0)
        self.assertIsNone(s.eta_s("u1", "scan", 10, 100))

    def test_sampler_eta_calcule_le_temps_restant(self):
        s = Sampler()
        s.observe(self._st(0), now=0.0)
        s.observe(self._st(20), now=10.0)     # 2 par seconde
        self.assertEqual(s.eta_s("u1", "scan", 20, 100), 40.0)

    def test_sampler_eta_zero_quand_done_depasse_total(self):
        s = Sampler()
        s.observe(self._st(10), now=0.0)
        s.observe(self._st(20), now=10.0)
        self.assertEqual(s.eta_s("u1", "scan", 20, 15), 0.0)

    def test_sampler_purge_les_points_hors_fenetre(self):
        s = Sampler(window_s=30)
        s.observe(self._st(0), now=0.0)
        s.observe(self._st(10), now=100.0)    # le premier point sort de la fenêtre
        self.assertIsNone(s.rate("u1", "scan"))

    def test_sampler_forget_idle_supprime_les_cles_absentes(self):
        s = Sampler()
        s.observe(self._st(0), now=0.0)
        s.observe(self._st(10), now=10.0)
        self.assertEqual(s.rate("u1", "scan"), 1.0)
        s.forget_idle({("u1", "autre")})
        self.assertIsNone(s.rate("u1", "scan"))


if __name__ == "__main__":
    unittest.main()
