"""Brief VPS 17/09 (« élargir l'univers de labels ») -- 3 chantiers dans
`radar_web/radar/scoring.py`, dans l'ordre imposé par le brief (le 1er est un
prérequis dur des deux suivants) :

1. Normalisation de `_compute_reco_rows` : avant ce lot, `tot` (dénominateur)
   était TOUJOURS la somme fixe des 5 poids configurés, même pour un label
   sans aucun signal réel sur 4 des 5 termes -- un label sans signal
   comportemental plafonnait donc très bas quelle que soit son affinité de
   style (constat 2 du brief). Corrigé : chaque terme absent (label jamais
   possédé/écouté/lié, jamais profilé, pas de tier suivi) est exclu du terme
   ET de son poids au dénominateur, avec le même rétrécissement K/PRIOR
   qu'`album_score` (diagnostic N3) pour ne pas afficher la même confiance
   qu'un label noté sur les cinq signaux à la fois.
2. Câblage du tier Cœur/Aimé d'un label comme un vrai poids (`scoring.reco.tier`
   x `scoring.label_tiers`) -- avant ce lot, `label_tier_map()` n'était lu que
   comme filtre d'appartenance, jamais comme un poids (constat 1).
3. `catalog_labelgraph.neighbors_for()` câblé comme 3e composante de
   `Ctx.label_db_signal`, à côté de direct/graphe (constat 3) : un label sans
   AUCUN lien comportemental mais VOISIN (co-crédit ou sous-label Discogs)
   d'un label suivi doit pouvoir remonter.

Isole `Ctx` de `/data` réel comme `test_ctx_stats.py`. `discogs_dump` reste
« non disponible » (pas de vrai dump dans ce test, comme `test_prune_labels.py`)
-- exercé nommément pour la voie de repli (`Ctx.profile`). `catalog_labelgraph`
est mocké (`available`/`neighbors_for`) plutôt que de construire une vraie
base SQLite : ce fichier teste le CÂBLAGE côté `scoring.py`, pas
`catalog_labelgraph.py` lui-même (couvert par
`test_catalog_labelgraph_neighbors.py`).

Lancer : python3 -m unittest tests.test_reco_rows_normalization -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_web.radar import catalog_labelgraph as clg  # noqa: E402
from radar_web.radar import paths, scoring, store  # noqa: E402


class RecoRowsNormalizationTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for p in (mock.patch.object(paths, "USERS_DIR", os.path.join(self._tmp.name, "users")),
                  mock.patch.object(paths, "SHARED_DIR", os.path.join(self._tmp.name, "shared"))):
            p.start()
            self.addCleanup(p.stop)
        store._CONFIG_CACHE.clear()
        store._LOAD_CACHE.clear()
        self.addCleanup(store._CONFIG_CACHE.clear)
        self.addCleanup(store._LOAD_CACHE.clear)
        scoring._DERIVED.clear()
        self.addCleanup(scoring._DERIVED.clear)

        self.uid = paths.DEFAULT_UID
        self.P = paths.user_paths(self.uid)
        for p in (self.P.corpus,):
            store.save(p, [])
        for p in (self.P.collection, self.P.profile, self.P.artists_res, self.P.resolved):
            store.save(p, {})
        store.save(self.P.graph, {"edges": {}, "labels": {}, "seeds": {}})

    def _cfg(self, **overrides):
        cfg = store.load_config(self.uid)
        cfg.update(overrides)
        store.save_config(cfg, self.uid)
        store._CONFIG_CACHE.clear()
        return cfg

    # ---------------------------------------------------- constat 2 : normalisation

    def test_label_sans_signal_comportemental_nest_plus_ecrase(self):
        """Poids « owner » cités par le brief (collection 1.0, corpus 0.8,
        artist 0.9, affinity 1.0, db_link 0.35 -> Σ=4.05). Un label profilé
        avec une affinité parfaite, jamais possédé/écouté/lié -- mais VOISIN
        (poids db_link fin, 0.2) d'un label suivi -- doit dépasser largement
        le plafond ~26/100 que l'ANCIENNE formule (dénominateur fixe) aurait
        donné pour ce même signal."""
        cfg = self._cfg()
        cfg["scoring"]["reco"] = {"collection": 1.0, "corpus": 0.8, "artist": 0.9,
                                   "affinity": 1.0, "want_factor": 0.6, "db_link": 0.35, "tier": 0.5}
        cfg["taste_categories"] = {"1": ["House"], "2": []}
        cfg["label_categories"] = {"1": ["Seed Label"], "2": []}
        store.save_config(cfg, self.uid)
        store._CONFIG_CACHE.clear()
        store.save(self.P.profile, {"neighbor label": {"style_counts": {"House": 100}}})

        with mock.patch.object(clg, "available", return_value=True), \
             mock.patch.object(clg, "neighbors_for", return_value={
                 "seed label": [{"label_key": "neighbor label", "kind": "artist",
                                  "weight": 1, "role": None}]}):
            c = scoring.Ctx(self.uid)
            rows = {r["key"]: r for r in c.reco_rows()}

        self.assertIn("neighbor label", rows)
        row = rows["neighbor label"]
        self.assertAlmostEqual(row["feat"]["affinity"], 1.0)
        self.assertGreater(row["feat"]["db_link"], 0)
        self.assertLess(row["feat"]["db_link"], 0.5)   # signal de voisinage fin, pas fort

        # reproduit l'ANCIENNE formule (dénominateur = Σ des 5 poids, fixe)
        # pour prouver le plafond que le brief dénonçait (constat 2).
        w = cfg["scoring"]["reco"]
        old_tot = w["collection"] + w["corpus"] + w["artist"] + w["affinity"] + w["db_link"]
        old_score = round(100 * (w["affinity"] * 1.0 + w["db_link"] * row["feat"]["db_link"]) / old_tot)
        self.assertGreater(row["score"], old_score + 30,
                            "la normalisation par poids réellement en jeu doit nettement "
                            "dépasser l'ancien plafond à dénominateur fixe")

    def test_label_avec_tous_les_signaux_reste_proche_du_max(self):
        """Non-régression : un label avec un signal fort sur les 5 termes ne
        doit pas être significativement pénalisé par le rétrécissement K/PRIOR
        (effet du rétrécissement negligeable quand `tot` est déjà grand)."""
        cfg = self._cfg()
        cfg["taste_categories"] = {"1": ["House"], "2": []}
        cfg["label_categories"] = {"1": ["Full Label"], "2": []}
        store.save_config(cfg, self.uid)
        store._CONFIG_CACHE.clear()
        store.save(self.P.profile, {"full label": {"style_counts": {"House": 100}}})
        store.save(self.P.collection, {"label_counts": {"full label": 5},
                                        "label_ids": {"full label": {"name": "Full Label"}}})
        store.save(self.P.corpus, [
            {"label": "Full Label", "artist": "Fave Artist", "title": "T", "source": "youtube"}])
        cfg2 = store.load_config(self.uid)
        cfg2["artist_categories"] = {"1": ["Fave Artist"], "2": []}
        store.save_config(cfg2, self.uid)
        store._CONFIG_CACHE.clear()

        with mock.patch.object(clg, "available", return_value=False):
            c = scoring.Ctx(self.uid)
            rows = {r["key"]: r for r in c.reco_rows()}
        self.assertGreaterEqual(rows["full label"]["score"], 85)

    # ---------------------------------------------------- constat 1 : tier câblé

    def test_tier_coeur_pese_plus_que_aime(self):
        """Même label, seule la catégorie change (Cœur vs Aimé) : le score
        DOIT différer -- avant ce lot, le tier n'avait aucun effet (constat
        1 du brief)."""
        base_cfg = self._cfg()
        base_cfg["scoring"]["reco"]["tier"] = 1.0
        store.save_config(base_cfg, self.uid)
        store._CONFIG_CACHE.clear()
        store.save(self.P.collection, {"label_counts": {"tier label": 1}})

        def _score_for(tier):
            cfg = store.load_config(self.uid)
            cfg["label_categories"] = {"1": ["Tier Label"] if tier == "1" else [],
                                        "2": ["Tier Label"] if tier == "2" else []}
            store.save_config(cfg, self.uid)
            store._CONFIG_CACHE.clear()
            scoring._DERIVED.clear()
            with mock.patch.object(clg, "available", return_value=False):
                c = scoring.Ctx(self.uid)
                rows = {r["key"]: r for r in c.reco_rows()}
            return rows["tier label"]

        coeur = _score_for("1")
        aime = _score_for("2")
        self.assertGreater(coeur["score"], aime["score"])
        self.assertAlmostEqual(coeur["feat"]["tier"], 1.0)
        self.assertAlmostEqual(aime["feat"]["tier"], 0.5)

    def test_label_non_suivi_le_terme_tier_est_exclu_pas_zero(self):
        """Un label sans tier (jamais suivi) ne doit pas être pénalisé comme
        s'il avait un tier « nul » -- le terme sort du calcul, il ne compte
        pas 0 au numérateur avec son poids toujours au dénominateur."""
        cfg = self._cfg()
        cfg["scoring"]["reco"]["tier"] = 5.0   # poids énorme : un bug ferait
        # chuter le score si le terme était compté à 0 au lieu d'être exclu
        store.save_config(cfg, self.uid)
        store._CONFIG_CACHE.clear()
        store.save(self.P.collection, {"label_counts": {"untiered label": 1}})
        with mock.patch.object(clg, "available", return_value=False):
            c = scoring.Ctx(self.uid)
            rows = {r["key"]: r for r in c.reco_rows()}
        row = rows["untiered label"]
        self.assertIsNone(row["tier"])
        # seul le terme "collection" est en jeu -> rétrécissement fort vers PRIOR=45,
        # mais un score bien supérieur à ce qu'un poids tier=5 nul écraserait à 0.
        self.assertGreater(row["score"], 30)

    # ---------------------------------------------------- constat 3 : voisinage

    def test_label_db_signal_neighbor_seul_reste_non_nul(self):
        """`Ctx.label_db_signal` directement (sans passer par reco_rows) :
        un label sans direct ni graphe, voisin d'un label suivi, obtient un
        score > 0 -- avant ce lot, `neighbors()`/`neighbors_for()` n'avait
        AUCUN appelant (constat 3 du brief)."""
        cfg = self._cfg()
        cfg["label_categories"] = {"1": ["Seed Label"], "2": []}
        store.save_config(cfg, self.uid)
        store._CONFIG_CACHE.clear()
        with mock.patch.object(clg, "available", return_value=True), \
             mock.patch.object(clg, "neighbors_for", return_value={
                 "seed label": [{"label_key": "discovered label", "kind": "artist",
                                  "weight": 4, "role": None}]}):
            c = scoring.Ctx(self.uid)
            sig = c.label_db_signal()
        self.assertIn("discovered label", sig)
        self.assertGreater(sig["discovered label"], 0)
        self.assertNotIn("seed label", sig, "un label suivi n'a pas besoin d'être 'découvert' par lui-même")

    def test_label_db_signal_sans_graphe_construit_inchange(self):
        """Non-régression : `catalog_labelgraph` pas encore construit (cas
        réel avant le premier `build_catalog_labelgraph`) -> comportement
        strictement identique à avant ce lot (direct+graphe seulement)."""
        cfg = self._cfg()
        cfg["label_categories"] = {"1": ["Seed Label"], "2": []}
        store.save_config(cfg, self.uid)
        store._CONFIG_CACHE.clear()
        with mock.patch.object(clg, "available", return_value=False):
            c = scoring.Ctx(self.uid)
            sig = c.label_db_signal()
        self.assertEqual(sig, {})


if __name__ == "__main__":
    unittest.main()
