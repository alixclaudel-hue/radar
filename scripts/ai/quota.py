#!/usr/bin/env python3
"""Ledger d'usage, rotation de clés, plafond payant, reçus (plan §4.1 et §4.3).

Deux fichiers d'état, tous deux sous `<usage_dir>/` :

- `ai_usage.jsonl` — une ligne par appel **réellement tenté** : fournisseur,
  modèle, clé, mode, tier, gratuit ou payant, jetons rapportés, coût estimé.
  C'est un journal, jamais réécrit, donc lisible par `radar_ops`.
- `ai_quota.json` — un compteur par clé et par fenêtre, plus la dépense payante
  du jour. Réécrit à chaque appel, de façon atomique.

Le compteur par clé est ce qui rend la rotation utile : avec `N` clés, la clé la
moins sollicitée de la fenêtre passe en premier, ce qui étale la consommation au
lieu d'épuiser la première puis de basculer. C'est la transposition de la
stratégie déjà rédigée pour les clés YouTube
([`docs/Strategie rotatio clés API.txt`](../../docs/Strategie%20rotatio%20clés%20API.txt:1)),
appliquée aux fournisseurs d'IA.

Règle de conception : **rien ici ne lève**. Un disque plein, un état corrompu ou
un dossier inaccessible ne doivent pas faire échouer un appel payé par ailleurs
correct — ils dégradent la mesure, ils ne cassent pas le service.
"""

from __future__ import annotations

import json
import os
import time

from scripts.ai_query import receipts_dir, write_receipt  # noqa: F401  (réexports)

from scripts.ai import catalogue as cat_mod

LEDGER_NAME = "ai_usage.jsonl"
STATE_NAME = "ai_quota.json"

# Durée pendant laquelle une clé ayant répondu 401/403 est écartée. Assez long
# pour ne pas la représenter à chaque appel d'une session, assez court pour
# qu'une clé correcte le redevienne au tour suivant si l'erreur était passagère.
KEY_BAD_TTL = 3600.0


def usage_dir() -> str:
    """Dossier de l'état d'usage.

    `RADAR_AI_USAGE_DIR` d'abord, sinon `<dossier des reçus>/ai` — donc le même
    dossier partagé que les reçus sur le VPS (`/data/ops`), pour que `radar_ops`
    n'ait qu'un endroit à lire. Le banc pointe cette variable vers un dossier
    temporaire : une mesure hors ligne ne doit jamais compter dans la
    comptabilité réelle.
    """
    override = (os.environ.get("RADAR_AI_USAGE_DIR") or "").strip()
    if override:
        return override
    return os.path.join(receipts_dir(), "ai")


def ledger_path(path: str | None = None) -> str:
    return path or os.path.join(usage_dir(), LEDGER_NAME)


def state_path(path: str | None = None) -> str:
    return path or os.path.join(usage_dir(), STATE_NAME)


def now_ts() -> float:
    return time.time()


def window_id(window: str, now: float | None = None) -> str:
    """Identifiant de la fenêtre courante, au format propre à chaque granularité.

    Comparer l'identifiant de fenêtre stocké à l'identifiant courant fait
    office de réinitialisation : aucune tâche planifiée, aucun compteur à
    remettre à zéro, la fenêtre change toute seule au changement de jour, d'heure
    ou de mois.
    """
    moment = time.gmtime(now if now is not None else now_ts())
    if window == cat_mod.WINDOW_HOURLY:
        return time.strftime("%Y-%m-%dT%H", moment)
    if window == cat_mod.WINDOW_MONTHLY:
        return time.strftime("%Y-%m", moment)
    return time.strftime("%Y-%m-%d", moment)


def blank_state() -> dict:
    return {"keys": {}, "paid": {"window": "", "spent_usd": 0.0}, "updated": 0.0}


