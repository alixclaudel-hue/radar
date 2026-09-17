"""`catalog_labelgraph.neighbors_for()` -- ajouté le 17/09 (brief VPS, constat
3) pour `Ctx.label_db_signal` : la version bulk de `neighbors()`, nécessaire
dès qu'on interroge le voisinage de milliers de labels suivis (Cœur/Aimé) à
la fois plutôt qu'un seul. Vérifie la même sémantique que `neighbors()`
(directions `kind="parent"`, agrégation de `kind="artist"`) sur plusieurs
clés en une passe, sur une base SQLite en mémoire (schéma réel du module,
`con=` explicite -- pas de fichier sur disque).

Lancer : python3 -m unittest tests.test_catalog_labelgraph_neighbors -v
"""
import os
import sqlite3
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_web.radar import catalog_labelgraph as clg  # noqa: E402


def _mem_db(pairs):
    """`pairs` : [(a, b, kind, weight), ...] -- insère tel quel dans le
    schéma réel du module (mêmes contraintes/index que la base réelle)."""
    con = sqlite3.connect(":memory:")
    clg._create_schema(con)
    con.executemany("INSERT INTO label_pairs(a, b, kind, weight) VALUES (?,?,?,?)", pairs)
    clg._create_indexes(con)
    con.commit()
    return con


class NeighborsForTestCase(unittest.TestCase):
    def test_agrege_liens_artist_dans_les_deux_sens(self):
        # "seed" lié à "other" (a=seed) et à "other2" (b=seed) : neighbors_for
        # doit voir les deux, quel que soit le sens de stockage de la paire.
        con = _mem_db([
            ("other", "seed", "artist", 3),
            ("seed", "other2", "artist", 2),
        ])
        out = clg.neighbors_for(["seed"], con=con)
        others = {e["label_key"]: e for e in out["seed"]}
        self.assertEqual(set(others), {"other", "other2"})
        self.assertEqual(others["other"]["weight"], 3)
        self.assertEqual(others["other2"]["weight"], 2)
        self.assertIsNone(others["other"]["role"])   # kind="artist" -> pas de sens

    def test_role_parent_enfant_selon_le_sens_de_stockage(self):
        # a=enfant, b=parent (convention du module) : "child" relie SON parent,
        # "parent" relie SON sous-label -- vérifié dans les deux sens de requête.
        con = _mem_db([("child_label", "parent_label", "parent", 1)])
        out = clg.neighbors_for(["child_label", "parent_label"], con=con)
        self.assertEqual(out["child_label"], [
            {"label_key": "parent_label", "kind": "parent", "weight": 1, "role": "child"}])
        self.assertEqual(out["parent_label"], [
            {"label_key": "child_label", "kind": "parent", "weight": 1, "role": "parent"}])

    def test_plusieurs_seeds_en_une_passe(self):
        con = _mem_db([
            ("seed_a", "shared", "artist", 5),
            ("seed_b", "shared", "artist", 1),
            ("seed_a", "only_a", "artist", 9),
        ])
        out = clg.neighbors_for(["seed_a", "seed_b", "absent_seed"], con=con)
        self.assertEqual({e["label_key"] for e in out["seed_a"]}, {"shared", "only_a"})
        self.assertEqual({e["label_key"] for e in out["seed_b"]}, {"shared"})
        self.assertEqual(out["absent_seed"], [])   # clé demandée sans aucun lien

    def test_min_weight_et_kinds_filtrent(self):
        con = _mem_db([
            ("seed", "weak", "artist", 1),
            ("seed", "strong", "artist", 10),
            ("seed", "parent_lbl", "parent", 1),   # weight=1 toujours (fait administratif)
        ])
        out = clg.neighbors_for(["seed"], min_weight=2, con=con)
        self.assertEqual({e["label_key"] for e in out["seed"]}, {"strong"})
        out2 = clg.neighbors_for(["seed"], kinds={"artist"}, con=con)
        self.assertEqual({e["label_key"] for e in out2["seed"]}, {"weak", "strong"})

    def test_liste_vide_ou_graphe_absent(self):
        self.assertEqual(clg.neighbors_for([]), {})
        # sans `con` ET sans graphe construit -> ne doit jamais tenter de se
        # connecter à DB_PATH (mock plutôt que de dépendre de l'état réel du
        # disque de test, potentiellement pollué par un autre test du run).
        with unittest.mock.patch.object(clg, "available", return_value=False):
            self.assertEqual(clg.neighbors_for(["x"]), {})


if __name__ == "__main__":
    unittest.main()
