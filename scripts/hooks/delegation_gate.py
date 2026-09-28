"""Hook PreToolUse : imposer la délégation préalable à un modèle externe.

Ce garde-fou existe car la politique du projet impose de déléguer certaines
tâches lourdes ou structurantes (revue de code, génération de tests) à un modèle
externe. Le SEUL relais conforme est `scripts/ai_broker.py` (multi-fournisseurs,
gratuit d'abord) ; `scripts/ai_query.py` n'est plus que le transport bas niveau
de la passerelle Gemini et un appel direct à celui-ci ne vaut PAS délégation :
le reçu qu'il écrit porte `via="direct"` et ne satisfait donc plus le gate
(correctif C-4 du diagnostic sous-traitance). Le mécanisme ne doit pas reposer
sur la vigilance du modèle : le hook bloque mécaniquement l'action tant qu'aucun
reçu conforme et récent ne l'atteste.

Le nom du fichier de reçus (`gemini-receipts.jsonl`) est **historique** : tous
les appelants écrivent dans le même fichier. Il ne doit pas être renommé sans
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
# Marqueur de la requête en cours, écrit par `workflow_reminder.py` au
# `UserPromptSubmit` et lu ici (correctif C-2). Nom PARTAGÉ : `workflow_reminder`
# le résout via `current_request_path()` ci-dessous, pour que l'écriture et la
# lecture ne puissent pas diverger (même piège que le nom du fichier de reçus).
CURRENT_REQUEST_NAME = "current_request.json"
# Nombre d'appels d'outils, cumulés depuis le début de la requête courante,
# au-delà duquel une écriture de `.py` doit passer par l'ouvrier `ai_worker.py`
# (correctif C-1). Un enchaînement explorer→modifier→tester est exactement le
# créneau de l'ouvrier (exécution autonome, 0 jeton du fil principal) : au-delà
# de ce seuil, on ne juge plus l'intention, on constate un compteur. Option A du
# diagnostic. Configurable via `RADAR_WORKER_GATE_MIN_TOOLS`.
WORKER_GATE_MIN_TOOLS = 8
# Modes du reçu attestant le passage par l'OUVRIER (reçu écrit par `ai_worker.py`,
# `via="worker"`).
WORKER_MODES = ("worker",)
# Modes attestant une délégation quelconque pour la requête en cours (C-2) :
# l'ouvrier, ou un appel courtier ayant produit du code ou des tests.
DELEGATION_MODES = ("worker", "code", "test")
# Nombre de lignes (`old_string` + `new_string`) au-delà duquel un `Edit` sur un
# module Python EXISTANT doit être précédé d'une délégation (correctif C-3). En
# dessous, c'est une correction chirurgicale dans du code déjà en contexte — le
# passe-droit historique d'`Edit` reste légitime. Au-delà, c'est une réécriture
# de bloc/fonction qui mérite le premier jet délégué. Configurable via
# `RADAR_EDIT_GATE_MIN_LINES` ; `0` (ou négatif) désactive le déclencheur.
EDIT_GATE_MIN_LINES = 40
# Motifs de BALAYAGE large dans l'en-tête du prompt de la requête courante
# (correctif C-6) : « où est », « trouve tous », « liste les »… Quand la requête
# réclame un inventaire/une recherche, un `Grep` (ou une lecture large) natif
# recharge tout le contexte dans le fil principal — c'est exactement le créneau
# de `explore-leger` (modèle haiku) ou du mode `search` du courtier.
SCAN_MARKERS = (
    "où est", "où sont", "où se trouve", "où trouver",
    "trouve tous", "trouve toutes", "trouve tout",
    "liste les", "liste tous", "liste toutes",
    "recense", "inventaire", "cherche dans tout",
    "balaye", "balayage", "scanne", "scan ",
)
# Mode du courtier attestant d'une recherche déléguée (C-6).
SEARCH_MODES = ("search",)


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
    *,
    exiger_via_conforme: bool = False,
) -> bool:
    """Détermine si un reçu valide et récent existe pour l'un des modes requis.

    Un statut 'error' est considéré comme satisfaisant pour ne pas bloquer
    Claude si le fournisseur est en panne ou en rupture de quota.

    Quand min_chars > 0, un reçu ok ne compte que si prompt_chars >= min_chars.
    Les reçus anciens (sans champ prompt_chars) restent valides pour la
    rétrocompatibilité.

    Quand exiger_via_conforme est vrai, un reçu marqué `via="direct"` (appel
    direct à `ai_query.py`, hors courtier) est IGNORÉ : il atteste d'un appel,
    pas d'une délégation conforme (C-4). Les reçus antérieurs au champ `via`
    restent valides — le filtrage ne vise que le marquage explicite.
    """
    for r in receipts:
        if r.get("mode") in modes:
            if exiger_via_conforme and _est_appel_direct(r):
                continue
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
# Relais CONFORME : seul `ai_broker.py` compte (multi-fournisseurs, gratuit
# d'abord, écrit un reçu riche avec `provider`). `ai_query.py` est le transport
# bas niveau de la passerelle Gemini : l'appeler en direct contourne le courtier
# et ne satisfait plus le gate (correctif C-4).
_BROKER = re.compile(r"\bai_broker\.py\b", re.IGNORECASE)
_DIRECT = re.compile(r"\bai_query\.py\b", re.IGNORECASE)
# Modes du courtier dont un reçu conforme récent suffit à autoriser un appel
# direct à `ai_query.py` : le point n'est pas le mode précis, mais d'avoir
# passé par `ai_broker.py` avant.
_MODES_LARGE_BROKER = ("code", "test", "pr", "diag", "context", "search", "reasoning")


def _est_appel_direct(recu: dict) -> bool:
    """Vrai si le reçu provient d'un appel DIRECT à `ai_query.py`.

    Les reçus antérieurs au champ `via` (absents) restent conformes : le
    filtrage ne vise que les reçus explicitement marqués `direct`."""
    return str(recu.get("via") or "").strip().lower() == "direct"


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


def current_request_path(project_dir: str) -> Path:
    """Chemin du marqueur de la requête courante.

    `<projet>/.claude/current_request.json`, volontairement INDÉPENDANT de
    `RADAR_TELEMETRY_DIR` : ce marqueur décrit la requête en cours dans CE
    checkout (WBS à tenir), pas une mesure partagée entre checkouts. Source
    unique du chemin — `workflow_reminder.py` écrit en passant par ici.
    """
    return Path(project_dir) / ".claude" / CURRENT_REQUEST_NAME


def load_current_request(project_dir: str) -> dict:
    """Marqueur de la requête courante, `{}` s'il est absent ou corrompu.

    Écrit par `workflow_reminder.py`. Ne lève jamais : le gate doit pouvoir
    décider sur ses reçus même si le marqueur est illisible (C-2)."""
    try:
        with current_request_path(project_dir).open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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
    """Vrai si la commande relaie vers le courtier CONFORME (`ai_broker.py`).

    Un `git diff ... | python3 scripts/ai_broker.py --mode pr --stdin` relaie
    déjà sa sortie vers la passerelle : c'est la forme conforme elle-même, pas
    besoin d'un reçu préalable pour l'autoriser. Sans cette exception, gater
    `git diff` bloquerait exactement la commande que la RÈGLE N°1 demande
    d'utiliser (cf. table du haut de CLAUDE.md).

    `ai_query.py` n'est PLUS accepté ici : c'est le transport bas niveau, pas
    un relais conforme (C-4). Pipeliner un diff vers lui laisse le diff atterrir
    chez Claude via un appel direct — précisément le contournement visé.
    """
    return bool(_BROKER.search(_QUOTED.sub(" ", command)))


def _invoque_ai_query_direct(command: str) -> bool:
    """Vrai si la commande appelle `ai_query.py` hors guillemets (appel direct).

    Même précaution que `_is_real_commit` : le contenu entre quotes est retiré
    avant la recherche, pour ne pas bloquer un `echo`/`grep` qui cite le nom."""
    return bool(_DIRECT.search(_QUOTED.sub(" ", command)))


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


def compte_lignes_edition(tool_input: dict) -> int:
    """Nombre de lignes touchées par un `Edit` : `old_string` + `new_string`.

    Approximation volontairement simple — le hook `PreToolUse` ne voit que la
    requête d'édition, pas le résultat — mais elle sépare nettement le
    remplacement d'une fonction/d'un bloc d'une correction ponctuelle, ce qui
    suffit à trancher (correctif C-3). Un champ absent ou vide compte 0 lignes
    (ex. une pure insertion n'a pas d'`old_string`), donc ne déclenche rien.
    """
    total = 0
    for champ in ("old_string", "new_string"):
        texte = tool_input.get(champ)
        if isinstance(texte, str) and texte:
            total += len(texte.splitlines())
    return total


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
        # Appel direct à `ai_query.py` : transport bas niveau, plus un relais
        # conforme (C-4). On exige un reçu broker récent ; le message du gate
        # renvoie vers `ai_broker.py`. Un reçu écrit par cet appel direct porte
        # `via="direct"` et ne satisfera donc pas la tentative suivante.
        if _invoque_ai_query_direct(command):
            return _MODES_LARGE_BROKER

    elif tool_name in ("Write", "Edit"):
        path_obj = Path(tool_input.get("file_path", ""))
        if path_obj.suffix != ".py":
            return None
        if "tests" in path_obj.parts and path_obj.name.startswith("test_"):
            return ("test", "code")
        if tool_name == "Write" and not path_obj.exists():
            return ("code", "test")
        # C-3 : un `Edit` sur un module Python EXISTANT n'était jamais filtré —
        # le commentaire de tête le justifiait par le « premier jet » seulement.
        # Un remplacement massif (`old_string` + `new_string` au-delà du seuil)
        # n'est plus une correction chirurgicale : c'est une réécriture de bloc.
        # En dessous du seuil, le passe-droit reste légitime et on laisse passer.
        if tool_name == "Edit" and path_obj.exists():
            try:
                seuil = int(os.environ.get("RADAR_EDIT_GATE_MIN_LINES", EDIT_GATE_MIN_LINES))
            except ValueError:
                seuil = EDIT_GATE_MIN_LINES
            if seuil > 0 and compte_lignes_edition(tool_input) > seuil:
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
    suffit. Chargé dynamiquement par un test, le dossier n'y est pas — d'où le
    repli par chemin. Rend `None` sans jamais
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


def a_recu_direct_recent(
    receipts: list[dict], modes: tuple[str, ...], now: float, ttl: float
) -> bool:
    """Vrai si un reçu récent pour ces modes vient d'un appel DIRECT.

    Ne sert qu'à choisir le bon message : un reçu `via="direct"` ne satisfait
    pas le gate, mais il ne faut pas dire à l'utilisateur qu'il n'a « rien
    tenté » alors qu'il a tenté — au mauvais endroit (il doit passer par
    `ai_broker.py`). Fonction pure : ni lecture ni écriture de fichier.
    """
    for r in receipts:
        if r.get("mode") not in modes or not _est_appel_direct(r):
            continue
        ts = r.get("ts")
        try:
            if ts is not None and (now - float(ts)) <= ttl:
                return True
        except (TypeError, ValueError):
            continue
    return False


# --- Contrainte de pattern : ouvrier (C-1) et WBS vérifiable (C-2) -----------


def _ts_requete(requete: dict) -> float | None:
    """Horodatage du marqueur de requête, None s'il est absent ou invalide."""
    ts = requete.get("ts") if isinstance(requete, dict) else None
    try:
        return float(ts) if ts is not None else None
    except (TypeError, ValueError):
        return None