def load_state(path: str | None = None) -> dict:
    """État d'usage ; toute anomalie rend un état vierge plutôt qu'une exception."""
    try:
        with open(state_path(path), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return blank_state()
    state = blank_state()
    if isinstance(data, dict):
        state.update(data)
    if not isinstance(state.get("keys"), dict):
        state["keys"] = {}
    if not isinstance(state.get("paid"), dict):
        state["paid"] = {"window": "", "spent_usd": 0.0}
    return state


def save_state(state: dict, path: str | None = None) -> bool:
    """Écriture atomique (`tmp` puis `replace`) : jamais d'état à moitié écrit."""
    target = state_path(path)
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        state["updated"] = now_ts()
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        return True
    except Exception:
        return False


def append_ledger(row: dict, path: str | None = None) -> bool:
    """Ajoute une ligne au journal d'usage. N'échoue jamais bruyamment."""
    target = ledger_path(path)
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        entry = dict(row)
        entry.setdefault("ts", now_ts())
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def read_ledger(path: str | None = None, limit: int | None = None) -> list[dict]:
    """Relit le journal d'usage (diagnostic, tableau de bord, banc)."""
    lignes: list[dict] = []
    try:
        with open(ledger_path(path), "r", encoding="utf-8", errors="replace") as f:
            for brute in f:
                brute = brute.strip()
                if not brute:
                    continue
                try:
                    entry = json.loads(brute)
                except Exception:
                    continue
                if isinstance(entry, dict):
                    lignes.append(entry)
    except Exception:
        return []
    if limit is not None and limit >= 0:
        return lignes[-limit:]
    return lignes


def _key_slot(provider: str, key_id: str) -> str:
    return f"{provider}|{key_id}"


def count_in_window(
    state: dict, provider: str, key_id: str, window: str, now: float | None = None
) -> int:
    """Appels déjà passés par cette clé dans la fenêtre courante."""
    entry = (state.get("keys") or {}).get(_key_slot(provider, key_id)) or {}
    if entry.get("window") != window_id(window, now):
        return 0
    try:
        return int(entry.get("count") or 0)
    except (TypeError, ValueError):
        return 0


def register_call(
    state: dict,
    provider: str,
    key_id: str,
    window: str,
    now: float | None = None,
    reset_window: str | None = None,
) -> dict:
    """Incrémente le compteur d'une clé, en repartant de zéro si la fenêtre a tourné."""
    moment = now if now is not None else now_ts()
    identifiant = window_id(window, moment)
    slot = _key_slot(provider, key_id)
    entries = state.setdefault("keys", {})
    entry = entries.get(slot) if isinstance(entries.get(slot), dict) else {}
    if entry.get("window") != identifiant:
        precedente = dict(entry)
        entry = {"window": identifiant, "count": 0}
        # `bad_until` survit au changement de fenêtre : sa durée de vie est déjà
        # bornée par `KEY_BAD_TTL` dans `is_key_bad()`. Le remettre à zéro ici
        # ressusciterait une clé rejetée avant l'heure, et un 403 en boucle
        # consommerait un appel par fenêtre pour rien.
        if precedente.get("bad_until"):
            entry["bad_until"] = precedente["bad_until"]
    entry["count"] = int(entry.get("count") or 0) + 1
    entry["window"] = identifiant
    entry["provider"] = provider
    entry["key_id"] = key_id
    if reset_window:
        entry["reset_window"] = reset_window
    entries[slot] = entry
    return state


def is_key_bad(state: dict, provider: str, key_id: str, now: float | None = None) -> bool:
    """La clé a-t-elle été rejetée récemment (401/403) ?"""
    moment = now if now is not None else now_ts()
    entry = (state.get("keys") or {}).get(_key_slot(provider, key_id)) or {}
    try:
        return float(entry.get("bad_until") or 0) > moment
    except (TypeError, ValueError):
        return False


def mark_key_bad(
    state: dict,
    provider: str,
    key_id: str,
    now: float | None = None,
    ttl: float = KEY_BAD_TTL,
) -> dict:
    """Écarte une clé rejetée par le fournisseur.

    C'est ce qui remplace le comportement observé en Phase 0 : sur un 403, la
    cascade mono-fournisseur brûlait les six modèles de la liste pour rien
    (5 reçus `http_403` sur 88 appels réels). Ici, la clé fautive est mise de
    côté, et le routage cesse de proposer les modèles de ce fournisseur tant
    qu'aucune autre clé n'est disponible.
    """
    moment = now if now is not None else now_ts()
    slot = _key_slot(provider, key_id)
    entries = state.setdefault("keys", {})
    entry = entries.get(slot) if isinstance(entries.get(slot), dict) else {}
    entry["bad_until"] = moment + max(0.0, float(ttl))
    entry["provider"] = provider
    entry["key_id"] = key_id
    entries[slot] = entry
    return state


def pick_key(
    state: dict,
    provider: str,
    candidates: list[str],
    window: str,
    limit: int | None = None,
    now: float | None = None,
) -> str | None:
    """Choisit la clé la moins utilisée de la fenêtre, ou `None` si aucune n'est utilisable.

    `candidates` sont des **noms de variables d'environnement** (jamais des
    secrets) : c'est l'appelant qui résout la valeur, ce qui garde ce module
    incapable de divulguer une clé.

    L'ordre est stable à consommation égale (ordre de déclaration), pour qu'un
    appel ne change pas de clé sans raison et que le banc soit reproductible.
    """
    if not candidates:
        return None
    utilisables: list[tuple[int, int, str]] = []
    for rang, key_id in enumerate(candidates):
        if is_key_bad(state, provider, key_id, now):
            continue
        compte = count_in_window(state, provider, key_id, window, now)
        if limit is not None and compte >= limit:
            continue
        utilisables.append((compte, rang, key_id))
    if not utilisables:
        return None
    utilisables.sort()
    return utilisables[0][2]


def paid_spent(state: dict, now: float | None = None) -> float:
    """Dépense payante cumulée de la fenêtre journalière courante."""
    paid = state.get("paid") or {}
    if paid.get("window") != window_id(cat_mod.WINDOW_DAILY, now):
        return 0.0
    try:
        return float(paid.get("spent_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def charge_paid(state: dict, cost: float, now: float | None = None) -> dict:
    """Impute un coût au compteur payant du jour.

    Appelé **après** un appel payant réussi comme après un échec facturé : un
    fournisseur facture les jetons d'entrée même quand il refuse de répondre.
    """
    moment = now if now is not None else now_ts()
    identifiant = window_id(cat_mod.WINDOW_DAILY, moment)
    paid = state.setdefault("paid", {"window": "", "spent_usd": 0.0})
    if paid.get("window") != identifiant:
        paid["window"] = identifiant
        paid["spent_usd"] = 0.0
    try:
        paid["spent_usd"] = float(paid.get("spent_usd") or 0.0) + max(0.0, float(cost))
    except (TypeError, ValueError):
        paid["spent_usd"] = max(0.0, float(cost))
    return state


def paid_cap_usd(policy: dict | None = None) -> float:
    """Plafond payant journalier : `RADAR_AI_DAILY_PAID_CAP_USD`, sinon catalogue.

    Défaut **0,00 $** quand rien n'est déclaré. Ce n'est pas un oubli : sans
    plafond déclaré, aucun appel payant n'est possible, donc aucun euro ne peut
    partir sans décision explicite de l'opérateur.
    """
    brut = os.environ.get("RADAR_AI_DAILY_PAID_CAP_USD")
    if brut is None or not str(brut).strip():
        brut = (policy or {}).get("daily_paid_cap_usd")
    if brut is None or not str(brut).strip():
        return 0.0
    try:
        return max(0.0, float(str(brut).strip()))
    except (TypeError, ValueError):
        return 0.0


def paid_allowed(
    state: dict,
    policy: dict | None,
    *,
    mode: str,
    allow_paid: bool = False,
    escalate: bool = False,
    estimated_cost: float = 0.0,
    now: float | None = None,
) -> tuple[bool, str]:
    """Un appel payant est-il autorisé, et sinon pourquoi ?

    Trois voies d'autorisation, toutes explicites :

    1. le mode est déclaré payant d'emblée dans le catalogue (`paid_modes`) ;
    2. l'opérateur l'a demandé pour cet appel (`--allow-paid`) ;
    3. l'escalade est déclenchée par un échec vérifié (`--escalate`).

    Aucune quatrième voie. Un dépassement de plafond n'« avertit » pas : il
    refuse l'appel et rend la raison, pour qu'un reçu dise exactement pourquoi
    le payant n'a pas été utilisé.
    """
    if estimated_cost <= 0.0:
        return True, "gratuit"

    modes_payants = set((policy or {}).get("paid_modes") or [])
    autorise = bool(allow_paid) or bool(escalate) or mode in modes_payants
    if not autorise:
        return False, (
            f"appel payant refusé : mode '{mode}' hors payants autorisés "
            f"(ni --allow-paid, ni --escalate)"
        )

    plafond = paid_cap_usd(policy)
    if plafond <= 0.0:
        return False, (
            "appel payant refusé : plafond journalier à 0 "
            "(RADAR_AI_DAILY_PAID_CAP_USD non déclarée)"
        )

    deja = paid_spent(state, now)
    if deja + estimated_cost > plafond:
        return False, (
            f"appel payant refusé : plafond journalier atteint "
            f"({deja:.4f} $ dépensés sur {plafond:.4f} $)"
        )
    return True, ""
