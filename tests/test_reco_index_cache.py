"""`Ctx.reco_index` — chantier M1 du brief VPS du 18/09, couches 1 et 3.

Couche 1 (mémoire) : `reco_index` dérivait de `reco_rows`, lui aussi mémoïsé.
Le chemin recherche gardait donc en mémoire les 465 284 lignes complètes
(787 Mo mesurés en prod) alors qu'il n'a besoin que de `{clé: score}` (20 Mo).
Les deux nœuds partagent désormais le même générateur (`_iter_reco_rows`) sans
que l'un retienne l'autre. Aucune règle de scoring ne change : les scores
doivent être strictement identiques.

Couche 3 (temps) : le calcul met 183,5 s à froid en prod, payés par la première
recherche après chaque redémarrage. `recoindex.py` le met en cache sur disque,
le worker le reconstruit hors requête, et `Ctx` retombe sur le calcul en ligne
dès que le cache est absent, périmé ou illisible.

Vérifié par inspection du cache `_DERIVED` et du contenu du fichier, jamais au
chronomètre (instable en CI) — même méthode que `tests/test_ctx_stats.py` et
`tests/test_wantlist_perf.py`.

Lancer : python3 -m unittest tests.test_reco_index_cache -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_web.radar import paths, recoindex, scoring, store  # noqa: E402

# Nœuds que le chemin recherche ne doit PAS payer quand le cache est bon.
_COSTLY = ("reco_rows", "label_db_signal", "label_artist_signal", "ascore",
           "artist_label_signal", "graph_rescore")


class RecoIndexTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for p in (mock.patch.object(paths, "USERS_DIR", os.path.join(self._tmp.name, "users")),
                  mock.patch.object(paths, "SHARED_DIR", os.path.join(self._tmp.name, "shared"))):
            p.start()
            self.addCleanup(p.stop)
        store._CONFIG_CACHE.clear()
        self.addCleanup(store._CONFIG_CACHE.clear)
        scoring._DERIVED.clear()
        self.addCleanup(scoring._DERIVED.clear)

        self.uid = paths.DEFAULT_UID
        self.P = paths.user_paths(self.uid)
        cfg = store.load_config(self.uid)
        cfg["taste_categories"] = {"1": ["House"], "2": ["Techno"]}
        cfg["artist_categories"] = {"1": [], "2": []}
        cfg["label_categories"] = {"1": ["Label Coeur"], "2": ["Label Aime"]}
        store.save_config(cfg, self.uid)

        self.k_coeur = store.normalize_label("Label Coeur")
        self.k_aime = store.normalize_label("Label Aime")
        self.k_corpus = store.normalize_label("Label Ecoute")
        store.save(self.P.corpus, [
            {"artist": "Corpus Artist", "label": "Label Ecoute", "source": "youtube"},
        ])
        store.save(self.P.collection, {
            "label_counts": {self.k_coeur: 4},
            "want_label_counts": {self.k_aime: 2},
            "label_ids": {self.k_coeur: {"name": "Label Coeur", "id": 1}},
        })
        store.save(self.P.profile, {
            self.k_coeur: {"style_counts": {"House": 30, "Jazz": 5}},
            self.k_aime: {"style_counts": {"Techno": 10}},
        })
        store.save(self.P.graph, {"edges": {}, "labels": {}, "seeds": {}})
        store.save(self.P.artists_res, {})
        store.save(self.P.resolved, {})

    def _slot(self, c):
        return scoring._DERIVED.get(c._key, {})

    # ------------------------------------------------------------ couche 1
    def test_reco_index_ne_materialise_pas_les_lignes(self):
        """Le garde-fou des 787 Mo : après `reco_index` seul, `reco_rows` ne
        doit pas être dans le cache dérivé."""
        c = scoring.Ctx(self.uid)
        self.assertTrue(c.reco_index)
        slot = self._slot(c)
        self.assertIn("reco_index", slot)
        self.assertNotIn("reco_rows", slot)

    def test_scores_identiques_a_reco_rows(self):
        """Couche 1 = pur gain mémoire : aucun score ne doit bouger."""
        c = scoring.Ctx(self.uid)
        self.assertEqual(c.reco_index, {r["key"]: r["score"] for r in c.reco_rows()})

    def test_reco_rows_reste_trie_par_score_decroissant(self):
        scores = [r["score"] for r in scoring.Ctx(self.uid).reco_rows()]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreaterEqual(len(scores), 3)     # les 3 labels du jeu de test

    def test_reco_rows_garde_toutes_ses_colonnes(self):
        """Non-régression du passage en générateur : l'UI lit ces clés."""
        row = next(r for r in scoring.Ctx(self.uid).reco_rows() if r["key"] == self.k_coeur)
        self.assertEqual(row["name"], "Label Coeur")
        self.assertEqual(row["owned"], 4)
        self.assertEqual(row["tier"], "1")
        self.assertIsNotNone(row["aff"])
        self.assertIn("affinity", row["feat"])

    # ------------------------------------------------------------ couche 3
    def test_cache_absent_calcul_en_ligne(self):
        c = scoring.Ctx(self.uid)
        self.assertIsNone(recoindex.load(self.uid, c._key))
        self.assertFalse(c.reco_index_is_fresh())
        self.assertTrue(c.reco_index)               # calculé quand même

    def test_cache_frais_relu_sans_recalcul(self):
        """Valeur volontairement fausse dans le cache : si `reco_index` la
        renvoie, c'est bien le fichier qui a été lu, pas un recalcul."""
        c = scoring.Ctx(self.uid)
        recoindex.save(self.uid, c._key, {"cache-temoin": 42})
        scoring._DERIVED.clear()

        c2 = scoring.Ctx(self.uid)
        self.assertTrue(c2.reco_index_is_fresh())
        self.assertEqual(c2.reco_index, {"cache-temoin": 42})
        slot = self._slot(c2)
        for node in _COSTLY:
            self.assertNotIn(node, slot, f"{node} calculé alors que le cache est frais")

    def test_cache_perime_par_changement_de_gout(self):
        """Signature différente -> cache ignoré, recalcul silencieux."""
        c = scoring.Ctx(self.uid)
        recoindex.save(self.uid, c._key, {"cache-temoin": 42})
        cfg = store.load_config(self.uid)
        cfg["label_categories"]["1"].append("Label Ajoute")
        store.save_config(cfg, self.uid)
        store._CONFIG_CACHE.clear()
        scoring._DERIVED.clear()

        c2 = scoring.Ctx(self.uid)
        self.assertFalse(c2.reco_index_is_fresh())
        self.assertNotIn("cache-temoin", c2.reco_index)
        self.assertIn(self.k_coeur, c2.reco_index)

    def test_cache_illisible_repli_sans_exception(self):
        c = scoring.Ctx(self.uid)
        recoindex.save(self.uid, c._key, {"cache-temoin": 42})
        with open(recoindex.index_path(self.uid), "w", encoding="utf-8") as fh:
            fh.write("{ tronqu")                    # JSON invalide
        scoring._DERIVED.clear()

        c2 = scoring.Ctx(self.uid)
        self.assertIn(self.k_coeur, c2.reco_index)  # repli, pas de crash

    def test_save_reco_index_ecrit_index_et_meta(self):
        c = scoring.Ctx(self.uid)
        meta = c.save_reco_index()
        self.assertEqual(meta["sig"], recoindex.sig_of(c._key))
        self.assertGreaterEqual(meta["n"], 3)
        with open(recoindex.index_path(self.uid), encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk, c.build_reco_index())
        self.assertTrue(c.reco_index_is_fresh())

    def test_signature_suit_le_graphe_catalogue(self):
        """`catalog_labelgraph` alimente `label_db_signal` : sa reconstruction
        mensuelle doit périmer le cache. Il manquait à la signature avant le
        18/09 (le cache mémoire aussi survivait donc à tort)."""
        from radar_web.radar import catalog_labelgraph as clg
        c = scoring.Ctx(self.uid)
        before = c._key
        # `clg.DB_PATH` est figé à l'import du module ; chaque test a son propre
        # répertoire temporaire, donc on crée le parent du chemin réellement visé.
        os.makedirs(os.path.dirname(clg.DB_PATH), exist_ok=True)
        self.addCleanup(lambda: os.path.exists(clg.DB_PATH) and os.remove(clg.DB_PATH))
        with open(clg.DB_PATH, "w", encoding="utf-8") as fh:
            fh.write("x")
        self.assertNotEqual(scoring.derived_key(self.uid, c.cfg), before)


if __name__ == "__main__":
    unittest.main()
