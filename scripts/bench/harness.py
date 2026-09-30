"""Harnais de benchmark : appels au courtier IA et suivi budgétaire.

Ce module isole les seules interactions « coûteuses » (sous-processus du
courtier et lecture du journal télémétrique) pour que les scénarios et le
juge puissent rester testables et purs. Le suivi budgétaire y est centralisé
afin qu'aucun module aval ne puisse lancer une passe non bornée.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Final

# Troncature des sorties : un modèle bavard peut produire des mégaoctets de
# texte ; on ne conserve que les premiers caractères, suffisants pour le
# rapport, afin de ne pas gonfler la mémoire des consommateurs.
_MAX_OUTPUT: Final[int] = 20_000

# Marge (en secondes) pour rattacher un reçu télémétrique à un appel : les
# horodatages du courtier et du harnais peuvent diverger légèrement, d'où la
# tolérance de 0.2 seconde.
_RECEIPT_SKEW_S: Final[float] = 0.2

# Le journal de reçus est append-only et peut croître sans borne : on ne
# remonte que sa queue, suffisante pour l'appel courant.
_RECEIPT_TAIL_LINES: Final[int] = 80


@dataclass
class CallResult:
    model: str
    rc: int
    stdout: str
    stderr: str
    latency_s: float
    tokens_in: int
    tokens_out: int
    tok_per_s: float | None
    ttft_s: None
    timed_out: bool
    broker_ms: int | None = None


def repo_root() -> str:
    """Racine du dépôt déduite de l'emplacement de ce fichier."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _broker_script() -> str:
    """Chemin absolu vers le courtier, ancré sur la racine du dépôt."""
    return os.path.join(repo_root(), 'scripts', 'ai_broker.py')


def _split_identifier(identifier: str) -> tuple[str, str]:
    """Découpe « fournisseur:modele » ; tolère l'absence de préfixe.

    Renvoie (fournisseur, modele). En l'absence de « : », le fournisseur est
    vide et le modele correspond à la chaîne entière.
    """
    if ':' in identifier:
        provider, model = identifier.split(':', 1)
        return provider, model
    return '', identifier


def _blocked_models() -> frozenset[str]:
    """Identifiants de modèles bloqués par la politique locale.

    Lecture directe et tolérante du JSON de config : une config absente ou
    malformée ne doit jamais faire échouer l'énumération des modèles.
    """
    path = os.path.join(repo_root(), 'config', 'ai_models.json')
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    policy = data.get('policy')
    if not isinstance(policy, dict):
        return frozenset()
    blocked = policy.get('blocked_ids')
    if not isinstance(blocked, list):
        return frozenset()
    return frozenset(str(item) for item in blocked)


