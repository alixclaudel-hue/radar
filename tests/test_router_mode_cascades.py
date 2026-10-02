import unittest

from scripts.ai import router
from scripts.bench_ai_broker import _catalogue, _model, _provider


class TestCascadesParModeDuRouteur(unittest.TestCase):
    '''Vérifie les cascades et exclusions déclarées par mode, sans accès externe.'''

    def test_cascade_place_b_puis_a_avant_c(self):
        cat = _catalogue(
            {
                'openrouter': _provider(
                    'openrouter',
                    keys_env=['OPENROUTER_API_KEY'],
                    models=[
                        _model('a'),
                        _model('b'),
                        _model('c'),
                    ],
                ),
            },
            policy={
                'mode_cascades': {
                    'code': ['openrouter:b', 'a'],
                }
            },
        )
        candidats = router.order_candidates(
            cat,
            mode='code',
            tier='fast',
            key_lookup=lambda nom: 'dummy-key',
            quota_state={},
            health_state={},
            now=0.0,
        )
        self.assertEqual(
            [(candidat.provider, candidat.model) for candidat in candidats],
            [
                ('openrouter', 'b'),
                ('openrouter', 'a'),
                ('openrouter', 'c'),
            ],
        )

    def test_mode_general_conserve_l_ordre_usuel(self):
        cat = _catalogue(
            {
                'openrouter': _provider(
                    'openrouter',
                    keys_env=['OPENROUTER_API_KEY'],
                    models=[
                        _model('a'),
                        _model('b'),
                        _model('c'),
                    ],
                ),
            },
            policy={
                'mode_cascades': {
                    'code': ['openrouter:b', 'a'],
                }
            },
        )
        candidats = router.order_candidates(
            cat,
            mode='general',
            tier='fast',
            prompt_tokens=0,
            key_lookup=lambda nom: 'dummy-key',
            quota_state={},
            health_state={},
            now=0.0,
        )
        self.assertEqual(
            [(candidat.provider, candidat.model) for candidat in candidats],
            [
                ('openrouter', 'a'),
                ('openrouter', 'b'),
                ('openrouter', 'c'),
            ],
        )

    def test_modele_exclu_du_mode_diag(self):
        cat = _catalogue(
            {
                'openrouter': _provider(
                    'openrouter',
                    keys_env=['OPENROUTER_API_KEY'],
                    models=[
                        _model('a'),
                        _model('b'),
                        _model('c'),
                    ],
                ),
            },
            policy={'mode_excluded': {'diag': ['c']}},
        )
        candidats_diag = router.order_candidates(
            cat,
            mode='diag',
            tier='fast',
            key_lookup=lambda nom: 'dummy-key',
            quota_state={},
            health_state={},
            now=0.0,
        )
        candidats_general = router.order_candidates(
            cat,
            mode='general',
            tier='fast',
            key_lookup=lambda nom: 'dummy-key',
            quota_state={},
            health_state={},
            now=0.0,
        )
        self.assertEqual(
            [candidat.model for candidat in candidats_diag],
            ['a', 'b'],
        )
        self.assertNotIn('c', [candidat.model for candidat in candidats_diag])
        self.assertIn('c', [candidat.model for candidat in candidats_general])

    def test_modele_exclu_reste_disponible_si_explicite(self):
        cat = _catalogue(
            {
                'openrouter': _provider(
                    'openrouter',
                    keys_env=['OPENROUTER_API_KEY'],
                    models=[
                        _model('a'),
                        _model('b'),
                        _model('c'),
                    ],
                ),
            },
            policy={'mode_excluded': {'diag': ['c']}},
        )
        candidats = router.order_candidates(
            cat,
            mode='diag',
            tier='fast',
            explicit_model='c',
            key_lookup=lambda nom: 'dummy-key',
            quota_state={},
            health_state={},
            now=0.0,
        )
        self.assertEqual(
            [(candidat.provider, candidat.model) for candidat in candidats],
            [('openrouter', 'c')],
        )

    def test_forme_complete_designe_le_bon_fournisseur(self):
        cat = _catalogue(
            {
                'openrouter': _provider(
                    'openrouter',
                    keys_env=['OPENROUTER_API_KEY'],
                    models=[_model('a')],
                ),
                'gemini': _provider(
                    'gemini',
                    keys_env=['GEMINI_API_KEY'],
                    models=[_model('a')],
                ),
            },
            policy={'mode_cascades': {'code': ['gemini:a']}},
        )
        candidats = router.order_candidates(
            cat,
            mode='code',
            tier='fast',
            key_lookup=lambda nom: 'dummy-key',
            quota_state={},
            health_state={},
            now=0.0,
        )
        self.assertEqual(
            [(candidat.provider, candidat.model) for candidat in candidats],
            [
                ('gemini', 'a'),
                ('openrouter', 'a'),
            ],
        )

    @unittest.skip(
        'Le fichier de référence ne montre pas la structure de health_state '
        'permettant de simuler fidèlement un échec de contrat.'
    )
    def test_echec_de_contrat_repousse_le_tete_de_cascade(self):
        pass

    def test_tables_absentes_ou_mal_typees_n_ont_aucun_effet(self):
        cat = _catalogue(
            {
                'openrouter': _provider(
                    'openrouter',
                    keys_env=['OPENROUTER_API_KEY'],
                    models=[
                        _model('a'),
                        _model('b'),
                        _model('c'),
                    ],
                ),
            },
        )
        for mode in ('code', 'diag'):
            with self.subTest(cas='tables absentes', mode=mode):
                candidats = router.order_candidates(
                    cat,
                    mode=mode,
                    tier='fast',
                    key_lookup=lambda nom: 'dummy-key',
                    quota_state={},
                    health_state={},
                    now=0.0,
                )
                self.assertEqual(
                    [candidat.model for candidat in candidats],
                    ['a', 'b', 'c'],
                )

        politiques = (
            (
                {'mode_cascades': None, 'mode_excluded': None},
                'tables nulles',
            ),
            (
                {'mode_cascades': ['code'], 'mode_excluded': ['diag']},
                'tables transformées en listes',
            ),
            (
                {
                    'mode_cascades': {'code': []},
                    'mode_excluded': {'diag': []},
                },
                'listes de mode vides',
            ),
            (
                {
                    'mode_cascades': {'code': ''},
                    'mode_excluded': {'diag': 'c'},
                },
                'valeurs textuelles mal typées',
            ),
            (
                {
                    'mode_cascades': {'code': [0]},
                    'mode_excluded': {'diag': [None]},
                },
                'identifiants zéro et nul',
            ),
        )
        for politique, libelle in politiques:
            cat = _catalogue(
                {
                    'openrouter': _provider(
                        'openrouter',
                        keys_env=['OPENROUTER_API_KEY'],
                        models=[
                            _model('a'),
                            _model('b'),
                            _model('c'),
                        ],
                    ),
                },
                policy=politique,
            )
            for mode in ('code', 'diag'):
                with self.subTest(cas=libelle, mode=mode):
                    candidats = router.order_candidates(
                        cat,
                        mode=mode,
                        tier='fast',
                        key_lookup=lambda nom: 'dummy-key',
                        quota_state={},
                        health_state={},
                        now=0.0,
                    )
                    self.assertEqual(
                        [candidat.model for candidat in candidats],
                        ['a', 'b', 'c'],
                    )


if __name__ == '__main__':
    unittest.main()
