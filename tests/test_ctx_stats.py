"""Diagnostic VPS 17/09 : `/patte` (page d'accueil, `Ctx.stats()`) mettait
135s à charger à froid -- `stats()["artists_identified"]` faisait
`len(self.ascore)`, forçant tout `graph_rescore()` (153 182 artistes,
623 747 co-occurrences mesurés en prod) pour n'en garder que la LONGUEUR.

Traité en deux temps : d'abord un calcul équivalent bon marché, puis (choix
utilisateur du 17/09) suppression pure et simple des deux tuiles concernées
(« artistes croisés dans ton écoute », « liens entre artistes ») et donc des
deux compteurs. Le garde-fou qui reste est le bon : `stats()` ne doit
JAMAIS déclencher un nœud coûteux du DAG -- vérifié ici en inspectant le
cache `_DERIVED` après l'appel, pas en chronométrant (un temps mesuré serait
instable en CI).

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

    def test_stats_ne_declenche_aucun_noeud_couteux(self):
        """Le vrai garde-fou des 135s : après `stats()`, ni `graph_rescore`
        ni `ascore` ne doivent avoir été calculés (cache `_DERIVED` vide de
        ces nœuds). Réintroduire un compteur qui les touche casse ce test."""
        scoring._DERIVED.clear()
        self.addCleanup(scoring._DERIVED.clear)
        c = scoring.Ctx(self.uid)
        c.stats()
        slot = scoring._DERIVED.get(c._key, {})
        self.assertNotIn("graph_rescore", slot)
        self.assertNotIn("ascore", slot)

    def test_stats_ne_publie_plus_les_deux_compteurs_retires(self):
        """Tuiles « artistes croisés dans ton écoute » / « liens entre
        artistes » retirées de patte.html le 17/09 (demande utilisateur) :
        leurs compteurs partent avec, ils n'ont plus de consommateur."""
        st = scoring.Ctx(self.uid).stats()
        self.assertNotIn("artists_identified", st)
        self.assertNotIn("graph_edges", st)
        self.assertIn("tracks", st)  # les autres compteurs restent

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
