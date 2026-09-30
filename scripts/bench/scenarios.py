from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

# Les validations déterministes vivent ici : elles ne consomment aucun jeton LLM.


@dataclass(frozen=True)
class Check:
    ok: bool
    score: float
    detail: str


@dataclass(frozen=True)
class Scenario:
    id: str
    titre: str
    mode: str
    extra_args: tuple[str, ...]
    build_prompt: Callable[[], str]
    check: Callable[[str], Check]
    judge_criteria: str | None


_CODE_BLOCK_RE = re.compile(r'```(?:python)?\s*\n?(.*?)```', re.DOTALL)


def _extract_code(text: str) -> str:
    m = _CODE_BLOCK_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _extract_json(text: str):
    return json.loads(text.strip())


def _run_py(code: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [sys.executable, '-c', code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, 'timeout'
    return proc.returncode, proc.stderr


_PROMPT_CODE_NOMINAL = (
    'Écris un worker Python asynchrone (asyncio) : async def run_worker(urls, fetch, concurrency=3) '
    'qui appelle la coroutine fetch(url) pour chaque url avec au plus `concurrency` appels simultanés '
    '(asyncio.Semaphore), renvoie la liste des résultats dans l\'ordre des urls, et capture les exceptions '
    'en renvoyant l\'exception comme résultat. Réponds uniquement avec le code. 60 lignes maximum.'
)


def _check_code_nominal(text: str) -> Check:
    code = _extract_code(text)
    passed = 0
    total = 5
    details: list[str] = []

    try:
        compile(code, 'test', 'exec')
        passed += 1
    except SyntaxError as exc:
        details.append(f'compilation: {exc}')

    if 'async def run_worker' in code:
        passed += 1
    else:
        details.append('async def run_worker absent')

    if 'Semaphore' in code:
        passed += 1
    else:
        details.append('Semaphore absent')

    def _test_suffix(assertion: str) -> str:
        return (
            code
            + '\n\nimport asyncio\n\n'
            + 'async def _fetch(url):\n'
            + '    await asyncio.sleep(0)\n'
            + "    if url == 'bad':\n"
            + "        raise ValueError('bad')\n"
            + '    return url.upper()\n\n'
            + 'async def _main():\n'
            + "    res = await run_worker(['a', 'bad', 'c'], _fetch, concurrency=3)\n"
            + assertion
            + '\n\nasyncio.run(_main())\n'
        )

    rc, err = _run_py(_test_suffix("    assert res[0] == 'A', res\n    assert res[2] == 'C', res\n"))
    if rc == 0:
        passed += 1
    else:
        details.append('ordre des résultats incorrect')

    rc, err = _run_py(_test_suffix("    assert isinstance(res[1], Exception), res\n"))
    if rc == 0:
        passed += 1
    else:
        details.append('exception non renvoyée comme résultat')

    score = passed / total
    return Check(ok=passed == total, score=score, detail='; '.join(details) or 'ok')


_BROKEN_MOYENNE = 'def moyenne(xs)\n    total = sum(xs)\n    return total / len(xs'


def _build_prompt_code_repair() -> str:
    try:
        compile(_BROKEN_MOYENNE, '<broken>', 'exec')
    except SyntaxError as exc:
        message = str(exc)
        lineno = exc.lineno
    else:
        message = 'SyntaxError inconnue'
        lineno = 0
    return (
        'Ce code échoue avec cette erreur exacte.\n'
        f'```python\n{_BROKEN_MOYENNE}\n```\n'
        f'Erreur : {message} (ligne {lineno})\n'
        'Corrige-le et renvoie uniquement le code corrigé complet.'
    )


def _check_code_repair(text: str) -> Check:
    code = _extract_code(text)
    passed = 0
    total = 3
    details: list[str] = []

    try:
        compile(code, 'test', 'exec')
        passed += 1
    except SyntaxError as exc:
        details.append(f'compilation: {exc}')

    rc, _ = _run_py(code + '\nassert callable(moyenne)\n')
    if rc == 0:
        passed += 1
    else:
        details.append('moyenne non définie')

    rc, _ = _run_py(code + '\nassert moyenne([2, 4, 6]) == 4\n')
    if rc == 0:
        passed += 1
    else:
        details.append('moyenne([2,4,6]) != 4')

    score = passed / total
    return Check(ok=passed == total, score=score, detail='; '.join(details) or 'ok')


_PROMPT_CODE_UI = (
    'Écris un composant de carte en HTML+CSS inline (un seul fichier, une balise <style>) '
    'pour afficher une recommandation de disque : titre, artiste, score. '
    'Charte : couleurs neutres, chaleureuses, modernes (bois clair, blanc cassé), '
    'pas de bleu vif ni de noir pur, coins arrondis. 40 lignes maximum.'
)


def _check_code_ui(text: str) -> Check:
    passed = 0
    total = 4
    details: list[str] = []
    lower = text.lower()

    if '<style' in lower:
        passed += 1
    else:
        details.append('pas de <style>')

    hex_colors = re.findall(r'#[0-9a-fA-F]{3,6}\b', text)
    if len(hex_colors) >= 3:
        passed += 1
    else:
        details.append(f'seulement {len(hex_colors)} couleurs hex')

    if 'border-radius' in lower:
        passed += 1
    else:
        details.append('pas de border-radius')

    if '#000000' not in text and '#000;' not in text and 'blue' not in lower:
        passed += 1
    else:
        details.append('couleur interdite')

    score = passed / total
    return Check(ok=passed == total, score=score, detail='; '.join(details) or 'ok')


_PROMPT_JSON_CONTRAT = (
    'Extrais les paramètres du texte et réponds UNIQUEMENT par un objet JSON avec exactement les clés '
    'ville (str), date_debut (AAAA-MM-JJ), date_fin (AAAA-MM-JJ), adultes (int), enfants (int), '
    'budget_max (nombre), petit_dejeuner (bool). '
    'Texte : Réserve un hôtel à Lyon du 12 au 15 octobre 2026 pour 2 adultes et 1 enfant, '
    'budget maximum 450 euros, petit-déjeuner inclus.'
)

_JSON_KEYS = {'ville', 'date_debut', 'date_fin', 'adultes', 'enfants', 'budget_max', 'petit_dejeuner'}


def _value_matches(key: str, value, expected) -> bool:
    if key == 'budget_max':
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value == expected
    if key == 'petit_dejeuner':
        return isinstance(value, bool) and value is expected
    if key in {'adultes', 'enfants'}:
        return isinstance(value, int) and not isinstance(value, bool) and value == expected
    return isinstance(value, str) and value == expected


def _check_json_contrat(text: str) -> Check:
    try:
        data = _extract_json(text)
    except ValueError as exc:
        return Check(ok=False, score=0.0, detail=f'JSON invalide: {exc}')

    keys_ok = isinstance(data, dict) and set(data.keys()) == _JSON_KEYS
    types_ok = False
    if isinstance(data, dict):
        types_ok = (
            isinstance(data.get('ville'), str)
            and isinstance(data.get('date_debut'), str)
            and isinstance(data.get('date_fin'), str)
            and isinstance(data.get('adultes'), int) and not isinstance(data.get('adultes'), bool)
            and isinstance(data.get('enfants'), int) and not isinstance(data.get('enfants'), bool)
            and isinstance(data.get('budget_max'), (int, float)) and not isinstance(data.get('budget_max'), bool)
            and isinstance(data.get('petit_dejeuner'), bool)
        )

    expected = {
        'ville': 'Lyon',
        'date_debut': '2026-10-12',
        'date_fin': '2026-10-15',
        'adultes': 2,
        'enfants': 1,
        'budget_max': 450,
        'petit_dejeuner': True,
    }
    matches = 0
    if isinstance(data, dict):
        for key, value in expected.items():
            if key in data and _value_matches(key, data[key], value):
                matches += 1

    score = (1 + int(keys_ok) + int(types_ok) + matches / 7) / 4
    ok = keys_ok and types_ok and matches == 7
    return Check(ok=ok, score=score, detail=f'clés={keys_ok} types={types_ok} valeurs={matches}/7')


_DOCS_CONTEXTE = [
    ('docs/alpha.md', [
        'Le cache disque évite les relectures coûteuses.',
        'Le cache disque est purgé régulièrement.',
        "Aucun autre sujet n'est traité ici.",
    ]),
    ('docs/beta.md', [
        'Le quota YouTube limite les appels API.',
        'Le quota YouTube est vérifié avant chaque requête.',
        "Aucun autre sujet n'est traité ici.",
    ]),
    ('docs/gamma.md', [
        'La rotation des journaux archive les logs.',
        "La rotation des journaux s'exécute chaque nuit.",
        "Aucun autre sujet n'est traité ici.",
    ]),
]

_EXPECTED_DOCS = {
    'docs/alpha.md': 'cache',
    'docs/beta.md': 'quota',
    'docs/gamma.md': 'rotation',
}


def _build_prompt_contexte_pavage() -> str:
    # Un en-tête explicite par document : sinon le premier essai réel rendait des chemins
    # approximatifs ; l'ambiguïté venait du prompt, pas du modèle.
    lignes: list[str] = []
    for k, (path, contenu) in enumerate(_DOCS_CONTEXTE, start=1):
        lignes.append(f'=== DOCUMENT {k} | path: {path} ===')
        for i, ligne in enumerate(contenu, start=1):
            lignes.append(f'{i:02d}| {ligne}')
    docs = '\n'.join(lignes)
    return (
        'Voici des documents, chacun avec son path et ses lignes numérotées :\n'
        f'{docs}\n\n'
        'Pour chaque document, rends une analyse de 15 mots maximum. '
        'Dans chaque élément, "path" recopie EXACTEMENT le path de l\'en-tête. '
        'Réponds UNIQUEMENT par un objet JSON {"analyses":[{"path":...,"analyse":...}, ...]} '
        'avec un élément par document.'
    )


def _check_contexte_pavage(text: str) -> Check:
    try:
        data = _extract_json(text)
    except ValueError as exc:
        return Check(ok=False, score=0.0, detail=f'JSON invalide: {exc}')

    analyses = data.get('analyses') if isinstance(data, dict) else None
    paths_ok = False
    own_ok = False
    no_mix_ok = False

    if isinstance(analyses, list):
        paths = []
        for item in analyses:
            if isinstance(item, dict) and isinstance(item.get('path'), str):
                paths.append(item['path'])
        paths_ok = len(analyses) == 3 and set(paths) == set(_EXPECTED_DOCS)

        own_ok = True
        for expected_path, keyword in _EXPECTED_DOCS.items():
            found = False
            for item in analyses:
                if isinstance(item, dict) and item.get('path') == expected_path:
                    analyse = item.get('analyse')
                    if isinstance(analyse, str) and keyword in analyse.lower():
                        found = True
                    break
            if not found:
                own_ok = False
                break

        no_mix_ok = True
        all_keywords = {'cache', 'quota', 'rotation'}
        for item in analyses:
            if not isinstance(item, dict):
                no_mix_ok = False
                break
            path = item.get('path')
            analyse = item.get('analyse')
            if not isinstance(path, str) or not isinstance(analyse, str) or path not in _EXPECTED_DOCS:
                no_mix_ok = False
                break
            own_keyword = _EXPECTED_DOCS[path]
            autres = all_keywords - {own_keyword}
            low = analyse.lower()
            if any(mot in low for mot in autres):
                no_mix_ok = False
                break

    score = (1 + int(paths_ok) + int(own_ok) + int(no_mix_ok)) / 4
    ok = paths_ok and own_ok and no_mix_ok
    return Check(ok=ok, score=score, detail=f'paths={paths_ok} propres={own_ok} sans_mélange={no_mix_ok}')


_SEARCH_HITS = [
    ('scripts/hooks/delegation_gate.py', 'bloque commit, écriture de test, Read > 80 lignes'),
    ('scripts/hooks/telemetry.py', 'envoie la télémétrie'),
    ('scripts/ai/quota.py', 'quotas fournisseurs'),
    ('radar_web/radar/ytcache.py', 'cache YouTube'),
    ('scripts/ai_worker.py', 'ouvrier'),
    ('scripts/hooks/workflow_reminder.py', 'rappel de workflow'),
]

_SEARCH_PATHS = {p for p, _ in _SEARCH_HITS}
_SEARCH_CORRECT = 'scripts/hooks/delegation_gate.py'


def _build_prompt_search_route() -> str:
    lignes = '\n'.join(f'{i + 1}. {path} — {desc}' for i, (path, desc) in enumerate(_SEARCH_HITS))
    return (
        'Requête : Où est le garde-fou qui bloque les lectures trop longues ?\n\n'
        'Chemins candidats :\n'
        f'{lignes}\n\n'
        'Consigne : Choisis UN chemin de la liste, exactement. '
        'Réponds UNIQUEMENT par un objet JSON {"path":..., "justification":...} '
        '(justification 15 mots maximum).'
    )


def _check_search_route(text: str) -> Check:
    try:
        data = _extract_json(text)
    except ValueError as exc:
        return Check(ok=False, score=0.0, detail=f'JSON invalide: {exc}')

    in_list = False
    correct = False
    if isinstance(data, dict):
        path = data.get('path')
        in_list = isinstance(path, str) and path in _SEARCH_PATHS
        correct = path == _SEARCH_CORRECT

    score = (1 + int(in_list) + int(correct)) / 3
    ok = in_list and correct
    return Check(ok=ok, score=score, detail=f'dans_liste={in_list} correct={correct}')


SCENARIOS: list[Scenario] = [
    Scenario(
        id='code_nominal',
        titre='Worker asynchrone borné',
        mode='code',
        extra_args=('--check-syntax', '--max-repairs', '0'),
        build_prompt=lambda: _PROMPT_CODE_NOMINAL,
        check=_check_code_nominal,
        judge_criteria=(
            'Architecture propre du worker asynchrone : concurrence bornée, ordre préservé, '
            'erreurs gérées, lisibilité.'
        ),
    ),
    Scenario(
        id='code_repair',
        titre='Réparation de code',
        mode='code',
        extra_args=('--check-syntax', '--max-repairs', '0'),
        build_prompt=_build_prompt_code_repair,
        check=_check_code_repair,
        judge_criteria=None,
    ),
    Scenario(
        id='code_ui',
        titre='Carte HTML+CSS',
        mode='code',
        extra_args=('--raw-code', '--no-envelope'),
        build_prompt=lambda: _PROMPT_CODE_UI,
        check=_check_code_ui,
        judge_criteria=(
            'Respect de la charte : couleurs neutres chaleureuses (bois clair, blanc cassé), '
            'rendu moderne et soigné.'
        ),
    ),
    Scenario(
        id='json_contrat',
        titre='Contrat JSON strict',
        mode='general',
        extra_args=('--json-output',),
        build_prompt=lambda: _PROMPT_JSON_CONTRAT,
        check=_check_json_contrat,
        judge_criteria=None,
    ),
    Scenario(
        id='contexte_pavage',
        titre='Contexte pavé sans mélange',
        mode='general',
        extra_args=('--json-output',),
        build_prompt=_build_prompt_contexte_pavage,
        check=_check_contexte_pavage,
        judge_criteria=None,
    ),
    Scenario(
        id='search_route',
        titre='Routage de recherche',
        mode='general',
        extra_args=('--json-output',),
        build_prompt=_build_prompt_search_route,
        check=_check_search_route,
        judge_criteria=(
            'La justification est courte, exacte et cohérente avec le chemin choisi '
            '(garde-fou de lecture).'
        ),
    ),
]


def get_scenarios(ids: list[str] | None = None) -> list[Scenario]:
    if ids is None:
        return list(SCENARIOS)
    connus = {s.id for s in SCENARIOS}
    inconnus = [i for i in ids if i not in connus]
    if inconnus:
        raise ValueError(f'Scénarios inconnus : {", ".join(inconnus)}')
    voulus = set(ids)
    return [s for s in SCENARIOS if s.id in voulus]
