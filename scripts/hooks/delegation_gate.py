"""Hook PreToolUse : imposer la délégation préalable à un modèle externe.

Ce garde-fou existe car la politique du projet impose de déléguer certaines
tâches lourdes ou structurantes (revue de code, génération de tests) à un modèle
externe — `scripts/ai_broker.py` (multi-fournisseurs, gratuit d'abord) ou
`scripts/ai_query.py` (Gemini natif). Le mécanisme ne doit pas reposer sur la
vigilance du modèle : le hook bloque mécaniquement l'action tant qu'aucun reçu
récent ne l'atteste.

Le nom du fichier de reçus (`gemini-receipts.jsonl`) est **historique** : les
deux passerelles écrivent dans le même fichier. Il ne doit pas être renommé sans
changer en même temps `ai_query.receipts_dir()` et `receipts_path_for()` — un
écart entre l'écriture et la lecture bloquerait le gate en permanence.

Depuis le passage au multi-fournisseurs, chaque reçu porte `provider`, `model`,
`tier`, `paid` et `cost_usd`. En plus du blocage historique, ce hook **signale**
(dans la télémétrie, et sans jamais bloquer) les reçus payants récents dont le
mode ne justifie pas la dépense : un paiement est légitime en mode `reasoning`
(le plan l'autorise d'emblée) ou quand le reçu porte une justification explicite
(`justified`, `escalated`, `allow_paid`). C'est la mesure du « scalaire payant »
exigée par le plan, pas une consigne de plus.
"""

from __future__ import annotations

import importlib.util
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
# Nombre de lignes au-delà duquel un `Read` direct doit être précédé d'une
# délégation (mode `context`) : en dessous, l'aller-retour coûterait plus que
# la lecture directe. Configurable via `RADAR_READ_GATE_MIN_LINES`.
READ_GATE_MIN_LINES = 80
# Une lecture bornée à `limit` lignes ou moins n'est jamais bloquée, quelle que
# soit la taille du fichier : c'est la relecture ciblée d'un passage signalé
# `uncertain` par un résumé déjà obtenu, ou l'extrait nécessaire juste avant une
# `Edit` précise — pas une lecture exploratoire. Configurable via
# `RADAR_READ_GATE_SURGICAL_LINES`.
READ_GATE_SURGICAL_LINES = 30
# Modes qui justifient d'emblée une dépense payante (escalade décidée, tracée).
PAID_JUSTIFICATION_MODES = ("reasoning",)
# Valeurs de `tier` qui signifient « appel payant » quand `paid` est absent
# (rétrocompatibilité avec d'éventuels reçus antérieurs au champ booléen).
PAID_TIERS = frozenset({"paid", "payant"})
# Type de ligne écrit dans `telemetry.jsonl` par le signalement non bloquant.
SIGNAL_KIND = "delegation_gate"
# Fichier d'état qui évite de re-signaler le même reçu à chaque appel d'outil.
SEEN_FILE = ".delegation-gate-paid-seen.json"


