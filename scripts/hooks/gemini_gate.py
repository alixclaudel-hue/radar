"""Hook PreToolUse pour forcer la délégation préalable à Gemini.

Ce garde-fou existe car la politique du projet impose de déléguer certaines
tâches lourdes ou structurantes (revue de code, génération de tests) à Gemini
via scripts/ai_query.py. Ce mécanisme protège le workflow et ne doit pas
reposer uniquement sur la vigilance du modèle.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# Constante de validité d'un reçu en secondes (1 heure)
GATE_TTL = 3600
# Nombre minimum de caractères de prompt exigés dans un reçu
GATE_MIN_PROMPT_CHARS = 50


def load_receipts(path: Path) -> list[dict]:
    """Charge l'historique des reçus Gemini depuis un fichier JSONL.

    Ignore silencieusement les lignes corrompues ou un fichier inexistant
    pour ne jamais bloquer le flux en cas de corruption des données.
    """
    receipts: list[dict] = []
    if not path.is_file():
        return receipts

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        receipts.append(data)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    return receipts


def has_recent_receipt(
    receipts: list[dict],
    modes: tuple[str, ...],
    now: float,
    ttl: float,
    min_chars: int = 0,
) -> bool:
    """Détermine si un reçu valide et récent existe pour l'un des modes requis.

    Un statut 'error' est considéré comme satisfaisant pour ne pas bloquer
    Claude si Gemini est en panne ou en rupture de quota.

    Quand min_chars > 0, un reçu ok ne compte que si prompt_chars >= min_chars.
    Les reçus anciens (sans champ prompt_chars) restent valides pour la
    rétrocompatibilité.
    """
    for r in receipts:
        if r.get("mode") in modes:
            ts = r.get("ts")
            if ts is not None:
                try:
                    if (now - float(ts)) <= ttl:
                        if min_chars == 0 or r.get("status") == "error":
                            return True
                        prompt_chars = r.get("prompt_chars")
                        if prompt_chars is None:
                            return True
                        if int(prompt_chars) >= min_chars:
                            return True
                except (TypeError, ValueError):
                    continue
    return False


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_GIT_COMMIT = re.compile(r"\bgit\b[^|;&]*\bcommit\b")


def receipts_path_for(project_dir: str) -> Path:
    """Chemin des reçus — même résolution que `ai_query.py::receipts_dir()`.

    Doit rester identique à l'écriture : sinon le gate lit un fichier que
    `write_receipt` n'alimente plus et bloque en permanence dès que l'un des
    deux change de cible sans l'autre. `RADAR_TELEMETRY_DIR` prioritaire (VPS,
    volume /data partagé entre les deux checkouts), repli sur le `.claude/`
    du projet sinon (dev local, CI, session cloud)."""
    override = (os.environ.get("RADAR_TELEMETRY_DIR") or "").strip()
    if override:
        return Path(override) / "gemini-receipts.jsonl"
    return Path(project_dir) / ".claude" / "gemini-receipts.jsonl"


def _is_real_commit(command: str) -> bool:
    """Vrai seulement pour un VRAI `git commit`, pas pour une mention du terme.

    Le contenu entre quotes est retiré avant la recherche : sans ça, un `echo`,
    un `grep` ou un message de test qui cite « git commit » se faisait bloquer —
    constaté dès le premier essai du garde-fou sur sa propre commande de test.
    """
    return bool(_GIT_COMMIT.search(_QUOTED.sub(" ", command)))


def required_modes(tool_name: str, tool_input: dict) -> tuple[str, ...] | None:
    """Détermine les modes Gemini exigés selon l'outil et son contenu.

    Retourne None si aucune délégation n'est requise.

    Seuls les déclencheurs MÉCANIQUES sont ici — ceux qu'un hook peut trancher
    sans juger l'intention. `Edit` sur un module existant n'est pas filtré : la
    règle du projet porte sur le PREMIER JET, pas sur une correction chirurgicale
    dans du code déjà écrit et déjà en contexte.
    """
    if tool_name == "Bash":
        if _is_real_commit(tool_input.get("command", "")):
            return ("pr",)

    elif tool_name in ("Write", "Edit"):
        path_obj = Path(tool_input.get("file_path", ""))
        if path_obj.suffix != ".py":
            return None
        if "tests" in path_obj.parts and path_obj.name.startswith("test_"):
            return ("test", "code")
        if tool_name == "Write" and not path_obj.exists():
            return ("code", "test")

    return None


def main() -> int:
    """Point d'entrée principal du hook PreToolUse.

    Lit l'événement sur stdin, vérifie les reçus et décide d'autoriser ou de bloquer.
    En cas d'erreur inattendue, laisse passer (code 0) pour ne pas paralyser Claude.
    """
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            return 0
        event = json.loads(raw_input)
    except Exception:
        return 0

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})

    modes = required_modes(tool_name, tool_input)
    if not modes:
        return 0

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    receipts_path = receipts_path_for(project_dir)

    try:
        ttl = float(os.environ.get("RADAR_GEMINI_GATE_TTL", GATE_TTL))
    except ValueError:
        ttl = float(GATE_TTL)

    try:
        min_chars = int(os.environ.get("RADAR_GEMINI_GATE_MIN_CHARS", GATE_MIN_PROMPT_CHARS))
    except ValueError:
        min_chars = int(GATE_MIN_PROMPT_CHARS)

    receipts = load_receipts(receipts_path)
    now = time.time()

    if has_recent_receipt(receipts, modes, now, ttl, min_chars):
        return 0

    if "pr" in modes:
        dleg_desc = "le message de commit / corps de PR (mode 'pr')"
        cmd_example = "git diff origin/main | python3 scripts/ai_query.py --mode pr --stdin"
    elif modes[0] == "test":
        dleg_desc = "le premier jet des tests (mode 'test')"
        cmd_example = ("python3 scripts/ai_query.py --mode test --check-syntax "
                       "-f <module_cible> -o <fichier_test> '<spec>'")
    else:
        dleg_desc = "le premier jet du module (mode 'code')"
        cmd_example = ("python3 scripts/ai_query.py --mode code --check-syntax "
                       "-o <fichier_cible> '<spec>'")

    chars_detail = f" et d'au moins {min_chars} caractères" if min_chars > 0 else ""
    sys.stderr.write(
        f"BLOQUÉ — délégation Gemini obligatoire avant cette action : {dleg_desc}.\n"
        f"Aucun reçu valide de moins de {int(ttl)} s{chars_detail} dans {receipts_path}.\n"
        f"Lance d'abord :\n  {cmd_example}\n"
        "Si Gemini est épuisé (quota/panne), la tentative ratée écrit elle-même "
        "un reçu et débloque l'action : il faut l'AVOIR tentée, pas l'avoir supposée.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
