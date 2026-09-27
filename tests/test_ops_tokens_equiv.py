"""Tests des tokens équivalents (`radar_ops.tokens`) et de leur intégration
dans `radar_ops.delegation` / `radar_ops.workflows`."""
import unittest

from radar_ops import delegation, tokens, workflows


class TestTokensEquiv(unittest.TestCase):
    """Cas de base délégués (`--mode test`, cf. reçu du courtier)."""

    def test_formule_de_base_sans_ventilation(self):
        # 1000*1.0 + 1000*0.10 + 1000*2.0 + 1000*5.0 : sans cache_write_1h/5m,
        # tout le cache écrit est compté au tarif 1h (2.0), pas au repli agrégé.
        result = tokens.equiv(input=1000, cache_read=1000, cache_write=1000, output=1000)
        self.assertEqual(result, 8100)

    def test_repli_non_ventile(self):
        result = tokens.equiv(cache_write=500)
        self.assertEqual(result, 1000)


class TestTokensEquivVentilation(unittest.TestCase):
    """Ventilation explicite 1h/5m de l'écriture de cache."""

    def test_ventilation_1h_5m_sans_reliquat(self):
        # 600*2.0 + 400*1.25, rien au-delà (600+400 == cache_write).
        result = tokens.equiv(cache_write=1000, cache_write_1h=600, cache_write_5m=400)
        self.assertEqual(result, 1700)

    def test_ventilation_avec_reliquat_compte_au_tarif_1h(self):
        # Le reliquat (1000 - 200 = 800, non identifié 1h ni 5m) est compté au
        # tarif 1h (2.0) : 200*1.25 + 800*2.0.
        result = tokens.equiv(cache_write=1000, cache_write_1h=0, cache_write_5m=200)
        self.assertEqual(result, 1850)

    def test_ventilation_a_zero_distincte_de_non_fournie(self):
        # cache_write_1h=0 (pas None) déclenche quand même la branche ventilée :
        # même résultat numérique que le repli ici, mais par un autre chemin.
        result = tokens.equiv(cache_write=500, cache_write_1h=0, cache_write_5m=0)
        self.assertEqual(result, 1000)


class TestTokensEquivCacheAgg(unittest.TestCase):
    """Repli `cache_agg` pour un cache non ventilé lecture/écriture."""

    def test_cache_agg_ignore_cache_read_et_cache_write(self):
        # 100*1.0 + 1000*0.15 + 10*5.0 = 300, cache_read/cache_write fournis
        # mais ignorés dès que cache_agg est présent.
        result = tokens.equiv(input=100, output=10, cache_read=99999,
                              cache_write=99999, cache_agg=1000)
        self.assertEqual(result, 300)

    def test_tout_a_zero(self):
        self.assertEqual(tokens.equiv(), 0)

    def test_weights_public_est_une_copie(self):
        w = tokens.weights_public()
        w["input"] = 999
        self.assertEqual(tokens.WEIGHTS["input"], 1.0)


class TestSousTraitanceEquiv(unittest.TestCase):
    """`delegation._sous_traitance` : part sur équivalents, cache relu pondéré."""

    def test_part_sur_equivalents_cache_relu_pondere(self):
        # Côté Claude : input=100, cache_read=10000 -> équivalent 100 + 1000 = 1100.
        # Côté courtier : 100 jetons utiles -> équivalent 100 (pas de cache/sortie).
        r = delegation._sous_traitance(
            {"total_tokens": 1000, "useful_tokens": 100, "failed_tokens": 900,
             "equiv_tokens": 1000, "useful_equiv_tokens": 100, "failed_equiv_tokens": 900},
            {"total_tokens": 10100, "cache_read_tokens": 10000, "equiv_tokens": 1100},
        )
        self.assertEqual(r["delegated_equiv"], 100)
        self.assertEqual(r["claude_equiv"], 1100)
        self.assertEqual(r["total_equiv"], 1200)
        self.assertEqual(r["share_delegated"], round(100 / 1200, 3))
        # Les champs bruts restent inchangés (cache relu toujours exclu de claude_tokens).
        self.assertEqual(r["claude_tokens"], 100)
        self.assertEqual(r["delegated_tokens"], 100)

    def test_les_deux_equiv_a_zero_share_none(self):
        r = delegation._sous_traitance({}, {})
        self.assertIsNone(r["share_delegated"])
        self.assertEqual(r["total_equiv"], 0)


