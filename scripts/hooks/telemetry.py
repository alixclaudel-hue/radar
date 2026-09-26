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


def telemetry_path(project_dir):
    """Fichier de journal, `RADAR_TELEMETRY_DIR` prioritaire, puis `<projet>/data/ops`.

    Le volume de données monté sur le VPS (`<projet>/data/ops`) est le chemin
    naturel : `radar_ops` lit le fichier sur place, sans transport. Le repli
    sur `<projet>/data/ops` dépend de `project_dir` (donc du `cwd`/
    `CLAUDE_PROJECT_DIR` de la session) : sur ce VPS, deux checkouts du même
    dépôt coexistent (`~/radar`, le vrai volume Docker ; `~/radar-work`, un
    clone sans `/data` monté) — d'où l'override `RADAR_TELEMETRY_DIR` posé
    dans `.claude/settings.json`, qui rend ce chemin indépendant du
    répertoire courant (cf. CLAUDE.md, piège confusion `~/radar`/`~/radar-work`)."""
    override = (os.environ.get("RADAR_TELEMETRY_DIR") or "").strip()
    if override:
        return os.path.join(override, "telemetry.jsonl")
    data_ops = os.path.join(project_dir, "data", "ops")
    if os.path.isdir(data_ops):
        return os.path.join(data_ops, "telemetry.jsonl")
    return os.path.join(project_dir, ".claude", "telemetry.jsonl")


def write_line(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.getsize(path) >= MAX_BYTES:
            os.replace(path, path + ".1")   # borne le disque, garde une génération
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def usage_state_path(telemetry_dir, session_id):
    """Chemin du curseur de lecture du transcript pour une session donnée."""
    return os.path.join(telemetry_dir, ".claude_usage_state", session_id + ".json")


def load_usage_offset(state_path):
    """Octet déjà traité du transcript, 0 si le curseur est absent ou invalide."""
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            offset = data.get("offset")
            if isinstance(offset, int):
                return offset
    except Exception:
        pass
    return 0


def save_usage_offset(state_path, offset):
    """Persiste le curseur, en écriture atomique (tmp + `os.replace`)."""
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        tmp_path = state_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"offset": offset}, f)
        os.replace(tmp_path, state_path)
    except Exception:
        pass


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def claude_usage_deltas(transcript_path, offset):
    """Jetons réels consommés depuis `offset`, un par appel API distinct.

    Le transcript répète le même `usage` sur plusieurs lignes JSONL d'un même
    appel (bloc de texte, appel d'outil...) — dédoublonnage par `requestId`,
    en gardant la dernière valeur rencontrée mais l'ordre de première
    apparition. Une ligne finale sans `\\n` est encore en cours d'écriture :
    elle n'est pas consommée, le nouvel offset pointe juste avant elle."""
    try:
        if os.path.getsize(transcript_path) < offset:
            offset = 0                       # transcript tronqué ou recréé
        with open(transcript_path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except Exception:
        return [], offset

    if b"\n" not in data:
        return [], offset

    lines = data.split(b"\n")
    last_incomplete = lines[-1]
    complete_lines = lines[:-1]
    new_offset = offset + len(data) - len(last_incomplete)

    deltas = []
    index_by_request = {}
    for line_bytes in complete_lines:
        if not line_bytes:
            continue
        try:
            obj = json.loads(line_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue

        message = obj.get("message") or {}
        usage = message.get("usage") or {}
        entry = {
            "model": message.get("model") or "inconnu",
            "input_tokens": _to_int(usage.get("input_tokens")),
            "output_tokens": _to_int(usage.get("output_tokens")),
            "cache_creation_input_tokens": _to_int(usage.get("cache_creation_input_tokens")),
            "cache_read_input_tokens": _to_int(usage.get("cache_read_input_tokens")),
        }

        request_id = obj.get("requestId")
        if request_id is None:
            deltas.append(entry)
        elif request_id in index_by_request:
            deltas[index_by_request[request_id]] = entry
        else:
            index_by_request[request_id] = len(deltas)
            deltas.append(entry)

    return deltas, new_offset


def log_claude_usage(event, project_dir):
    """Émet un événement `claude_usage` par appel API vu depuis le dernier tour.

    Appelé à `Stop`/`SessionEnd` seulement : c'est là que `transcript_path` est
    déjà flush pour le tour qui vient de finir. Le curseur par session rend
    l'appel idempotent — un `SessionEnd` qui revoit un tour déjà traité au
    `Stop` précédent n'écrit rien de plus."""
    if event.get("hook_event_name") not in ("Stop", "SessionEnd"):
        return
    transcript_path = str(event.get("transcript_path") or "").strip()
    session_id = str(event.get("session_id") or "").strip()
    if not transcript_path or not session_id:
        return

    telemetry_dir = os.path.dirname(telemetry_path(project_dir))
    state_path = usage_state_path(telemetry_dir, session_id)
    offset = load_usage_offset(state_path)
    deltas, new_offset = claude_usage_deltas(transcript_path, offset)

    if deltas:
        path = telemetry_path(project_dir)
        ts = time.time()
        for entry in deltas:
            write_line(path, dict(entry, session=session_id, ts=ts, kind="claude_usage"))
    if new_offset != offset:
        save_usage_offset(state_path, new_offset)


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
            write_line(telemetry_path(project_dir), record)
        log_claude_usage(event, project_dir)
    except Exception:
        pass                                 # mesurer ne doit jamais coûter une action
    return 0


if __name__ == "__main__":
    sys.exit(main())
