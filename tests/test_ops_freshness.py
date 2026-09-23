import json
import os
import sqlite3
import tempfile
import unittest

from radar_ops import freshness
from radar_web.radar import opslog


class TestRadarOpsFreshness(unittest.TestCase):

    def test_read_meta_chemin_inexistant(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "inexistant.db")
            res = freshness.read_meta(db_path)
            self.assertEqual(res, {})
            self.assertFalse(os.path.exists(db_path))

    def test_read_meta_sans_table_scorestore_meta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "sans_table.db")
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE autre_table (id INTEGER)")
            con.commit()
            con.close()

            res = freshness.read_meta(db_path)
            self.assertEqual(res, {})

    def test_read_meta_lit_paires_cle_valeur(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "valide.db")
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE scorestore_meta (key TEXT, value TEXT)")
            con.execute("INSERT INTO scorestore_meta VALUES ('fingerprint', 'hash123')")
            con.execute("INSERT INTO scorestore_meta VALUES ('rows', '150')")
            con.commit()
            con.close()

            res = freshness.read_meta(db_path)
            self.assertEqual(res, {"fingerprint": "hash123", "rows": "150"})

    def test_compare_absent_sans_meta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "vide.db")
            res = freshness.compare(db_path, "hash123")
            self.assertEqual(res["state"], "absent")
            self.assertIn("aucun précalcul", res["reason"])

    def test_compare_a_jour_si_empreinte_egale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "ajour.db")
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE scorestore_meta (key TEXT, value TEXT)")
            con.execute("INSERT INTO scorestore_meta VALUES ('fingerprint', 'hash123')")
            con.commit()
            con.close()

            res = freshness.compare(db_path, "hash123")
            self.assertEqual(res["state"], "a_jour")
            self.assertIsNone(res["reason"])

    def test_compare_perime_si_empreinte_differente(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "perime.db")
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE scorestore_meta (key TEXT, value TEXT)")
            con.execute("INSERT INTO scorestore_meta VALUES ('fingerprint', 'hash_ancien')")
            con.commit()
            con.close()

            res = freshness.compare(db_path, "hash_nouveau")
            self.assertEqual(res["state"], "perime")

    def test_compare_perime_avec_detail_remplit_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "detail.db")
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE scorestore_meta (key TEXT, value TEXT)")
            con.execute("INSERT INTO scorestore_meta VALUES ('fingerprint', 'hash_ancien')")
            detail_stocke = {"config": "ancienne_config", "fichier1.txt": "v1"}
            con.execute("INSERT INTO scorestore_meta VALUES ('detail', ?)", (json.dumps(detail_stocke),))
            con.commit()
            con.close()

            detail_courant = {"config": "nouvelle_config", "fichier1.txt": "v1"}
            res = freshness.compare(db_path, "hash_nouveau", detail_courant)
            self.assertEqual(res["state"], "perime")
            self.assertIn("la configuration de scoring a changé", res["changes"])

    def test_explain_diff_detecte_config_changee(self):
        stored = {"config": "v1"}
        current = {"config": "v2"}
        diffs = freshness.explain_diff(stored, current)
        self.assertIn("la configuration de scoring a changé", diffs)

    def test_explain_diff_detecte_fichier_reecrit(self):
        stored = {"config": "v1", "data.csv": "hash_a"}
        current = {"config": "v1", "data.csv": "hash_b"}
        diffs = freshness.explain_diff(stored, current)
        self.assertIn("data.csv a été réécrit", diffs)

    def test_explain_diff_detecte_fichier_apparu(self):
        stored = {"config": "v1"}
        current = {"config": "v1", "nouveau.csv": "hash_a"}
        diffs = freshness.explain_diff(stored, current)
        self.assertIn("nouveau.csv est apparu", diffs)

    def test_explain_diff_detecte_fichier_disparu(self):
        stored = {"config": "v1", "ancien.csv": "hash_a"}
        current = {"config": "v1"}
        diffs = freshness.explain_diff(stored, current)
        self.assertIn("ancien.csv a disparu", diffs)