def list_free_models(exclude_blocked: bool = True) -> list[str]:
    """Liste les modèles gratuits candidats, filtrés par la politique locale.

    Renvoie des identifiants « fournisseur:modele » tels que produits par le
    courtier, dans l'ordre d'apparition de sa sortie.
    """
    cmd = [
        sys.executable,
        _broker_script(),
        '--list-candidates',
        '--mode',
        'general',
        '--max-candidates',
        '100',
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_root(),
            capture_output=True,
            text=True,
            timeout=60.0,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    blocked = _blocked_models() if exclude_blocked else frozenset()
    models: list[str] = []
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith('[inclus] ') or '(gratuit)' not in line:
            continue
        tokens = line.split()
        # Format attendu : « [inclus] fournisseur:modele ... » ; l'identifiant
        # est le 2e token.
        if len(tokens) < 2:
            continue
        identifier = tokens[1]
        _, model = _split_identifier(identifier)
        if exclude_blocked and model in blocked:
            continue
        models.append(identifier)
    return models


def _telemetry_dir() -> str:
    """Dossier des reçus télémétriques, selon les sources disponibles.

    Chaîne de replis : API interne du worker de score, puis variable
    d'environnement, puis valeur par défaut sous la racine du dépôt.
    """
    try:
        from scripts.ai import worker_score  # import opportuniste

        getter = getattr(worker_score, '_dossier_recus', None)
        if callable(getter):
            candidate = getter()
            if candidate:
                return str(candidate)
    except Exception:
        # Toute erreur (import cassé, nom absent, dépendance interne en
        # défaut) doit retomber silencieusement sur les replis suivants :
        # la télémétrie est optionnelle, pas un préalable à l'appel.
        pass
    env_dir = os.environ.get('RADAR_TELEMETRY_DIR')
    if env_dir:
        return env_dir
    return os.path.join(repo_root(), '.claude')


def _as_int(value: object) -> int:
    """Convertit un champ numérique de reçu en entier, sinon 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _read_usage_journal(t_start: float, t_end: float, model_id: str) -> tuple[int, int, int] | None:
    # journal d'usage du courtier, une ligne par appel avec durée côté courtier, plus fiable que les reçus, et fenêtre serrée car la fenêtre des reçus (1 s avant) absorbait l'appel précédent du même modèle (3512 jetons comptés pour un prompt de 200)
    path = os.path.join(_telemetry_dir(), 'ai', 'ai_usage.jsonl')
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            lines = list(deque(fh, maxlen=150))
    except OSError:
        return None

    total_in = 0
    total_out = 0
    total_elapsed = 0
    found = False

    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue

        provider = entry.get('provider', '')
        model = entry.get('model', '')
        full_id = f"{provider}:{model}"
        if full_id != model_id:
            continue

        ts = entry.get('ts')
        if not isinstance(ts, (int, float)):
            continue
        if not (t_start - 0.05 <= ts <= t_end + 0.5):
            continue

        found = True
        tokens = entry.get('tokens')
        if isinstance(tokens, dict):
            total_in += _as_int(tokens.get('prompt_tokens'))
            total_out += _as_int(tokens.get('output_tokens'))
        total_elapsed += _as_int(entry.get('elapsed_ms'))

    if not found:
        return None
    return total_in, total_out, total_elapsed


def _read_receipts(t_wall: float, model: str) -> tuple[int, int] | None:
    """Somme des jetons des reçus attribuables à un appel.

    On ne lit que la queue du journal (append-only, potentiellement énorme) et
    on filtre à la fois sur le modèle et sur un horodatage postérieur au début
    de l'appel : sans ce filtre, les jetons d'un appel concurrent seraient
    indûment imputés au modèle courant. Renvoie None si aucun reçu ne
    correspond (par opposition à un vrai total de 0).
    """
    path = os.path.join(_telemetry_dir(), 'gemini-receipts.jsonl')
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            # deque(maxlen=...) garde les 80 dernières lignes sans charger tout
            # le fichier en mémoire.
            lines = list(deque(fh, maxlen=_RECEIPT_TAIL_LINES))
    except OSError:
        return None

    total_in = 0
    total_out = 0
    found = False
    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue

        model_field = entry.get('model')
        if not isinstance(model_field, str):
            continue
        # Le reçu peut stocker « modele » seul ou « fournisseur:modele » : on
        # normalise pour comparer la partie modèle.
        _, entry_model = _split_identifier(model_field)
        # Les modèles OpenRouter gratuits finissent par « :free » : on compare aussi
        # le champ brut, sinon le découpage au premier « : » ne retrouve jamais le
        # reçu (jetons comptés à 0).
        if entry_model != model and model_field != model:
            continue

        ts = entry.get('ts')
        if not isinstance(ts, (int, float)):
            continue
        if ts < t_wall - _RECEIPT_SKEW_S:
            continue

        found = True
        total_in += _as_int(entry.get('prompt_tokens'))
        total_out += _as_int(entry.get('output_tokens'))

    if not found:
        return None
    return total_in, total_out


def _decode_partial(value: object) -> str:
    """Reconvertit une sortie partielle éventuellement en octets.

    subprocess.TimeoutExpired expose stdout/stderr en octets même lorsque
    text=True, car le décodage n'a pas eu lieu.
    """
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value)


def call_broker(
    model: str,
    prompt: str,
    *,
    mode: str = 'general',
    extra_args: tuple[str, ...] = (),
    timeout: float = 90.0,
) -> CallResult:
    """Appelle le courtier pour un modèle et renvoie des mesures normalisées."""
    cmd = [
        sys.executable,
        _broker_script(),
        '--mode',
        mode,
        '--no-cache',
        '-m',
        model,
        *extra_args,
        prompt,
    ]
    # t_wall marque le début « logique » de l'appel pour la corrélation des
    # reçus télémétriques ; time.monotonic sert à la latence (monotone, non
    # sensible aux ajustements d'horloge système).
    t_wall = time.time()
    start = time.monotonic()

    timed_out = False
    rc = 0
    stdout = ''
    stderr = ''
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_root(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        rc = proc.returncode
        stdout = proc.stdout or ''
        stderr = proc.stderr or ''
    except subprocess.TimeoutExpired as exc:
        # Convention Unix : 124 = dépassement du délai. On conserve la sortie
        # partielle déjà produite pour permettre le diagnostic.
        timed_out = True
        rc = 124
        stdout = _decode_partial(getattr(exc, 'stdout', None))
        stderr = _decode_partial(getattr(exc, 'stderr', None))
    except OSError as exc:
        # Courtier absent ou non exécutable : on dégrade sans lever, car les
        # appelants veulent comparer des modèles, pas gérer des crashs.
        rc = 127
        stderr = f'{type(exc).__name__}: {exc}'

    latency_s = time.monotonic() - start
    t_end = time.time()

    broker_ms: int | None = None
    usage = _read_usage_journal(t_wall, t_end, model)
    if usage is not None:
        tokens_in, tokens_out, broker_ms = usage
    else:
        _, model_part = _split_identifier(model)
        receipts = _read_receipts(t_wall, model_part)
        if receipts is None:
            tokens_in = 0
            tokens_out = 0
        else:
            tokens_in, tokens_out = receipts

    tok_per_s: float | None = None
    if latency_s > 0 and tokens_out > 0:
        tok_per_s = tokens_out / latency_s

    return CallResult(
        model=model,
        rc=rc,
        stdout=stdout[:_MAX_OUTPUT],
        stderr=stderr[:_MAX_OUTPUT],
        latency_s=latency_s,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tok_per_s=tok_per_s,
        # Le courtier ne streame pas : le Time-to-First-Token n'est pas
        # observable via son interface, on le laisse explicitement à None.
        ttft_s=None,
        timed_out=timed_out,
        broker_ms=broker_ms,
    )


class TokenBudget:
    """Compteur de jetons partagé et sûr entre threads.

    Le harnais peut être sollicité en parallèle (juge et scénarios) ; le verrou
    garantit qu'aucunes lecture concurrente ne voit un état incohérent et que
    remaining ne descend jamais sous zéro.
    """

    def __init__(self, limit: int = 200_000) -> None:
        self.limit = int(limit)
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def add(self, n: int) -> None:
        """Incrémente le compteur consommé d'au moins n jetons."""
        if n <= 0:
            return
        with self._lock:
            self._used += int(n)

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self._used)

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._used >= self.limit
