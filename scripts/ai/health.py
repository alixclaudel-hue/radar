#!/usr/bin/env python3
"""Disjoncteur par (fournisseur, modèle) — plan §4.1 et §10.

Un modèle qui répond 429 ou 503 ne doit pas être reproposé au candidat suivant :
c'est exactement le gaspillage mesuré en Phase 0 — 25 % des appels réels ont
consommé un repli, et 12 appels ont épuisé une cascade de six modèles. Ici, un
échec durable écarte le modèle pour un temps, et le routage n'a plus à le
découvrir.

Deux natures d'échec, deux durées :

- **429** : le fournisseur dit lui-même quand revenir (`Retry-After`) quand il
  le publie ; on le croit. Sinon, backoff exponentiel : 2 min, 4 min, 8 min…
- **404** : le modèle n'existe plus (retiré ou renommé). Il ne revient pas par
  attente : il est retiré jusqu'au prochain rafraîchissement du catalogue.
- **5xx / panne réseau** : aléa, backoff exponentiel comme un 429 sans en-tête.

Ce qui n'est **pas** traité ici, volontairement : les 401/403. Ce n'est pas le
modèle qui est en cause mais la clé — le ledger
([`quota.mark_key_bad()`](quota.py:1)) s'en charge, et le routage écarte alors
tous les modèles de ce fournisseur faute de clé utilisable. Mettre un modèle sain
en quarantaine pour une clé morte serait une fausse conclusion.
"""

from __future__ import annotations

import json
import os
import time

from scripts.ai import quota

STATE_NAME = "ai_health.json"

# Cooldown d'un modèle écarté : bornes de repli quand le fournisseur ne dit rien.
BASE_COOLDOWN = 120.0
MAX_COOLDOWN = 3600.0

# Statuts que l'attente ne répare pas : le modèle a disparu du catalogue.
RETIRED_STATUSES = frozenset({404})

# Statuts dont la cause n'est ni le modèle ni la clé, mais la requête : rien à
# retenir durablement, sinon on condamne un modèle sain pour un prompt trop long.
NEUTRAL_STATUSES = frozenset({400, 413, 422})


def health_path(path: str | None = None) -> str:
    return path or os.path.join(quota.usage_dir(), STATE_NAME)


def blank_state() -> dict:
    return {"entries": {}, "updated": 0.0}


