"""Juge LLM minimaliste : une note 1-10 et dix mots de justification, pas plus.

Le budget de la passe de benchmark est plafonné à 200 000 jetons : chaque
appel au juge doit donc coûter quelques dizaines de jetons seulement, et un
échec ne doit jamais être « rattrapé » par une nouvelle tentative. On préfère
rendre un score indisponible (score=None + error renseignée) que consommer du
budget pour un verdict de second ordre.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from scripts.bench import harness

__all__ = ["JudgeResult", "build_judge_prompt", "parse_judge_output", "judge"]

# Bornes calibrées pour que le verdict coûte beaucoup moins cher que le
# scénario jugé : 1500 caractères ≈ 400 jetons d'entrée, largement de quoi
# laisser un juge trancher sur du code court ou un contrat JSON.
MAX_OUTPUT_CHARS = 1500
MAX_COMMENT_CHARS = 120
MAX_ERROR_CHARS = 120
_ELLIPSE = "…"

# Le chiffre doit ouvrir le texte et ne pas être le préfixe d'un nombre plus
# long (« 10 » oui, « 100 » non) : \b s'en charge, car un chiffre suivant est
# un caractère de mot.
_SCORE_RE = re.compile(r"^(10|[1-9])\b")


@dataclass(frozen=True)
class JudgeResult:
    """Verdict du juge : score éventuel, commentaire, jetons consommés, erreur."""

    score: int | None
    comment: str
    tokens: int
    error: str


def _tronque(texte: str, limite: int, *, ellipse: bool = False) -> str:
    """Ramène `texte` à `limite` caractères au plus.

    L'ellipse éventuelle est comptée dans la limite : la longueur du prompt
    est la seule variable du coût du juge que l'on contrôle vraiment.
    """
    if len(texte) <= limite:
        return texte
    if not ellipse:
        return texte[:limite]
    return texte[: limite - len(_ELLIPSE)] + _ELLIPSE


def build_judge_prompt(criteria: str, output: str) -> str:
    """Prompt de jugement le plus court possible, en français."""
    critere = (criteria or "").strip()
    sortie = _tronque((output or "").strip(), MAX_OUTPUT_CHARS, ellipse=True)
    return (
        "Juge cette sortie selon le critère. "
        f"Critère : {critere} "
        f"Sortie : <<<{sortie}>>> "
        "Réponds UNIQUEMENT par un chiffre de 1 à 10, un espace, "
        "puis 10 mots maximum de justification. Rien d'autre."
    )


def parse_judge_output(text: str) -> tuple[int | None, str]:
    """Extrait (score, commentaire) d'une réponse de juge.

    Un texte non conforme vaut score=None : l'appelant saura que le verdict
    est inexploitable, mais le commentaire brut reste disponible pour le
    diagnostic.
    """
    brut = (text or "").strip()
    correspondance = _SCORE_RE.match(brut)
    if correspondance is None:
        return None, _tronque(brut, MAX_COMMENT_CHARS)
    score = int(correspondance.group(1))
    commentaire = brut[correspondance.end() :].strip()
    return score, _tronque(commentaire, MAX_COMMENT_CHARS)


def judge(
    criteria: str, output: str, *, model: str, timeout: float = 60.0
) -> JudgeResult:
    """Note une sortie via `model`, sans jamais lever d'exception."""
    prompt = build_judge_prompt(criteria, output)
    # 3 essais espacés : en passe complète, 32 appels du juge sur 57 étaient
    # écartés par le courtier (cooldown quota du juge sous rafale parallèle). Un
    # appel refusé ne consomme aucun jeton, réessayer ne grève donc pas le budget.
    jetons = 0
    for essai in range(3):
        appel = harness.call_broker(model, prompt, mode="general", timeout=timeout)
        jetons += int(appel.tokens_in) + int(appel.tokens_out)
        if appel.rc == 0 and not appel.timed_out:
            break
        if essai < 2:
            time.sleep(8.0)

    # timed_out est testé avec rc : selon le courtier, un timeout peut remonter
    # un code de retour nul avec une sortie vide, donc un score absent sans
    # cause explicite.
    if appel.rc != 0 or appel.timed_out:
        erreur = _tronque((appel.stderr or "").strip(), MAX_ERROR_CHARS)
        if not erreur:
            # stderr vide (timeout, kill) : on garde malgré tout une trace.
            erreur = "timeout du juge" if appel.timed_out else f"rc={appel.rc} sans stderr"
        # Aucun nouvel essai : le budget est global à la passe, un juge
        # capricieux ne doit pas manger les jetons des scénarios restants.
        return JudgeResult(score=None, comment="", tokens=jetons, error=erreur)

    score, commentaire = parse_judge_output(appel.stdout)
    return JudgeResult(score=score, comment=commentaire, tokens=jetons, error="")
