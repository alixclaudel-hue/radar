"""Correctifs courtier et part déléguée (27/09) : santé par contrat, .env de worktree,
--model fournisseur:modèle, jetons en échec exclus de la part déléguée."""
import os
import tempfile
import unittest
from unittest import mock

from radar_ops import delegation, workflows
from scripts.ai import catalogue, health, router

class ContractFailureTests(unittest.TestCase):
    def test_etat_vide_retourne_zero(self):
        state = {}
        self.assertEqual(health.contract_failures(state, 'p', 'm', 'context'), 0)

    def test_deux_echecs_meme_mode_compte_deux_zero_pour_autre_mode(self):
        state = {}
        health.record_contract_failure(state, 'p', 'm', 'context', now=1000.0)
        health.record_contract_failure(state, 'p', 'm', 'context', now=1000.0)
        self.assertEqual(health.contract_failures(state, 'p', 'm', 'context', now=1000.0), 2)
        self.assertEqual(health.contract_failures(state, 'p', 'm', 'code', now=1000.0), 0)

    def test_echec_expire_hors_fenetre_retourne_zero(self):
        state = {}
        health.record_contract_failure(state, 'p', 'm', 'context', now=0.0)
        self.assertEqual(
            health.contract_failures(state, 'p', 'm', 'context', now=health.CONTRACT_WINDOW + 1),
            0
        )

    def test_record_success_ne_reinitialise_pas_contract_failures(self):
        state = {}
        health.record_contract_failure(state, 'p', 'm', 'context', now=1000.0)
        health.record_success(state, 'p', 'm')
        self.assertEqual(health.contract_failures(state, 'p', 'm', 'context', now=1000.0), 1)

    def test_state_none_retourne_zero(self):
        self.assertEqual(health.contract_failures(None, 'p', 'm', 'context'), 0)


class ShareExcludesFailuresTests(unittest.TestCase):
    """Tests de non-régression : les échecs ne comptent pas dans la part déléguée."""

    def test_sankey_exclut_echecs_du_numerateur(self):
        """Sankey : useful_delegated_tokens exclut failed_tokens (part sur équivalents)."""
        req = {
            'totals': {
                'claude': {
                    'input': 100,
                    'cache_read': 10000,
                    'cache_create': 0,
                    'output': 0
                }
            },
            'steps': [
                {
                    'kind': 'broker',
                    'label': 'mode code',
                    'receipt': {
                        'status': 'ok',
                        'provider': 'p',
                        'model': 'm',
                        'prompt_tokens': 50,
                        'output_tokens': 50
                    },
                    'attempts': [
                        {
                            'status': 'error',
                            'tokens': {
                                'prompt_tokens': 300,
                                'output_tokens': 0
                            }
                        }
                    ]
                }
            ]
        }
        result = workflows.sankey([req])
        totals = result['totals']
        self.assertEqual(totals['useful_delegated_tokens'], 100)
        self.assertEqual(totals['failed_tokens'], 300)
        # share_delegated se calcule maintenant en tokens équivalents : le
        # cache relu (10000, pondéré 0,1 -> 1000) entre dans claude_equiv au
        # lieu d'en être exclu, ce qui change le ratio par rapport à l'ancien
        # calcul sur jetons bruts (qui donnait 0.5).
        self.assertEqual(totals['claude_equiv'], 100 + 10000 * 0.10)
        self.assertEqual(totals['delegated_equiv'], 50 + 50 * 5.0 + 300)
        self.assertEqual(totals['failed_equiv'], 300)
        self.assertEqual(totals['useful_delegated_equiv'], 300)
        self.assertEqual(totals['share_delegated'], round(300 / (300 + 1100), 3))

    def test_summary_calcul_part_deleguee_et_echecs(self):
        """Summary : share_delegated = 0.5 et delegated_failed_tokens = 400."""
        r = {
            'totals': {
                'claude': {'input': 100},
                'delegated': {
                    'prompt': 100,
                    'output': 0,
                    'failed_prompt': 400,
                    'failed_output': 0
                }
            }
        }
        summary = workflows.summary(r)
        self.assertEqual(summary['share_delegated'], 0.5)
        self.assertEqual(summary['delegated_failed_tokens'], 400)