def load(path: str | None = None) -> dict:
    """État du disjoncteur ; toute anomalie rend un état vierge."""
    try:
        with open(health_path(path), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return blank_state()
    state = blank_state()
    if isinstance(data, dict):
        state.update(data)
    if not isinstance(state.get("entries"), dict):
        state["entries"] = {}
    return state


def save(state: dict, path: str | None = None) -> bool:
    """Écriture atomique, jamais bloquante."""
    target = health_path(path)
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        state["updated"] = time.time()
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        return True
    except Exception:
        return False


def key(provider: str, model: str) -> str:
    return f"{provider}|{model}"


def entry(state: dict, provider: str, model: str) -> dict:
    found = (state.get("entries") or {}).get(key(provider, model))
    return found if isinstance(found, dict) else {}


def reason(state: dict, provider: str, model: str, now: float | None = None) -> str:
    """Pourquoi ce modèle n'est pas proposable ; chaîne vide s'il l'est.

    Sert aussi au diagnostic : `--list-candidates` affiche la raison exacte de
    chaque écart, ce qui vaut mieux qu'un modèle qui disparaît en silence.
    """
    moment = now if now is not None else time.time()
    found = entry(state, provider, model)
    if found.get("retired"):
        return "modèle retiré du catalogue (404)"
    try:
        until = float(found.get("cooldown_until") or 0.0)
    except (TypeError, ValueError):
        until = 0.0
    if until > moment:
        return f"en cooldown {until - moment:.0f}s (dernier statut {found.get('last_status')})"
    return ""


def is_available(state: dict, provider: str, model: str, now: float | None = None) -> bool:
    return not reason(state, provider, model, now)


def record_success(state: dict, provider: str, model: str) -> dict:
    """Un succès efface l'historique d'échec : le compteur repart de zéro.

    Garder trace d'anciens échecs ferait croître le cooldown indéfiniment pour un
    modèle qui répond très bien depuis.
    """
    (state.setdefault("entries", {})).pop(key(provider, model), None)
    return state


def cooldown_seconds(
    failures: int,
    retry_after: float | None = None,
    base: float = BASE_COOLDOWN,
    max_delay: float = MAX_COOLDOWN,
) -> float:
    """Durée d'écartement : `Retry-After` s'il existe, sinon backoff doublant."""
    if retry_after is not None:
        # On ajoute une seconde de marge : rejouer exactement à l'instant annoncé
        # retombe régulièrement sur la même limite.
        return min(max_delay, max(0.0, float(retry_after)) + 1.0)
    puissance = max(1, int(failures))
    return min(max_delay, base * (2 ** (puissance - 1)))


def record_failure(
    state: dict,
    provider: str,
    model: str,
    *,
    status: int | None = None,
    retry_after: float | None = None,
    now: float | None = None,
    base: float = BASE_COOLDOWN,
    max_delay: float = MAX_COOLDOWN,
    detail: str | None = None,
) -> dict:
    """Enregistre un échec et en déduit la suite à donner à ce modèle.

    Rend l'état modifié ; ne lève jamais. Un statut neutre (400/413/422) est
    enregistré pour le diagnostic (`last_status`) mais ne pose aucun cooldown :
    le modèle n'a rien fait de mal, la requête était mal formée pour lui.
    """
    moment = now if now is not None else time.time()
    entries = state.setdefault("entries", {})
    slot = key(provider, model)
    courant = entries.get(slot) if isinstance(entries.get(slot), dict) else {}
    entree = dict(courant)
    entree["failures"] = int(entree.get("failures") or 0) + 1
    entree["last_status"] = status
    entree["updated"] = moment
    if detail:
        # Tronqué : un reçu ou un état ne doit pas devenir un dépotoir de logs.
        entree["last_detail"] = str(detail)[:200]

    if status in RETIRED_STATUSES:
        entree["retired"] = True
        # On conserve un cooldown pour que `reason()` reste parlant même si un
        # rafraîchissement tardif ramenait le modèle dans le catalogue.
        entree["cooldown_until"] = moment + max_delay
    elif status in NEUTRAL_STATUSES:
        entree.pop("retired", None)
    else:
        entree["cooldown_until"] = moment + cooldown_seconds(
            entree["failures"], retry_after, base, max_delay
        )

    entries[slot] = entree
    return state


def retired(state: dict) -> list[tuple[str, str]]:
    """Modèles retirés par un 404 — à ne plus rafraîchir sans vérification."""
    out: list[tuple[str, str]] = []
    for slot, entree in (state.get("entries") or {}).items():
        if isinstance(entree, dict) and entree.get("retired"):
            provider, _, model = str(slot).partition("|")
            out.append((provider, model))
    return sorted(out)


def cooling(state: dict, now: float | None = None) -> list[tuple[str, str, float]]:
    """Modèles actuellement en cooldown, avec le temps restant en secondes."""
    moment = now if now is not None else time.time()
    out: list[tuple[str, str, float]] = []
    for slot, entree in (state.get("entries") or {}).items():
        if not isinstance(entree, dict):
            continue
        try:
            until = float(entree.get("cooldown_until") or 0.0)
        except (TypeError, ValueError):
            continue
        if until > moment and not entree.get("retired"):
            provider, _, model = str(slot).partition("|")
            out.append((provider, model, until - moment))
    return sorted(out, key=lambda item: -item[2])


def prune(state: dict, now: float | None = None) -> dict:
    """Retire les entrées expirées, sauf les modèles retirés (information durable)."""
    moment = now if now is not None else time.time()
    entries = state.get("entries") or {}
    for slot in list(entries):
        entree = entries.get(slot)
        if not isinstance(entree, dict) or entree.get("retired"):
            continue
        try:
            until = float(entree.get("cooldown_until") or 0.0)
        except (TypeError, ValueError):
            until = 0.0
        if until <= moment:
            entries.pop(slot, None)
    return state
