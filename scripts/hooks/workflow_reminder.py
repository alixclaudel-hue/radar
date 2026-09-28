#!/usr/bin/env python3
"""Hook `UserPromptSubmit` : rappelle le skill `workflow` sur toute requête non triviale.

Avant ce hook, rien ne forçait le passage par `workflow` (cadrage/WBS/
délégation) — seule la mémoire de Claude d'un tour à l'autre. Constaté le
27/09 : aucun événement `UserPromptSubmit` ne portait ce rappel, d'où des
requêtes multi-étapes traitées à coups de commandes isolées dans le fil
principal plutôt que déléguées. Une question simple type chatbot ("c'est
quoi X ?") n'a pas besoin d'un WBS ; ce hook ne rappelle donc que sur un
verbe d'action ou une requête longue.

Correctif C-2 (diagnostic sous-traitance) : un rappel en langage naturel ne
suffisait pas — rien ne rendait le jugement « requête multi-étapes »
vérifiable par le gate. Ce hook écrit donc aussi un MARQUEUR
(`.claude/current_request.json`) que `delegation_gate.py` relit à chaque
`PreToolUse`. Le chemin vient de `delegation_gate.current_request_path()` :
source unique, pour que l'écriture et la lecture ne divergent jamais (même
piège que le nom du fichier de reçus, cf. `delegation_gate.py`).
"""

import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

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

# Longueur du texte de requête conservé dans le marqueur : même borne que la
# télémétrie (`telemetry.PROMPT_HEAD`) — un en-tête suffit à identifier la
# requête sans recopier tout le prompt dans un fichier d'état.
PROMPT_HEAD = 200


def _charger_gate():
    """Le module voisin `delegation_gate.py`, importé en script ou par chemin.

    Lancé comme hook, `scripts/hooks` est sur `sys.path` : `import
    delegation_gate` suffit. Chargé dynamiquement par un test, le dossier n'y
    est pas — d'où le repli par chemin. Rend `None` sans jamais lever : écrire
    le marqueur est un effet de bord, pas une dépendance dure."""
    try:
        import delegation_gate  # noqa: PLC0415 — voisin, présent quand lancé en script
        return delegation_gate
    except ImportError:
        pass

    try:
        chemin = Path(__file__).resolve().parent / "delegation_gate.py"
        spec = importlib.util.spec_from_file_location("radar_hooks_delegation_gate", chemin)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _projet_dir() -> str:
    """Racine du projet : `CLAUDE_PROJECT_DIR`, sinon `PWD`, sinon rien."""
    return (os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("PWD") or "").strip()


def marquer_requete(payload, prompt, *, non_trivial, project_dir=None) -> dict | None:
    """Écrit le marqueur de la requête courante ; jamais d'exception qui remonte.

    Renvoie le contenu écrit, ou None si le marqueur ne peut pas être écrit
    (module voisin indisponible, racine de projet inconnue, erreur d'E/S). Un
    marqueur manquant ne doit jamais coûter la session : le gate retombe alors
    sur la seule analyse des reçus."""
    try:
        gate = _charger_gate()
        if gate is None:
            return None
        racine = project_dir or _projet_dir()
        if not racine:
            return None
        chemin = gate.current_request_path(racine)
        os.makedirs(str(chemin.parent), exist_ok=True)
        contenu = {
            "ts": time.time(),
            "session_id": payload.get("session_id") or "",
            "prompt_chars": len(prompt),
            "prompt_head": prompt[:PROMPT_HEAD],
            "non_trivial": bool(non_trivial),
        }
        # Écriture atomique : jamais de marqueur à moitié écrit (même règle que
        # le reste du socle de télémétrie).
        tmp = str(chemin) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(contenu, f, ensure_ascii=False)
        os.replace(tmp, str(chemin))
        return contenu
    except Exception:
        return None


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

        # Le marqueur est écrit pour TOUTE requête (triviale comprise) : une
        # nouvelle requête simple doit écraser le marqueur « non trivial » de
        # la précédente, sinon le gate croirait encore la requête en cours
        # multi-étapes (C-2).
        marquer_requete(payload, prompt, non_trivial=not is_simple)

        if not is_simple:
            print(WORKFLOW_REMINDER)
        return 0
    except Exception:
        # Un hook de rappel ne doit jamais coûter la session.
        return 0


if __name__ == "__main__":
    sys.exit(main())
