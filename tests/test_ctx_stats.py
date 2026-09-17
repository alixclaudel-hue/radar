"""Diagnostic VPS 17/09 : `/patte` (page d'accueil, `Ctx.stats()`) mettait
135s à charger à froid -- `stats()["artists_identified"]` faisait
`len(self.ascore)`, forçant tout `graph_rescore()` (153 182 artistes,
623 747 co-occurrences mesurés en prod) pour n'en garder que la LONGUEUR.

Corrigé : `Ctx._identified_artist_keys()` calcule le même ensemble de clés
sans passer par le score/`why` de `graph_rescore` -- ce test vérifie
l'ÉQUIVALENCE mathématique exacte avec `set(ascore)` (pas juste "ça
tourne"), sur un graphe synthétique.

Corrigé aussi : `Ctx._key` incluait la mtime du fichier de config ENTIER
-- enregistrer un réglage sans rapport (ex. une clé API YouTube) jetait le
cache de `graph_rescore`/`ascore`. `_config_scoring_sig()` n'empreinte plus
que `scoring`/`artist_categories`/`label_categories`/`taste_categories`,
les seuls sous-arbres lus par les nœuds mémoïsés (`NODE_DEPS`).

Isole `Ctx` de `/data` réel en patchant `paths.USERS_DIR`/`SHARED_DIR` vers
un `tempfile.TemporaryDirectory()`.

Lancer : python3 -m unittest tests.test_ctx_stats -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar_web.radar import paths, store, scoring  # noqa: E402


class CtxStatsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        users_dir = os.path.join(self._tmp.name, "users")
        shared_dir = os.path.join(self._tmp.name, "shared")
        for p in (mock.patch.object(paths, "USERS_DIR", users_dir),
                  mock.patch.object(paths, "SHARED_DIR", shared_dir)):
            p.start()
            self.addCleanup(p.stop)
        # `store.read_config` mémorise par chemin de fichier : chaque test a son
        # propre tmp donc son propre chemin, mais on repart propre par prudence.
        store._CONFIG_CACHE.clear()
        self.addCleanup(store._CONFIG_CACHE.clear)

        self.uid = paths.DEFAULT_UID
        self.P = paths.user_paths(self.uid)
        cfg = store.load_config(self.uid)
        cfg["artist_categories"] = {"1": ["Artist Coeur"], "2": []}
        cfg["label_categories"] = {"1": ["Label Coeur"], "2": []}
        store.save_config(cfg, self.uid)

        store.save(self.P.corpus, [
            {"artist": "Corpus Artist", "source": "youtube"},
            {"artist": "Various", "source": "youtube"},  # placeholder, doit être filtré
        ])
        store.save(self.P.collection, {"artist_counts": {"Collection Artist": 3}})
        # graphe synthétique : c'est CE calcul (co-occurrences par artiste) que
        # graph_rescore()/ascore payaient en entier pour un simple `len()`.
        edges = {}
        for i in range(500):
            ck = f"graphartist{i}"
            co = {f"seed{j}": {"n": j + 1, "rw": 1.0} for j in range(5)}
            edges[ck] = {"name": ck, "id": None, "co": co}
        store.save(self.P.graph, {"edges": edges, "labels": {},
                                   "seeds": {f"seed{j}": f"Seed {j}" for j in range(5)}})
        store.save(self.P.profile, {})
        store.save(self.P.artists_res, {})
        store.save(self.P.resolved, {})

    def test_identified_artist_keys_equivaut_a_ascore(self):
        c = scoring.Ctx(self.uid)
        exact = set(c.ascore)
        cheap = c._identified_artist_keys()
        self.assertEqual(exact, cheap)
        self.assertGreater(len(exact), 0)

    def test_stats_utilise_le_calcul_bon_marche(self):
        c = scoring.Ctx(self.uid)
        st = c.stats()
        self.assertEqual(st["artists_identified"], len(c._identified_artist_keys()))

    def test_key_stable_sur_reglage_hors_scope(self):
        c1 = scoring.Ctx(self.uid)
        cfg = store.load_config(self.uid)
        cfg["youtube_api_key"] = "une-cle-sans-rapport"
        store.save_config(cfg, self.uid)
        c2 = scoring.Ctx(self.uid)
        self.assertEqual(c1._key, c2._key)

    def test_key_invalide_sur_changement_artist_categories(self):
        c1 = scoring.Ctx(self.uid)
        cfg = store.load_config(self.uid)
        cfg["artist_categories"]["1"].append("Nouvel Artiste")
        store.save_config(cfg, self.uid)
        c2 = scoring.Ctx(self.uid)
        self.assertNotEqual(c1._key, c2._key)

    def test_key_invalide_sur_changement_scoring(self):
        c1 = scoring.Ctx(self.uid)
        cfg = store.load_config(self.uid)
        cfg["scoring"]["artist_score"]["manual"] = 0.9
        store.save_config(cfg, self.uid)
        c2 = scoring.Ctx(self.uid)
        self.assertNotEqual(c1._key, c2._key)


if __name__ == "__main__":
    unittest.main()