class SousTraitanceTests(unittest.TestCase):
    def test_echecs_et_cache_relu_hors_part(self):
        # Champs bruts inchangés (cache relu toujours exclu de claude_tokens) ;
        # share_delegated, lui, se calcule désormais sur les équivalents, où le
        # cache relu (10000 jetons) est pondéré à 0,1 (1000) plutôt qu'exclu :
        # claude_equiv = 100 (input) + 1000 = 1100, delegated_equiv = 100 (utile).
        r = delegation._sous_traitance(
            {"total_tokens": 1000, "useful_tokens": 100, "failed_tokens": 900,
             "equiv_tokens": 1000, "useful_equiv_tokens": 100, "failed_equiv_tokens": 900},
            {"total_tokens": 10100, "cache_read_tokens": 10000, "equiv_tokens": 1100},
        )
        self.assertEqual(r["delegated_tokens"], 100)
        self.assertEqual(r["claude_tokens"], 100)
        self.assertEqual(r["delegated_failed_tokens"], 900)
        self.assertEqual(r["delegated_equiv"], 100)
        self.assertEqual(r["claude_equiv"], 1100)
        self.assertEqual(r["share_delegated"], round(100 / 1200, 3))


class DotenvWorktreeTests(unittest.TestCase):
    def test_worktree_repli_sur_env_du_depot_principal(self):
        with tempfile.TemporaryDirectory() as d:
            wt = os.path.join(d, ".worktrees", "x")
            os.makedirs(wt)
            with open(os.path.join(wt, ".git"), "w") as f:
                f.write(f"gitdir: {d}/.git/worktrees/x\n")
            with mock.patch.object(catalogue, "repo_root", return_value=wt):
                self.assertEqual(catalogue.dotenv_paths(),
                                 [os.path.join(wt, ".env"), os.path.join(d, ".env")])

    def test_checkout_principal_un_seul_env(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".git"))
            with mock.patch.object(catalogue, "repo_root", return_value=d):
                self.assertEqual(catalogue.dotenv_paths(), [os.path.join(d, ".env")])


class ModeleExpliciteTests(unittest.TestCase):
    CATALOGUE = {"providers": {"gemini": {
        "kind": "gemini", "keys_env": ["K"], "quota": {"window": "daily", "limit": None},
        "models": [{"id": "g-lite", "free": True, "context_length": 100000}],
    }}}

    def _retenus(self, explicite):
        infos = router.explain(self.CATALOGUE, mode="general", explicit_model=explicite,
                               key_lookup=lambda nom: "k")
        return [i["model"] for i in infos if i.get("included")]

    def test_forme_fournisseur_modele_acceptee(self):
        self.assertEqual(self._retenus("gemini:g-lite"), self._retenus("g-lite"))

    def test_modele_inconnu_ecarte(self):
        self.assertEqual(self._retenus("gemini:autre"), [])


class RangContratTests(unittest.TestCase):
    CATALOGUE = {"providers": {"gemini": {
        "kind": "gemini", "keys_env": ["K"], "quota": {"window": "daily", "limit": None},
        "models": [{"id": "a", "free": True, "rank": 1, "context_length": 100000},
                   {"id": "b", "free": True, "rank": 2, "context_length": 100000}],
    }}}

    def _ordre(self, etat):
        return [c.model for c in router.order_candidates(
            self.CATALOGUE, mode="context", health_state=etat, key_lookup=lambda n: "k")]

    def test_echec_de_contrat_recule_le_modele_dans_ce_mode_seulement(self):
        etat = {}
        self.assertEqual(self._ordre(etat), ["a", "b"])
        health.record_contract_failure(etat, "gemini", "a", "context")
        self.assertEqual(self._ordre(etat), ["b", "a"])
        health.record_contract_failure(etat, "gemini", "b", "code")
        self.assertEqual(self._ordre(etat), ["b", "a"])


if __name__ == "__main__":
    unittest.main()
