import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from radar_web.radar.discovery import discovery_artists


class FakeCtx:
    def __init__(self, graph, tiers, corpus=None, collection=None, rescore=None):
        self.graph = graph
        self.corpus = corpus
        self.collection = collection
        self._tiers = tiers
        self._rescore = rescore
        self.graph_rescore_calls = 0

    def artist_tier_map(self):
        return self._tiers

    def canon_artist_key(self, name):
        return name.strip().lower()

    def graph_rescore(self):
        self.graph_rescore_calls += 1
        return {
            "artists": self._rescore or {},
            "labels": {},
        }


class TestDiscoverySeedsStrings(unittest.TestCase):
    def test_graine_cha_inee_produit_seeds_et_why_de_repli(self):
        """Vérifie qu'une graine textuelle est résolue sans erreur et utilisée dans le repli."""
        graph = {
            "edges": {
                "ck": {
                    "name": "Artiste CK",
                    "id": 1,
                    "co": {
                        "graine_1": {"n": 3, "rw": 1.0},
                    },
                },
            },
            "seeds": {"graine_1": "Nom De La Graine"},
        }
        ctx = FakeCtx(
            graph,
            {"graine_1": "Nom De La Graine"},
            rescore={
                "autre_artiste": {
                    "score": 99.0,
                    "why": ["Explication externe"],
                },
            },
        )

        résultat = discovery_artists(ctx, min_n=3)

        self.assertEqual(résultat["ck"]["seeds"], ["Nom De La Graine"])
        self.assertEqual(
            résultat["ck"]["why"],
            ["3× avec Nom De La Graine"],
        )


if __name__ == "__main__":
    unittest.main()
