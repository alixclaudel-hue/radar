import unittest

from scripts.ai import router
from scripts.bench_ai_broker import _catalogue, _model, _provider


class TestRouterBlockedIds(unittest.TestCase):
    """Le 27/09, `thinkingmachines/inkling-small:free` a répondu 403 en boucle
    (restriction commerciale du modèle, jamais utilisable ici). `blocked_ids`
    doit l'écarter de la cascade avant même le premier essai, sans toucher aux
    autres modèles du même fournisseur."""

    def test_blocked_id_exclu_des_candidats(self):
        cat = _catalogue(
            {
                "or": _provider(
                    "or",
                    keys_env=["OPENROUTER_API_KEY"],
                    models=[
                        _model("bon-modele"),
                        _model("thinkingmachines/inkling-small:free"),
                    ],
                ),
            },
            policy={"blocked_ids": ["thinkingmachines/inkling-small:free"]},
        )
        candidats = router.order_candidates(
            cat,
            mode="context",
            tier="fast",
            key_lookup=lambda nom: "dummy-key",
            quota_state={},
            health_state={},
        )
        ids = {c.model for c in candidats}
        self.assertIn("bon-modele", ids)
        self.assertNotIn("thinkingmachines/inkling-small:free", ids)

    def test_sans_blocked_ids_rien_n_est_filtre(self):
        cat = _catalogue(
            {
                "or": _provider(
                    "or",
                    keys_env=["OPENROUTER_API_KEY"],
                    models=[_model("bon-modele")],
                ),
            },
        )
        candidats = router.order_candidates(
            cat,
            mode="context",
            tier="fast",
            key_lookup=lambda nom: "dummy-key",
            quota_state={},
            health_state={},
        )
        self.assertEqual({c.model for c in candidats}, {"bon-modele"})


if __name__ == "__main__":
    unittest.main()