def _debut_requete(requete: dict, now: float, ttl: float) -> float:
    """Borne basse d'un reçu « de la requête courante ».

    On prend la plus RÉCENTE de deux bornes : le début réel de la requête
    (marqueur C-2) et l'entrée dans la fenêtre TTL. Un reçu doit être à la fois
    frais (dans le TTL) et postérieur au début de la requête (appartenance) —
    sans quoi un unique reçu ancien satisferait la contrainte à vie.
    """
    marqueur = _ts_requete(requete)
    borne = now - ttl
    return marqueur if marqueur is not None and marqueur > borne else borne


def telemetry_path_for(project_dir: str) -> Path | None:
    """Chemin du journal de télémétrie, via le module voisin `telemetry.py`.

    On réutilise SA résolution (`telemetry.telemetry_path`) plutôt que de la
    recopier : un écart entre la lecture et l'écriture ferait compter zéro appel
    d'outil et le déclencheur C-1 ne se lancerait jamais (même piège que le
    fichier de reçus). None si le module est introuvable — le décompte est un
    appui, pas une dépendance dure : sans lui, le gate retombe sur ses reçus.
    """
    module = _charger_telemetrie()
    if module is None:
        return None
    try:
        return Path(module.telemetry_path(project_dir))
    except Exception:
        return None