class TestRadarWebOpslog(unittest.TestCase):

    def test_append_ecrit_ligne_json_et_ne_mute_pas_entree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "ops.log")
            entree = {"action": "test", "params": [1, 2]}
            entree_copie = dict(entree)

            opslog.append(log_path, entree)

            self.assertEqual(entree, entree_copie)
            with open(log_path, "r", encoding="utf-8") as f:
                ligne = f.read().strip()
                donnees_ecrites = json.loads(ligne)
                # `append` horodate la ligne écrite sans toucher au dict de
                # l'appelant : c'est précisément ce que ce test verrouille.
                self.assertIn("ts", donnees_ecrites)
                self.assertNotIn("ts", entree)
                del donnees_ecrites["ts"]
                self.assertEqual(donnees_ecrites, entree)

    def test_append_cree_dossier_parent_manquant(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "sous_dossier", "autre_sous_dossier", "ops.log")
            opslog.append(log_path, {"action": "creation"})
            self.assertTrue(os.path.exists(log_path))

    def test_tail_renvoie_du_plus_recent_au_plus_ancien_et_respecte_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "ops.log")
            opslog.append(log_path, {"id": 1})
            opslog.append(log_path, {"id": 2})
            opslog.append(log_path, {"id": 3})

            res = opslog.tail(log_path, limit=2)
            self.assertEqual([e["id"] for e in res], [3, 2])

    def test_tail_fonctionne_sur_grand_fichier(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "ops.log")
            for i in range(2500):
                opslog.append(log_path, {"id": i})

            res = opslog.tail(log_path, limit=5)
            self.assertEqual([e["id"] for e in res], [2499, 2498, 2497, 2496, 2495])

    def test_tail_ignore_ligne_invalide(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "ops.log")
            opslog.append(log_path, {"id": 1})
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("LIGNE_INVALID_JSON\n")
            opslog.append(log_path, {"id": 2})

            res = opslog.tail(log_path, limit=10)
            self.assertEqual([e["id"] for e in res], [2, 1])

    def test_stream_new_renvoie_evenements_et_fait_progresser_offset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "ops.log")
            opslog.append(log_path, {"id": 1})

            evenements, offset = opslog.stream_new(log_path, 0)
            self.assertEqual([e["id"] for e in evenements], [1])
            self.assertGreater(offset, 0)

            opslog.append(log_path, {"id": 2})
            nouveaux_evs, nouvel_offset = opslog.stream_new(log_path, offset)
            self.assertEqual([e["id"] for e in nouveaux_evs], [2])
            self.assertGreater(nouvel_offset, offset)

    def test_stream_new_ignore_derniere_ligne_sans_retour_a_la_ligne(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "ops.log")
            opslog.append(log_path, {"id": 1})
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": 2}))

            evenements, offset = opslog.stream_new(log_path, 0)
            self.assertEqual([e["id"] for e in evenements], [1])

    def test_stream_new_repart_de_zero_si_le_fichier_a_retreci(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "ops.log")
            opslog.append(log_path, {"id": 1})
            opslog.append(log_path, {"id": 2})

            _, offset = opslog.stream_new(log_path, 0)

            with open(log_path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"id": 3}) + "\n")

            evenements, nouvel_offset = opslog.stream_new(log_path, offset)
            self.assertEqual([e["id"] for e in evenements], [3])
            self.assertGreater(nouvel_offset, 0)

    def test_is_noisy_vrai_et_faux(self):
        self.assertTrue(opslog.is_noisy("/static/x.css"))
        self.assertTrue(opslog.is_noisy("/jobs/scan_recos/status"))
        self.assertTrue(opslog.is_noisy("/health"))
        self.assertFalse(opslog.is_noisy("/search"))


if __name__ == "__main__":
    unittest.main()
