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
    """Détermine les modes exigés selon l'outil et son contenu.

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

    if "pr" in modes:
        dleg_desc = "le message de commit / corps de PR (mode 'pr')"
        cmd_example = "git diff origin/main | python3 scripts/ai_broker.py --mode pr --stdin"
    elif modes[0] == "test":
        dleg_desc = "le premier jet des tests (mode 'test')"
        cmd_example = ("python3 scripts/ai_broker.py --mode test --check-syntax "
                       "-f <module_cible> -o <fichier_test> '<spec>'")
    else:
        dleg_desc = "le premier jet du module (mode 'code')"
        cmd_example = ("python3 scripts/ai_broker.py --mode code --check-syntax "
                       "-o <fichier_cible> '<spec>'")

    chars_detail = f" et d'au moins {min_chars} caractères" if min_chars > 0 else ""
    sys.stderr.write(
        f"BLOQUÉ — délégation obligatoire avant cette action : {dleg_desc}.\n"
        f"Aucun reçu valide de moins de {int(ttl)} s{chars_detail} dans {receipts_path}.\n"
        f"Lance d'abord :\n  {cmd_example}\n"
        "Si tous les fournisseurs gratuits sont épuisés (quota/panne), la tentative "
        "ratée écrit elle-même un reçu et débloque l'action : il faut l'AVOIR "
        "tentée, pas l'avoir supposée.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
