"""Diagnostic VPS 17/09 (session distincte du pt 54) : `/univers?tab=cart`
mettait ~190s à froid alors que la wantlist n'a besoin que de `cart.json`.
Cause : `univers_page()` calculait `top_aff`/`top_reco` (raccourcis du
graphe de labels, onglet "labels" uniquement) quel que soit l'onglet ->
`_pick_base_labels` -> `_base_labels_ranked` -> `c.reco_index` -> `ascore`
-> `artist_label_signal` -> deux GROUP BY sur tout le dump Discogs (181s
mesurés en prod sur 10 187 labels).

Corrigé en deux temps :
- A-1 : `univers_page()` ne calcule `top_aff`/`top_reco` que si `tab == "labels"`.
- A-2 : `_base_labels_ranked(c, need_reco=...)` n'accède à `c.reco_index` que si
  l'appelant trie/affiche réellement par reco (sort=="reco" / metric=="reco") --
  jamais pour un tri par affinité/possédés ni pour la recherche de nom.
- B : `/wantlist` devient une vraie page dédiée (plus un 4e onglet caché de
  "Mes labels & artistes"), sans `Ctx()` du tout -- `/univers?tab=cart`
  redirige (303) pour ne pas casser les signets.

Garde-fou (comme `test_ctx_stats.py`) : inspection du cache `_DERIVED` après
appel, jamais un chronométrage (instable en CI).

Lancer : python3 -m unittest tests.test_wantlist_perf -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

from fastapi.testclient import TestClient  # noqa: E402

from radar_web import app as appmod  # noqa: E402
from radar_web.radar import paths, store, scoring  # noqa: E402

_COSTLY_NODES = ("reco_index", "reco_rows", "ascore", "artist_label_signal", "label_artist_signal")


class WantlistPerfTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        p = mock.patch.object(appmod, "_dev_mode", return_value=True)
        p.start()
        self.addCleanup(p.stop)

        self.uid = paths.DEFAULT_UID
        cfg = store.load_config(self.uid)
        cfg["label_categories"] = {"1": ["Label Coeur"], "2": ["Label Aime"]}
        store.save_config(cfg, self.uid)

        scoring._DERIVED.clear()
        self.addCleanup(scoring._DERIVED.clear)

    def _assert_no_costly_node(self):
        for slot in scoring._DERIVED.values():
            for node in _COSTLY_NODES:
                self.assertNotIn(node, slot,
                                  f"{node!r} calculé alors qu'il ne devrait pas l'être")

    def test_univers_tab_cart_redirige_sans_creer_de_ctx(self):
        with mock.patch.object(appmod, "Ctx",
                                side_effect=AssertionError("Ctx() ne doit pas être créé pour rediriger tab=cart")):
            r = self.client.get("/univers?tab=cart", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/wantlist")

    def test_wantlist_ne_cree_aucun_ctx(self):
        with mock.patch.object(appmod, "Ctx",
                                side_effect=AssertionError("Ctx() ne doit pas être créé sur /wantlist")):
            r = self.client.get("/wantlist")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Wantlist", r.text)

    def test_univers_autres_onglets_ne_declenchent_pas_reco_index(self):
        for tab in ("artists", "sets"):
            with self.subTest(tab=tab):
                scoring._DERIVED.clear()
                r = self.client.get(f"/univers?tab={tab}")
                self.assertEqual(r.status_code, 200)
                self._assert_no_costly_node()

    def test_univers_tab_labels_calcule_bien_les_raccourcis(self):
        """Non-régression : l'onglet labels a toujours besoin de reco_index
        pour ses raccourcis "mes + recommandations" -- seul cet onglet doit
        payer le coût."""
        r = self.client.get("/univers?tab=labels")
        self.assertEqual(r.status_code, 200)
        slot = next(iter(scoring._DERIVED.values()), {})
        self.assertIn("reco_index", slot)

    def test_base_labels_ranked_need_reco_false_evite_reco_index(self):
        c = scoring.Ctx(self.uid)
        rows = appmod._base_labels_ranked(c, need_reco=False)
        self._assert_no_costly_node()
        self.assertTrue(all(r["_reco"] is None for r in rows))

    def test_base_labels_ranked_need_reco_true_calcule_reco_index(self):
        c = scoring.Ctx(self.uid)
        appmod._base_labels_ranked(c, need_reco=True)
        slot = scoring._DERIVED.get(c._key, {})
        self.assertIn("reco_index", slot)

    def test_search_labels_ne_declenche_pas_reco_index(self):
        r = self.client.get("/search/labels?q=label")
        self.assertEqual(r.status_code, 200)
        self._assert_no_costly_node()

    def test_pick_base_labels_metric_reco_calcule_reco_index(self):
        c = scoring.Ctx(self.uid)
        appmod._pick_base_labels(c, "reco", None, 5)
        slot = scoring._DERIVED.get(c._key, {})
        self.assertIn("reco_index", slot)

    def test_pick_base_labels_metric_aff_evite_reco_index(self):
        c = scoring.Ctx(self.uid)
        appmod._pick_base_labels(c, "aff", None, 5)
        self._assert_no_costly_node()


if __name__ == "__main__":
    unittest.main()
