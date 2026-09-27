#!/usr/bin/env python3
"""Hook `UserPromptSubmit` : rappelle le skill `workflow` sur toute requête non triviale.

Avant ce hook, rien ne forçait le passage par `workflow` (cadrage/WBS/
délégation) — seule la mémoire de Claude d'un tour à l'autre. Constaté le
27/09 : aucun événement `UserPromptSubmit` ne portait ce rappel, d'où des
requêtes multi-étapes traitées à coups de commandes isolées dans le fil
principal plutôt que déléguées. Une question simple type chatbot ("c'est
quoi X ?") n'a pas besoin d'un WBS ; ce hook ne rappelle donc que sur un
verbe d'action ou une requête longue.
"""

import json
import re
import sys

ACTION_PATTERN = re.compile(
    r"\b("
    r"corrige[rzs]?|fix(?:e[rs]?)?|implémente[rzs]?|ajoute[rzs]?|analyse[rzs]?"
    r"|propose[rzs]?|crée[rzs]?|modifie[rzs]?|refactor(?:e[rzs]?)?|supprime[rzs]?"
    r"|déploie[sz]?|déployer|teste[rzs]?|diagnostique[rzs]?|écri[st]|écrire"
    r"|génère[rzs]?|migre[rzs]?"
    r")\b",
    re.IGNORECASE,
)

QUESTION_PATTERN = re.compile(
    r"^(qu['’]est-ce|c['’]est quoi|pourquoi|explique|résume|définis)\b",
    re.IGNORECASE,
)

WORKFLOW_REMINDER = (
    "[Rappel workflow] Requête à plusieurs étapes probable. Charger le skill "
    "'workflow' (cadrage, WBS découpé par LIVRABLE et non par commande shell, "
    "ordonnancement, délégation au courtier ou sous-agent) avant d'agir."
)


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            return 0
        prompt = prompt.strip()
        if not prompt:
            return 0

        has_action = bool(ACTION_PATTERN.search(prompt))
        is_question = bool(QUESTION_PATTERN.match(prompt))
        is_simple = (not has_action) and (len(prompt) < 60 or is_question)

        if not is_simple:
            print(WORKFLOW_REMINDER)
        return 0
    except Exception:
        # Un hook de rappel ne doit jamais coûter la session.
        return 0


if __name__ == "__main__":
    sys.exit(main())