def load_receipts(path: Path) -> list[dict]:
    """Charge l'historique des reçus depuis un fichier JSONL.

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
    Claude si le fournisseur est en panne ou en rupture de quota.

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
_GIT_DIFF = re.compile(r"\bgit\b[^|;&]*\bdiff\b")
_COURTIER = re.compile(r"\bai_(?:broker|query)\.py\b", re.IGNORECASE)


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


def _is_real_diff(command: str) -> bool:
    """Vrai seulement pour un VRAI `git diff`, même principe que `_is_real_commit`."""
    return bool(_GIT_DIFF.search(_QUOTED.sub(" ", command)))


def _relaie_au_courtier(command: str) -> bool:
    """Vrai si la commande mentionne `ai_broker.py`/`ai_query.py` hors guillemets.

    Un `git diff ... | python3 scripts/ai_broker.py --mode pr --stdin` relaie
    déjà sa sortie vers la passerelle : c'est la forme conforme elle-même, pas
    besoin d'un reçu préalable pour l'autoriser. Sans cette exception, gater
    `git diff` bloquerait exactement la commande que la RÈGLE N°1 demande
    d'utiliser (cf. table du haut de CLAUDE.md).
    """
    return bool(_COURTIER.search(_QUOTED.sub(" ", command)))


def compte_lignes_seuil(chemin: str, seuil: int) -> int | None:
    """Nombre de lignes du fichier, arrêté dès `seuil` dépassé (`seuil + 1`).

    None si le fichier est introuvable ou illisible en texte — jamais
    d'exception qui remonte, même règle que le reste du hook. Le calcul
    n'ouvre jamais tout un gros fichier : ça éviterait de payer en lecture ce
    que le hook cherche justement à éviter.
    """
    try:
        with open(chemin, "r", encoding="utf-8", errors="replace") as f:
            n = 0
            for _ in f:
                n += 1
                if n > seuil:
                    return seuil + 1
            return n
    except OSError:
        return None


def required_modes(tool_name: str, tool_input: dict) -> tuple[str, ...] | None:
    """Détermine les modes exigés selon l'outil et son contenu.

    Retourne None si aucune délégation n'est requise.

    Seuls les déclencheurs MÉCANIQUES sont ici — ceux qu'un hook peut trancher
    sans juger l'intention. `Edit` sur un module existant n'est pas filtré : la
    règle du projet porte sur le PREMIER JET, pas sur une correction chirurgicale
    dans du code déjà écrit et déjà en contexte.
    """
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if _is_real_commit(command):
            return ("pr",)
        # Un `git diff` brut, jamais relayé vers la passerelle, finit lu
        # directement par Claude — exactement le contournement observé (diff
        # de plusieurs fichiers passé à `tail`/au terminal plutôt qu'à
        # `ai_broker.py --mode pr/diag --stdin`). `diag`/`context` couvrent
        # aussi l'exploration hors-commit (comprendre un diff sans rédiger de
        # PR) ; seul un vrai relais vers le courtier échappe au gate.
        if _is_real_diff(command) and not _relaie_au_courtier(command):
            return ("pr", "diag", "context")

    elif tool_name in ("Write", "Edit"):
        path_obj = Path(tool_input.get("file_path", ""))
        if path_obj.suffix != ".py":
            return None
        if "tests" in path_obj.parts and path_obj.name.startswith("test_"):
            return ("test", "code")
        if tool_name == "Write" and not path_obj.exists():
            return ("code", "test")

    elif tool_name == "Read":
        limit = tool_input.get("limit")
        try:
            surgical = int(os.environ.get("RADAR_READ_GATE_SURGICAL_LINES", READ_GATE_SURGICAL_LINES))
        except ValueError:
            surgical = READ_GATE_SURGICAL_LINES
        # `0 < limit` exclut 0 et le négatif : sans ça, `limit<=surgical` les
        # laisserait passer alors qu'ils ne bornent rien (relecture large).
        if isinstance(limit, int) and 0 < limit <= surgical:
            return None
        try:
            seuil = int(os.environ.get("RADAR_READ_GATE_MIN_LINES", READ_GATE_MIN_LINES))
        except ValueError:
            seuil = READ_GATE_MIN_LINES
        lignes = compte_lignes_seuil(_chemin_projet(tool_input.get("file_path", "")), seuil)
        if lignes is None or lignes <= seuil:
            return None
        return ("context",)

    elif tool_name == "Grep":
        # Pas de seuil de taille ici : contrairement à `Read`, l'outil peut
        # reconstruire un gros fichier en plusieurs petits appels — un seuil
        # par appel ne verrait jamais l'accumulation. Le mode `search` du
        # courtier ne remplace pas un vrai grep (pas de regex, pas de
        # multi-match) : ce n'est pas grave ici, un reçu récent en mode
        # `context` (déjà obtenu pour lire un fichier) satisfait la même
        # condition — le gate n'exige qu'une tentative de délégation récente,
        # pas que `search` ait réellement remplacé ce Grep précis.
        return ("context", "search")

    return None


def _chemin_projet(chemin: str) -> str:
    """Résout un chemin relatif contre `CLAUDE_PROJECT_DIR`.

    Sans ça, un chemin relatif compte les lignes du mauvais fichier si le
    `cwd` du processus diffère du projet — piège déjà rencontré sur ce dépôt
    entre `~/radar` et `~/radar-work` (même arborescence, deux checkouts)."""
    if os.path.isabs(chemin):
        return chemin
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    return os.path.join(project_dir, chemin) if project_dir else chemin


# --- Signalement non bloquant du paiement non justifié -----------------------


def _doc_id(recu: dict) -> str:
    """Clé stable d'un reçu, pour ne le signaler qu'une fois."""
    return "|".join(str(recu.get(champ, "")) for champ in
                    ("ts", "mode", "provider", "model", "key_id"))


def _est_payant(recu: dict) -> bool:
    """Vrai si le reçu décrit un appel facturé.

    `paid` est le champ canonique du courtier ; `tier` sert de repli pour des
    reçus antérieurs. L'absence des deux signifie « gratuit » : le défaut sûr
    pour un signalement est de ne rien affirmer.
    """
    if recu.get("paid") is True:
        return True
    return str(recu.get("tier") or "").strip().lower() in PAID_TIERS


def _justifie_le_paiement(recu: dict) -> bool:
    """Vrai si le reçu porte une raison explicite de payer."""
    mode = str(recu.get("mode") or "").strip().lower()
    if mode in PAID_JUSTIFICATION_MODES:
        return True
    return any(bool(recu.get(champ)) for champ in
               ("justified", "escalated", "allow_paid"))


def paid_without_reason(
    receipts: list[dict], now: float, ttl: float
) -> list[dict]:
    """Reçus payants récents dont le mode ne justifie pas la dépense.

    Fonction pure : ne lit ni n'écrit aucun fichier, ce qui la rend testable
    sans toucher au disque ni à la télémétrie.
    """
    trouves: list[dict] = []
    for recu in receipts:
        if not _est_payant(recu):
            continue
        if _justifie_le_paiement(recu):
            continue
        ts = recu.get("ts")
        if ts is None:
            continue
        try:
            if (now - float(ts)) > ttl:
                continue
        except (TypeError, ValueError):
            continue
        trouves.append(recu)
    return trouves


def _charger_telemetrie():
    """Le module voisin `telemetry.py`, importé en script ou chargé par chemin.

    Lancé comme hook, `scripts/hooks` est sur `sys.path` : `import telemetry`
    suffit. Chargé dynamiquement par un test (ou par l'alias `gemini_gate`), le
    dossier n'y est pas — d'où le repli par chemin. Rend `None` sans jamais
    lever : la mesure est un effet de bord, pas une dépendance dure.
    """
    try:
        import telemetry  # noqa: PLC0415 — voisin, présent quand lancé en script
        return telemetry
    except ImportError:
        pass

    try:
        chemin = Path(__file__).resolve().parent / "telemetry.py"
        spec = importlib.util.spec_from_file_location("radar_hooks_telemetry", chemin)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _ids_deja_signales(path: str) -> set[str]:
    """Identifiants déjà signalés ; tout fichier illisible rend l'ensemble vide."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(item) for item in data}
    except (OSError, ValueError):
        pass
    return set()


def _memoriser_ids(path: str, ids: set[str]) -> None:
    """Écriture atomique : jamais d'état à moitié écrit (même règle que le socle)."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(ids), f)
        os.replace(tmp, path)
    except OSError:
        pass


def signal_unjustified_paid(
    receipts: list[dict],
    now: float,
    ttl: float,
    *,
    telemetry_path: str,
    seen_path: str | None = None,
) -> list[dict]:
    """Signale en télémétrie chaque reçu payant non justifié, une seule fois.

    Rend la liste des reçus réellement signalés (vide si tout était déjà connu,
    si la télémétrie est indisponible ou si rien n'est suspect). N'échoue jamais
    bruyamment : un signalement qui casse l'action qu'il mesure serait pire
    qu'absent.
    """
    suspects = paid_without_reason(receipts, now, ttl)
    if not suspects:
        return []

    if seen_path is None:
        seen_path = os.path.join(os.path.dirname(telemetry_path) or ".", SEEN_FILE)

    deja = _ids_deja_signales(seen_path)
    nouveaux = [r for r in suspects if _doc_id(r) not in deja]
    if not nouveaux:
        return []

    telemetrie = _charger_telemetrie()
    if telemetrie is None:
        return []

    signales: list[dict] = []
    for recu in nouveaux:
        try:
            telemetrie.write_line(
                telemetry_path,
                {
                    "ts": now,
                    "kind": SIGNAL_KIND,
                    "subkind": "paid_unjustified",
                    "provider": recu.get("provider", ""),
                    "model": recu.get("model", ""),
                    "mode": recu.get("mode", ""),
                    "cost_usd": recu.get("cost_usd"),
                    "receipt_ts": recu.get("ts"),
                    "doc_id": _doc_id(recu),
                },
            )
            signales.append(recu)
        except Exception:
            continue

    if signales:
        _memoriser_ids(seen_path, deja | {_doc_id(r) for r in signales})
    return signales


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

    # Signalement non bloquant : un paiement non justifié se mesure, il
    # n'interrompt pas. C'est ce compteur qui alimente le tableau de bord.
    try:
        telemetrie = _charger_telemetrie()
        if telemetrie is not None:
            signal_unjustified_paid(
                receipts,
                now,
                ttl,
                telemetry_path=telemetrie.telemetry_path(project_dir),
            )
    except Exception:
        pass

    if has_recent_receipt(receipts, modes, now, ttl, min_chars):
        return 0

    if modes == ("pr", "diag", "context"):
        dleg_desc = "cette lecture d'un `git diff` brut, jamais relayée vers la passerelle"
        cmd_example = (
            "git diff ... | python3 scripts/ai_broker.py --mode diag --stdin   "
            "(ou --mode pr avant un commit)"
        )
    elif "pr" in modes:
        dleg_desc = "le message de commit / corps de PR (mode 'pr')"
        cmd_example = "git diff origin/main | python3 scripts/ai_broker.py --mode pr --stdin"
    elif modes[0] == "test":
        dleg_desc = "le premier jet des tests (mode 'test')"
        cmd_example = ("python3 scripts/ai_broker.py --mode test --check-syntax "
                       "-f <module_cible> -o <fichier_test> '<spec>'")
    elif tool_name == "Grep":
        dleg_desc = "une tentative de délégation récente avant cette recherche native (mode 'search' ou 'context')"
        cmd_example = ('python3 scripts/ai_broker.py --mode search --root <dossier> '
                        f'"{tool_input.get("pattern", "<motif>")}"')
    elif modes[0] == "context":
        cible = tool_input.get("file_path", "<fichier>")
        dleg_desc = "la pré-digestion de ce fichier avant lecture directe (mode 'context')"
        cmd_example = f'python3 scripts/ai_broker.py --mode context -f {cible} "<ce que tu cherches>"'
    else:
        dleg_desc = "le premier jet du module (mode 'code')"
        cmd_example = ("python3 scripts/ai_broker.py --mode code --check-syntax "
                       "-o <fichier_cible> '<spec>'")

    surgical_hint = (
        "\nSi le résumé signale un passage incertain (`uncertain`), relis "
        "seulement cette zone (`limit` <= "
        f"{os.environ.get('RADAR_READ_GATE_SURGICAL_LINES', READ_GATE_SURGICAL_LINES)} "
        "lignes) — jamais bloqué.\n"
        if tool_name == "Read" else ""
    )
    chars_detail = f" et d'au moins {min_chars} caractères" if min_chars > 0 else ""
    sys.stderr.write(
        f"BLOQUÉ — délégation obligatoire avant cette action : {dleg_desc}.\n"
        f"Aucun reçu valide de moins de {int(ttl)} s{chars_detail} dans {receipts_path}.\n"
        f"Lance d'abord :\n  {cmd_example}\n"
        f"{surgical_hint}"
        "Si tous les fournisseurs gratuits sont épuisés (quota/panne), la tentative "
        "ratée écrit elle-même un reçu et débloque l'action : il faut l'AVOIR "
        "tentée, pas l'avoir supposée.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