def compte_appels_outils(
    chemin: Path | None, session_id: str, depuis_ts: float | None = None
) -> int:
    """Nombre d'appels d'outils (`kind="tool"`) journalisés pour une session.

    `depuis_ts` borne le décompte au début de la requête courante (marqueur C-2) :
    sans lui, un long chantier accumulerait les appels des requêtes précédentes et
    déclencherait le gate à tort. Fonction pure : aucune exception ne remonte, un
    journal illisible se lit comme « zéro appel » (on ne bloque pas sur un doute).
    """
    if chemin is None or not session_id:
        return 0
    total = 0
    try:
        with Path(chemin).open("r", encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if not ligne:
                    continue
                try:
                    rec = json.loads(ligne)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or rec.get("kind") != "tool":
                    continue
                if str(rec.get("session") or "") != session_id:
                    continue
                if depuis_ts is not None:
                    ts = rec.get("ts")
                    try:
                        if ts is None or float(ts) < depuis_ts:
                            continue
                    except (TypeError, ValueError):
                        continue
                total += 1
    except OSError:
        return 0
    return total


def a_recu_depuis(receipts: list[dict], modes: tuple[str, ...], depuis_ts: float) -> bool:
    """Vrai si un reçu conforme de ces modes a été écrit depuis `depuis_ts`.

    Plus strict que `has_recent_receipt` (borne = TTL) : ici la borne est le DÉBUT
    DE LA REQUÊTE courante (marqueur C-2). Un reçu d'une requête précédente, même
    frais, ne compte pas. Un reçu direct (`via="direct"`) ne compte pas non plus
    (C-4) ; un reçu `error` compte (tentative ratée = délégation tentée).
    """
    for r in receipts:
        if r.get("mode") not in modes or _est_appel_direct(r):
            continue
        ts = r.get("ts")
        if ts is None:
            continue
        try:
            if float(ts) >= depuis_ts:
                return True
        except (TypeError, ValueError):
            continue
    return False


def est_ecriture_python(tool_name: str, tool_input: dict) -> bool:
    """Vrai pour une écriture (`Write`/`Edit`) portant sur un `.py`."""
    if tool_name not in ("Write", "Edit"):
        return False
    return Path(tool_input.get("file_path", "")).suffix == ".py"


def raison_delegation_ecriture(
    tool_name: str,
    tool_input: dict,
    *,
    appels_outils: int,
    min_appels: int,
    requete: dict,
) -> tuple[tuple[str, ...], str] | None:
    """Délégation exigée par le PATTERN de la requête pour cette écriture `.py`.

    Rend `(modes_requis, raison)` ou None si aucune contrainte de pattern.

    - **C-1 (`multi_outils`)** : la requête courante a déjà enchaîné `min_appels`
      appels d'outils → c'est un chantier explorer→modifier→tester, créneau de
      l'ouvrier `ai_worker.py`. On exige un reçu `worker`.
    - **C-2 (`requete_multi_etapes`)** : le marqueur de requête annonce une
      requête multi-étapes → toute écriture `.py` exige une délégation
      quelconque (ouvrier ou courtier code/tests) enregistrée depuis le début de
      la requête.

    Ne concerne que les `.py` : la politique porte sur le code, pas sur la
    documentation ni les données.
    """
    if not est_ecriture_python(tool_name, tool_input):
        return None
    if appels_outils >= min_appels:
        return WORKER_MODES, "multi_outils"
    if isinstance(requete, dict) and bool(requete.get("non_trivial")):
        return DELEGATION_MODES, "requete_multi_etapes"
    return None


def message_pattern(
    raison: str, *, receipts_path, appels_outils: int, min_appels: int
) -> str:
    """Message de blocage quand l'exigence vient du PATTERN (C-1/C-2)."""
    if raison == "multi_outils":
        return (
            "BLOQUÉ — délégation obligatoire avant cette action : la requête "
            f"courante enchaîne déjà {appels_outils} appels d'outils "
            f"(>= {min_appels}) — un chantier « explorer + modifier + tester ».\n"
            "C'est le créneau de l'OUVRIER autonome, à 0 jeton du fil principal.\n"
            "Aucun reçu `worker` (mode=\"worker\", via=\"worker\") depuis le début "
            f"de la requête dans {receipts_path}.\n"
            "Lance d'abord :\n"
            '  python3 scripts/ai_worker.py "<livrable, en clair>" --json\n'
            "L'ouvrier travaille dans un worktree git isolé et rend un diff : le "
            "fil principal ne fait plus que l'assemblage et l'échantillonnage.\n"
        )
    return (
        "BLOQUÉ — délégation obligatoire avant cette action : la requête courante "
        f"est marquée « à plusieurs étapes » (marqueur {CURRENT_REQUEST_NAME}) et "
        "aucune délégation n'a encore été enregistrée DEPUIS SON DÉBUT.\n"
        "Délègue d'abord le livrable :\n"
        '  python3 scripts/ai_worker.py "<livrable, en clair>" --json       '
        "(exécution autonome, 0 jeton)\n"
        "  python3 scripts/ai_broker.py --mode code|test -o <fichier> '<spec>'   "
        "(premier jet délégué)\n"
        "Un reçu antérieur à cette requête ne compte pas : la contrainte repart à "
        "zéro à chaque demande.\n"
    )


def _motif_balayage(texte: str) -> bool:
    """Vrai si le texte (en-tête de prompt) annonce un balayage large (C-6)."""
    t = (texte or "").lower()
    return any(m in t for m in SCAN_MARKERS)


def raison_delegation_balayage(
    tool_name: str,
    modes_requis: tuple[str, ...] | None,
    requete: dict,
) -> tuple[tuple[str, ...], str] | None:
    """C-6 : délégation de recherche exigée pour un balayage natif.

    Déclenché quand la requête courante (marqueur C-2) annonce un balayage
    (« où est », « trouve tous », « liste les »…) ET que le fil principal tente un
    `Grep` — ou une `Read` DÉJÀ jugée large par `required_modes` (une relecture
    ciblée, `modes_requis is None`, reste libre). Retourne `(SEARCH_MODES, raison)`
    ou None.
    """
    if tool_name == "Grep":
        pass
    elif tool_name == "Read":
        if modes_requis is None:
            return None
    else:
        return None
    if not isinstance(requete, dict):
        return None
    if not _motif_balayage(str(requete.get("prompt_head") or "")):
        return None
    return SEARCH_MODES, "balayage_a_deleguer"


def a_recu_recherche(receipts: list[dict], depuis_ts: float) -> bool:
    """C-6 : reçu de recherche conforme écrit depuis le début de la requête.

    Vaut pour un reçu `mode="search"` (courtier) OU la trace d'un `explore-leger`
    (champ `source`/`via` mentionnant « explore »). Les reçus directs
    (`via="direct"`) sont exclus, comme partout (C-4). Fonction pure.
    """
    for r in receipts:
        if _est_appel_direct(r):
            continue
        mode = str(r.get("mode") or "").strip().lower()
        trace = f"{r.get('source') or ''} {r.get('via') or ''}".lower()
        if mode != "search" and "explore" not in trace:
            continue
        ts = r.get("ts")
        if ts is None:
            continue
        try:
            if float(ts) >= depuis_ts:
                return True
        except (TypeError, ValueError):
            continue
    return False


def message_balayage(receipts_path: Path) -> str:
    """Message de blocage quand un balayage natif n'a pas été délégué (C-6)."""
    return (
        "BLOQUÉ — délégation obligatoire avant ce balayage : la requête courante "
        "demande une recherche/large inventaire (motif détecté dans "
        f"{CURRENT_REQUEST_NAME}). Un `Grep`/`Read` natif rechargerait tout le "
        "contexte dans le fil principal.\n"
        "Délègue d'abord la recherche :\n"
        '  python3 scripts/ai_broker.py --mode search --root <dossier> "<motif>"\n'
        "  ou l'agent `explore-leger` (modèle haiku) pour un balayage guidé.\n"
        "Aucun reçu `search` (ni trace `explore-leger`) depuis le début de la "
        f"requête dans {receipts_path}.\n"
    )


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

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    # Marqueur écrit par `workflow_reminder.py` au `UserPromptSubmit` (C-2) :
    # début de la requête courante, et fait qu'elle a été jugée multi-étapes.
    requete = load_current_request(project_dir)

    # C-1 : au-delà d'un seuil d'appels d'outils dans la requête courante, une
    # écriture de `.py` doit passer par l'ouvrier. Le décompte vient de la
    # télémétrie (`kind="tool"`, écrite en PostToolUse), borné au début de la
    # requête par le marqueur C-2 pour ne pas traîner les requêtes précédentes.
    try:
        min_appels = int(os.environ.get("RADAR_WORKER_GATE_MIN_TOOLS", WORKER_GATE_MIN_TOOLS))
    except ValueError:
        min_appels = int(WORKER_GATE_MIN_TOOLS)
    appels_outils = 0
    if est_ecriture_python(tool_name, tool_input):
        appels_outils = compte_appels_outils(
            telemetry_path_for(project_dir),
            str(event.get("session_id") or ""),
            _ts_requete(requete),
        )

    besoin_delegation = raison_delegation_ecriture(
        tool_name,
        tool_input,
        appels_outils=appels_outils,
        min_appels=min_appels,
        requete=requete,
    )

    if not modes and besoin_delegation is None:
        return 0

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

    # Un reçu écrit par `ai_query.py` en direct porte `via="direct"` : il ne
    # vaut pas délégation conforme (C-4). On le distingue pour orienter le
    # message vers le courtier plutôt que vers une énième tentative directe.
    recu_direct_recent = a_recu_direct_recent(receipts, modes or (), now, ttl)

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

    # C-1 / C-2 : quand c'est la REQUÊTE elle-même qui porte le pattern (chantier
    # multi-outils, ou requête marquée multi-étapes), la délégation exigée est
    # celle du pattern — pas seulement l'acte isolé. Satisfaite, elle REMPLACE le
    # chemin historique : un reçu `worker` atteste que la règle a été exécutée en
    # profondeur, il serait contradictoire d'exiger en plus un reçu code/test du
    # fil principal.
    if besoin_delegation is not None:
        modes_pattern, raison = besoin_delegation
        if a_recu_depuis(receipts, modes_pattern, _debut_requete(requete, now, ttl)):
            return 0
        sys.stderr.write(
            message_pattern(
                raison,
                receipts_path=receipts_path,
                appels_outils=appels_outils,
                min_appels=min_appels,
            )
        )
        return 2

    # C-6 : un balayage demandé par la requête (« où est », « trouve tous »…) ne
    # doit pas se payer en `Grep`/`Read` natif. Il exige un reçu `search` (ou la
    # trace d'un `explore-leger`) écrit depuis le début de la requête ; un reçu
    # `context` seul ne suffit plus ici (contrairement au régime historique de
    # `Grep`). On passe AVANT la vérification historique, sinon un `context`
    # récent laisserait filer le balayage.
    if raison_delegation_balayage(tool_name, modes, requete) is not None:
        if a_recu_recherche(receipts, _debut_requete(requete, now, ttl)):
            return 0
        sys.stderr.write(message_balayage(receipts_path))
        return 2

    if has_recent_receipt(receipts, modes, now, ttl, min_chars, exiger_via_conforme=True):
        return 0

    if modes == _MODES_LARGE_BROKER:
        dleg_desc = "cet appel direct à `ai_query.py` (transport bas niveau, non conforme)"
        cmd_example = (
            "python3 scripts/ai_broker.py --mode "
            "code|test|pr|diag|context|search|reasoning ...   (seul relais conforme)"
        )
    elif modes == ("pr", "diag", "context"):
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
    elif tool_name == "Edit":
        # C-3 : la branche ci-dessus (modes[0] == 'test') a déjà couvert un `Edit`
        # massif sur un fichier de test ; on arrive ici pour un module ordinaire.
        dleg_desc = ("cette réécriture large d'un module existant (au-delà du seuil "
                     "chirurgical)")
        cmd_example = ("python3 scripts/ai_broker.py --mode code --check-syntax "
                       "-o <fichier_cible> '<spec>'   (ou scripts/ai_worker.py pour "
                       "un chantier explorer+modifier+tester)")
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
    via_hint = (
        "\nUn reçu récent existe mais vient d'un appel DIRECT à `ai_query.py` "
        "(via=\"direct\") : ça ne vaut pas délégation conforme. Passe par le "
        "courtier `ai_broker.py`.\n"
        if recu_direct_recent else ""
    )
    requete_hint = (
        "\nRequête courante marquée « à plusieurs étapes » (marqueur "
        f"{CURRENT_REQUEST_NAME}) : elle attend un WBS découpé par LIVRABLE et "
        "une sous-traitance AVANT d'agir.\n"
        if requete.get("non_trivial") else ""
    )
    sys.stderr.write(
        f"BLOQUÉ — délégation obligatoire avant cette action : {dleg_desc}.\n"
        f"Aucun reçu conforme de moins de {int(ttl)} s{chars_detail} dans {receipts_path}.\n"
        f"{via_hint}"
        f"{requete_hint}"
        f"Lance d'abord :\n  {cmd_example}\n"
        f"{surgical_hint}"
        "Si tous les fournisseurs gratuits sont épuisés (quota/panne), la tentative "
        "ratée écrit elle-même un reçu et débloque l'action : il faut l'AVOIR "
        "tentée, pas l'avoir supposée.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
