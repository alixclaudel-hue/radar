"""Hook multi-événements : journalise l'activité d'une session de développement.

Alimente la partie « diagnostic du développement » de `radar_ops` — ce qui a été
demandé, ce que Claude a exécuté, et ce qui a été sous-traité à Gemini (celui-là
étant déjà tracé par `scripts/ai_query.py` dans son propre fichier de reçus).

Deux règles non négociables :

1. **Ne jamais bloquer.** Le code de sortie est toujours 0, quoi qu'il arrive.
   Un hook de mesure qui fait échouer l'action qu'il mesure est pire qu'absent.
2. **Ne jamais journaliser de secret.** Pour `Bash`, seule la `description`
   rédigée en clair est retenue — jamais la commande, qui peut contenir un
   jeton, un mot de passe ou le contenu d'un `.env`.

Le texte des demandes est réglé par `RADAR_TELEMETRY_PROMPTS` : `none` ne garde
que la longueur, `head` (défaut) les 200 premiers caractères, `full` le texte
entier. Le défaut est volontairement prudent : ce journal part sur une branche
git, donc hors de la machine.
"""
import json
import os
import sys
import time

MAX_BYTES = 5 * 1024 * 1024
MAX_LABEL = 160
PROMPT_HEAD = 200

# Outils dont l'argument identifiant est sûr à journaliser tel quel.
_PATH_TOOLS = frozenset({"Write", "Edit", "Read", "NotebookEdit"})
_PATTERN_TOOLS = frozenset({"Glob", "Grep"})
_DESC_TOOLS = frozenset({"Bash", "Task", "Agent"})


def _relative(path, project_dir):
    try:
        return os.path.relpath(path, project_dir)
    except (ValueError, TypeError):
        return str(path)


def tool_label(tool_name, tool_input, project_dir):
    """Résumé court et sûr d'un appel d'outil.

    `Bash` passe par `description` et jamais par `command` : c'est la seule
    raison pour laquelle ce journal peut sortir de la machine."""
    if not isinstance(tool_input, dict):
        return ""
    if tool_name in _DESC_TOOLS:
        label = tool_input.get("description") or ""
    elif tool_name in _PATH_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        label = _relative(raw, project_dir) if raw else ""
    elif tool_name in _PATTERN_TOOLS:
        label = tool_input.get("pattern") or ""
    else:
        label = ""
    return str(label)[:MAX_LABEL]


def tool_ok(tool_response):
    """Vrai sauf si la réponse porte une erreur explicite.

    Test volontairement étroit : chercher « error » dans n'importe quelle clé
    marquerait en échec une réponse contenant `errors: []`, c'est-à-dire
    exactement le cas où tout s'est bien passé."""
    if not isinstance(tool_response, dict):
        return True
    if tool_response.get("is_error") or tool_response.get("isError"):
        return False
    return not tool_response.get("error")


def build_record(event, project_dir, prompt_policy="head"):
    """Ligne à journaliser, ou None si l'événement ne nous apprend rien.

    Horodatage en secondes epoch, comme les reçus Gemini et `opslog` : le
    tableau de bord fusionne les trois flux sur un même axe de temps, une
    troisième convention de date l'aurait obligé à convertir."""
    name = event.get("hook_event_name")
    base = {"session": event.get("session_id") or "", "ts": time.time()}

    if name == "SessionStart":
        return dict(base, kind="session_start")
    if name == "SessionEnd":
        return dict(base, kind="session_end")
    if name == "Stop":
        return dict(base, kind="turn_end")
    if name == "UserPromptSubmit":
        prompt = str(event.get("prompt") or "")
        rec = dict(base, kind="prompt", chars=len(prompt))
        if prompt_policy == "full":
            rec["head"] = prompt
        elif prompt_policy != "none":
            rec["head"] = prompt[:PROMPT_HEAD]
        return rec
    if name == "PostToolUse":
        tool = str(event.get("tool_name") or "")
        return dict(base, kind="tool", tool=tool,
                    label=tool_label(tool, event.get("tool_input"), project_dir),
                    ok=tool_ok(event.get("tool_response")))
    return None


def write_line(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.getsize(path) >= MAX_BYTES:
            os.replace(path, path + ".1")   # borne le disque, garde une génération
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main():
    try:
        raw = sys.stdin.read().strip()
        if not raw:
            return 0
        event = json.loads(raw)
        if not isinstance(event, dict):
            return 0
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        policy = (os.environ.get("RADAR_TELEMETRY_PROMPTS") or "head").strip().lower()
        record = build_record(event, project_dir, policy)
        if record is not None:
            write_line(os.path.join(project_dir, ".claude", "telemetry.jsonl"), record)
    except Exception:
        pass                                 # mesurer ne doit jamais coûter une action
    return 0


if __name__ == "__main__":
    sys.exit(main())
