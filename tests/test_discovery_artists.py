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


class TestDiscoveryArtists(unittest.TestCase):
    def test_graphe_vide_ou_none_retourne_un_dictionnaire_vide(self):
        """Vérifie qu'un graphe absent ou sans arêtes ne produit aucun artiste."""
        for graph in (None, {}, {"edges": {}}):
            with self.subTest(graph=graph):
                ctx = FakeCtx(graph, {"graine": "Graine"})
                self.assertEqual(discovery_artists(ctx), {})

    def test_tiers_vide_retourne_un_dictionnaire_vide(self):
        """Vérifie qu'aucune graine disponible n'empêche pas le résultat d'être vide."""
        ctx = FakeCtx(
            {"edges": {"artiste": {"name": "Artiste", "id": 1, "co": {}}}},
            {},
        )

        self.assertEqual(discovery_artists(ctx), {})

    def test_seuil_min_n_exclut_ou_inclut_les_voisins(self):
        """Vérifie le seuil minimal de co-crédits et l'inclusion à égalité."""
        graph = {
            "edges": {
                "artiste_2": {
                    "name": "Artiste 2",
                    "id": 2,
                    "co": {"graine": {"n": 2, "rw": 1.0}},
                },
                "artiste_3": {
                    "name": "Artiste 3",
                    "id": 3,
                    "co": {"graine": {"n": 3, "rw": 1.0}},
                },
            },
            "seeds": {"graine": {"name": "Graine"}},
        }
        ctx = FakeCtx(graph, {"graine": "Graine"})

        résultat = discovery_artists(ctx, k=10, min_n=3)

        self.assertEqual(set(résultat), {"artiste_3"})
        self.assertEqual(résultat["artiste_3"]["score"], 3.0)

    def test_k_limite_les_voisins_par_graine(self):
        """Vérifie que seuls les k voisins les plus forts sont conservés."""
        graph = {
            "edges": {
                "voisin_6": {
                    "name": "Voisin 6",
                    "id": 6,
                    "co": {"graine": {"n": 6, "rw": 1.0}},
                },
                "voisin_7": {
                    "name": "Voisin 7",
                    "id": 7,
                    "co": {"graine": {"n": 7, "rw": 1.0}},
                },
                "voisin_8": {
                    "name": "Voisin 8",
                    "id": 8,
                    "co": {"graine": {"n": 8, "rw": 1.0}},
                },
                "voisin_9": {
                    "name": "Voisin 9",
                    "id": 9,
                    "co": {"graine": {"n": 9, "rw": 1.0}},
                },
            },
            "seeds": {"graine": {"name": "Graine"}},
        }
        ctx = FakeCtx(graph, {"graine": "Graine"})

        résultat = discovery_artists(ctx, k=2, min_n=3)

        self.assertEqual(set(résultat), {"voisin_8", "voisin_9"})
        self.assertEqual(résultat["voisin_8"]["score"], 8.0)
        self.assertEqual(résultat["voisin_9"]["score"], 9.0)

    def test_egalite_de_n_est_departagee_par_cle_croissante(self):
        """Vérifie le départage stable par clé lors d'égalités de co-crédits."""
        graph = {
            "edges": {
                "zeta": {
                    "name": "Zeta",
                    "id": 1,
                    "co": {"graine": {"n": 5, "rw": 1.0}},
                },
                "alpha": {
                    "name": "Alpha",
                    "id": 2,
                    "co": {"graine": {"n": 5, "rw": 1.0}},
                },
                "beta": {
                    "name": "Beta",
                    "id": 3,
                    "co": {"graine": {"n": 5, "rw": 1.0}},
                },
            },
            "seeds": {"graine": {"name": "Graine"}},
        }
        ctx = FakeCtx(graph, {"graine": "Graine"})

        résultat = discovery_artists(ctx, k=2, min_n=3)

        self.assertEqual(set(résultat), {"alpha", "beta"})
        self.assertNotIn("zeta", résultat)

    def test_union_des_graines_et_tri_des_graines_par_n_decroissant(self):
        """Vérifie l'union des relations et l'ordre décroissant des graines."""
        graph = {
            "edges": {
                "artiste": {
                    "name": "Artiste",
                    "id": 42,
                    "co": {
                        "graine_faible": {"n": 4, "rw": 1.0},
                        "graine_forte": {"n": 9, "rw": 1.0},
                    },
                },
            },
            "seeds": {
                "graine_faible": {"name": "Graine faible"},
                "graine_forte": {"name": "Graine forte"},
            },
        }
        ctx = FakeCtx(
            graph,
            {"graine_faible": "Graine faible", "graine_forte": "Graine forte"},
        )

        résultat = discovery_artists(ctx, min_n=3)

        self.assertEqual(
            résultat["artiste"],
            {
                "name": "Artiste",
                "id": 42,
                "score": 13.0,
                "why": ["9× avec Graine forte", "4× avec Graine faible"],
                "seeds": ["Graine forte", "Graine faible"],
            },
        )

    def test_voisin_deja_graine_est_exclu(self):
        """Vérifie qu'un voisin qui est lui-même une graine est exclu."""
        graph = {
            "edges": {
                "artiste": {
                    "name": "Artiste",
                    "id": 1,
                    "co": {"graine": {"n": 5, "rw": 1.0}},
                },
            },
            "seeds": {
                "graine": {"name": "Graine"},
                "artiste": {"name": "Artiste"},
            },
        }
        ctx = FakeCtx(graph, {"graine": "Graine", "artiste": "Artiste"})

        self.assertEqual(discovery_artists(ctx), {})

    def test_voisin_present_dans_corpus_est_exclu(self):
        """Vérifie qu'un artiste présent dans le corpus est exclu."""
        graph = {
            "edges": {
                "artiste": {
                    "name": "Artiste",
                    "id": 1,
                    "co": {"graine": {"n": 5, "rw": 1.0}},
                },
            },
            "seeds": {"graine": {"name": "Graine"}},
        }
        ctx = FakeCtx(
            graph,
            {"graine": "Graine"},
            corpus=[{"artist": "  ARTISTE  "}],
        )

        self.assertEqual(discovery_artists(ctx), {})

    def test_voisin_present_dans_collection_est_exclu(self):
        """Vérifie qu'un artiste présent dans la collection est exclu."""
        graph = {
            "edges": {
                "artiste": {
                    "name": "Artiste",
                    "id": 1,
                    "co": {"graine": {"n": 5, "rw": 1.0}},
                },
            },
            "seeds": {"graine": {"name": "Graine"}},
        }
        ctx = FakeCtx(
            graph,
            {"graine": "Graine"},
            collection={"artist_counts": {" Artiste ": 2}},
        )

        self.assertEqual(discovery_artists(ctx), {})

    def test_score_et_why_utilisent_le_rescoring_ou_le_score_de_fallback(self):
        """Vérifie le rescoring prioritaire et le calcul de repli des explications."""
        graph = {
            "edges": {
                "artiste_rescore": {
                    "name": "Artiste rescore",
                    "id": 1,
                    "co": {"graine": {"n": 4, "rw": 1.0}},
                },
                "artiste_fallback": {
                    "name": "Artiste fallback",
                    "id": 2,
                    "co": {"graine": {"n": 3, "rw": 1.0}},
                },
            },
            "seeds": {"graine": {"name": "Graine"}},
        }
        ctx = FakeCtx(
            graph,
            {"graine": "Graine"},
            rescore={
                "artiste_rescore": {
                    "score": 99.5,
                    "why": ["Score externe"],
                }
            },
        )

        résultat = discovery_artists(ctx, min_n=3)

        self.assertEqual(résultat["artiste_rescore"]["score"], 99.5)
        self.assertEqual(résultat["artiste_rescore"]["why"], ["Score externe"])
        self.assertEqual(résultat["artiste_fallback"]["score"], 3.0)
        self.assertEqual(
            résultat["artiste_fallback"]["why"],
            ["3× avec Graine"],
        )

    def test_graph_rescore_est_appele_au_plus_une_fois(self):
        """Vérifie qu'un seul rescoring est demandé pour plusieurs artistes."""
        graph = {
            "edges": {
                "artiste_a": {
                    "name": "Artiste A",
                    "id": 1,
                    "co": {"graine": {"n": 5, "rw": 1.0}},
                },
                "artiste_b": {
                    "name": "Artiste B",
                    "id": 2,
                    "co": {"graine": {"n": 4, "rw": 1.0}},
                },
            },
            "seeds": {"graine": {"name": "Graine"}},
        }
        ctx = FakeCtx(graph, {"graine": "Graine"})

        résultat = discovery_artists(ctx, min_n=3)

        self.assertEqual(set(résultat), {"artiste_a", "artiste_b"})
        self.assertEqual(ctx.graph_rescore_calls, 1)


if __name__ == "__main__":
    unittest.main()