class TestSankeyValueEq(unittest.TestCase):
    """`workflows.sankey` : chaque lien porte `value` (brut) et `value_eq`."""

    def test_liens_portent_value_et_value_eq(self):
        req = {
            "totals": {
                "claude": {"input": 100, "cache_read": 10000, "cache_create": 0, "output": 0},
            },
            "steps": [{
                "kind": "broker", "label": "mode code",
                "receipt": {"status": "ok", "provider": "p", "model": "m",
                           "prompt_tokens": 50, "output_tokens": 50},
                "attempts": [{"status": "error",
                             "tokens": {"prompt_tokens": 300, "output_tokens": 0}}],
            }],
        }
        s = workflows.sankey([req])
        links = {(l["source"], l["target"]): l for l in s["links"]}

        # req -> claude : value brut = somme des jetons Claude (100 + 10000) ;
        # value_eq pondère le cache relu à 0,1 (100 + 10000*0.10 = 1100).
        req_claude = links[("req", "claude")]
        self.assertEqual(req_claude["value"], 10100)
        self.assertEqual(req_claude["value_eq"], 1100)

        # broker -> m:p/m : value brut = 100 (50+50) ; value_eq = 50 + 50*5.0 = 300.
        broker_model = links[("broker", "m:p/m")]
        self.assertEqual(broker_model["value"], 100)
        self.assertEqual(broker_model["value_eq"], 300)

        # broker -> failed : value brut = 300 (échec, entrée seule) ; value_eq = 300.
        failed = links[("broker", "failed")]
        self.assertEqual(failed["value"], 300)
        self.assertEqual(failed["value_eq"], 300)

        self.assertEqual(s["totals"]["claude_equiv"], 1100)
        self.assertEqual(s["totals"]["delegated_equiv"], 600)
        self.assertEqual(s["totals"]["failed_equiv"], 300)
        self.assertEqual(s["totals"]["useful_delegated_equiv"], 300)
        self.assertEqual(s["totals"]["share_delegated"], round(300 / (300 + 1100), 3))


class TestSummaryClaudeEquiv(unittest.TestCase):
    """`workflows.summary` : `claude_equiv`/`delegated_equiv` d'une requête."""

    def test_claude_equiv_cache_read_pondere_pas_exclu(self):
        r = {
            "totals": {
                "claude": {"input": 100, "cache_read": 10000, "cache_create": 0, "output": 0},
                "delegated": {"prompt": 100, "output": 0, "failed_prompt": 0, "failed_output": 0},
            },
        }
        summary = workflows.summary(r)
        self.assertEqual(summary["claude_equiv"], 100 + 10000 * 0.10)
        self.assertEqual(summary["delegated_equiv"], 100)
        self.assertEqual(summary["share_delegated"], round(100 / (100 + 1100), 3))

    def test_ventilation_1h_5m_depuis_totals_claude(self):
        r = {
            "totals": {
                "claude": {"input": 0, "cache_read": 0, "cache_create": 1000, "output": 0,
                          "cache_create_1h": 600, "cache_create_5m": 400},
                "delegated": {"prompt": 0, "output": 0, "failed_prompt": 0, "failed_output": 0},
            },
        }
        summary = workflows.summary(r)
        self.assertEqual(summary["claude_equiv"], 1700)  # 600*2.0 + 400*1.25

    def test_pas_de_ventilation_connue_repli_a_none(self):
        # Pas de clés cache_create_1h/5m du tout dans totals["claude"] : le
        # repli doit s'appliquer (tout le cache écrit à 2.0), pas planter.
        r = {
            "totals": {
                "claude": {"input": 0, "cache_read": 0, "cache_create": 1000, "output": 0},
                "delegated": {"prompt": 0, "output": 0, "failed_prompt": 0, "failed_output": 0},
            },
        }
        summary = workflows.summary(r)
        self.assertEqual(summary["claude_equiv"], 2000)  # 1000*2.0


class TestClaudeUsageRowsEquiv(unittest.TestCase):
    def test_ligne_claude_porte_ses_equivalents(self):
        rows = delegation._claude_usage_rows([{
            "kind": "claude_usage", "ts": 1.0, "model": "m",
            "input_tokens": 10, "output_tokens": 2,
            "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100,
            "cache_creation_1h_input_tokens": 60, "cache_creation_5m_input_tokens": 40,
        }])
        # 10 + 1000×0,1 + 60×2 + 40×1,25 + 2×5
        self.assertEqual(rows[0]["equiv_tokens"], 10 + 100 + 120 + 50 + 10)
        self.assertEqual(rows[0]["total_tokens"], 1112)


class DelegationTemplateUnitTests(unittest.TestCase):
    """La bascule bruts/équivalents rend sans erreur, y compris sur une valeur
    inconnue (repli « bruts ») et sans `unit` du tout (anciens appelants)."""

    def _render(self, **extra):
        import os
        import tempfile
        from unittest.mock import patch

        import jinja2
        env = jinja2.Environment(loader=jinja2.FileSystemLoader("radar_ops/templates"),
                                 autoescape=True)
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {}, clear=True):
            return env.get_template("delegation.html").render(
                d=delegation.snapshot_windowed(tmpdir, preset="7d"), sha="abcdef0", **extra)

    def test_mode_equivalents(self):
        html = self._render(unit="eq")
        self.assertIn("Jetons équivalents", html)
        self.assertIn('name="unit" value="eq"', html)

    def test_valeur_inconnue_et_absente_retombent_sur_bruts(self):
        for extra in ({"unit": "nimportequoi"}, {}):
            html = self._render(**extra)
            self.assertIn('name="unit" value="raw"', html)


if __name__ == "__main__":
    unittest.main()
