#!/usr/bin/env python3
"""Catalogue engendré des modèles, et politique de routage (plan §4.1).

Le catalogue `config/ai_models.json` n'est **pas** saisi à la main : il est
produit par [`scripts/ai/refresh_models.py`](refresh_models.py:1), qui interroge
la liste de modèles de chaque fournisseur. La raison est mesurable : un nom de
modèle retiré coûte un aller-retour perdu et un reçu d'échec de plus — la
Phase 0 en a compté un (`gemini-3.1-pro-preview`, présent dans la cascade,
jamais capable de répondre).

Ce module ne fait que **lire** ce fichier, avec une tolérance assumée : un
catalogue absent, tronqué ou illisible rend un catalogue vide au lieu de lever.
Une passerelle qui refuse de démarrer parce que son cache de modèles est corrompu
échangerait un problème de fraîcheur contre une panne totale.

Un nom de fournisseur n'est jamais écrit en dur ailleurs que dans le fichier de
configuration ; le code ne connaît que des **formes** (`kind` = `openai` ou
`gemini`).
"""

from __future__ import annotations

import json
import os
import time

DEFAULT_CATALOGUE_NAME = os.path.join("config", "ai_models.json")

# Fenêtres de réinitialisation reconnues. Une fenêtre inconnue retombe sur
# `daily` : le pire cas est de sous-compter un quota, jamais de le dépasser.
WINDOW_DAILY = "daily"
WINDOW_HOURLY = "hourly"
WINDOW_MONTHLY = "monthly"
KNOWN_WINDOWS = frozenset({WINDOW_DAILY, WINDOW_HOURLY, WINDOW_MONTHLY})


def repo_root() -> str:
    """Racine du dépôt — `scripts/ai/catalogue.py` est à trois niveaux."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def catalogue_path(path: str | None = None) -> str:
    """Emplacement du catalogue : argument, puis `RADAR_AI_CATALOGUE`, puis dépôt.

    L'override par variable d'environnement existe pour la même raison que
    `RADAR_TELEMETRY_DIR` : sur le VPS, deux checkouts du même dépôt coexistent
    (`~/radar` avec le volume `/data`, `~/radar-work` sans) et un chemin relatif
    agit silencieusement dans le mauvais.
    """
    if path:
        return path
    override = (os.environ.get("RADAR_AI_CATALOGUE") or "").strip()
    if override:
        return override
    return os.path.join(repo_root(), DEFAULT_CATALOGUE_NAME)


def default_policy() -> dict:
    """Politique par défaut, volontairement restrictive.

    `paid_allowlist` est **vide** : aucun identifiant de modèle payant n'est
    écrit dans le code. Les modèles payants autorisés vivent dans le fichier de
    configuration, où l'opérateur les valide explicitement.

    `daily_paid_cap_usd` vaut `None`, ce qui laisse la main à
    `RADAR_AI_DAILY_PAID_CAP_USD` et, en dernier recours, à zéro : **aucun appel
    payant n'est possible tant que le plafond n'a pas été déclaré**. C'est la
    traduction littérale de « jamais de payant silencieux ».
    """
    return {
        "free_first": True,
        "paid_modes": ["reasoning"],
        "paid_allowlist": [],
        "daily_paid_cap_usd": None,
        "max_candidates": 6,
        "retry": {"per_model": 2, "base_delay": 4.0, "max_delay": 30.0},
    }


def empty_catalogue() -> dict:
    """Catalogue vide mais structurellement valide."""
    return {
        "version": 1,
        "generated_at": None,
        "generated_by": None,
        "policy": default_policy(),
        "providers": {},
    }


def load(path: str | None = None) -> dict:
    """Charge le catalogue ; toute erreur rend un catalogue vide."""
    target = catalogue_path(path)
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return empty_catalogue()
    if not isinstance(data, dict):
        return empty_catalogue()
    cat = empty_catalogue()
    cat.update(data)
    cat["policy"] = policy(cat)
    cat.setdefault("providers", {})
    if not isinstance(cat["providers"], dict):
        cat["providers"] = {}
    return cat


def save(catalogue: dict, path: str | None = None) -> bool:
    """Écrit le catalogue de façon atomique. Rend `False` en cas d'échec."""
    target = catalogue_path(path)
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(catalogue, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, target)
        return True
    except Exception:
        return False


def policy(catalogue: dict | None = None) -> dict:
    """Politique effective : celle du fichier, complétée par les défauts."""
    merged = default_policy()
    raw = (catalogue or {}).get("policy") or {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            merged[key] = value
    retry = dict(merged.get("retry") or {})
    raw_retry = raw.get("retry") if isinstance(raw, dict) else None
    if isinstance(raw_retry, dict):
        retry.update(raw_retry)
    merged["retry"] = retry
    if not isinstance(merged.get("paid_allowlist"), list):
        merged["paid_allowlist"] = []
    if not isinstance(merged.get("paid_modes"), list):
        merged["paid_modes"] = list(default_policy()["paid_modes"])
    return merged


def provider_names(catalogue: dict) -> list[str]:
    return sorted((catalogue or {}).get("providers", {}) or {})


def provider_spec(catalogue: dict, provider: str) -> dict:
    """Spécification d'un fournisseur, ou dict vide s'il est inconnu."""
    spec = ((catalogue or {}).get("providers", {}) or {}).get(provider)
    return spec if isinstance(spec, dict) else {}


def models(catalogue: dict, provider: str) -> list[dict]:
    """Modèles déclarés d'un fournisseur, entrées malformées écartées."""
    raw = provider_spec(catalogue, provider).get("models") or []
    return [m for m in raw if isinstance(m, dict) and m.get("id")]


def all_models(catalogue: dict) -> list[tuple[str, dict]]:
    """Toutes les entrées du catalogue, avec leur fournisseur."""
    out: list[tuple[str, dict]] = []
    for provider in provider_names(catalogue):
        out.extend((provider, entry) for entry in models(catalogue, provider))
    return out


def find_model(catalogue: dict, model_id: str) -> tuple[str, dict] | None:
    """Retrouve un modèle précis, quelle que soit sa place dans le catalogue."""
    if not model_id:
        return None
    for provider, entry in all_models(catalogue):
        if entry.get("id") == model_id:
            return provider, entry
    return None


def is_free(entry: dict) -> bool:
    """Un modèle est gratuit si son prix est nul ou si son identifiant le dit.

    Les deux critères sont acceptés parce que les fournisseurs ne sont pas
    cohérents : OpenRouter marque `:free` en suffixe **et** met un prix nul,
    l'API Gemini ne publie aucun prix (tout est dans le palier gratuit).
    """
    if entry.get("free") is True:
        return True
    if str(entry.get("id", "")).endswith(":free"):
        return True
    pricing = entry.get("pricing") or {}
    try:
        return (
            float(pricing.get("prompt_per_1m") or 0) == 0.0
            and float(pricing.get("completion_per_1m") or 0) == 0.0
        )
    except (TypeError, ValueError):
        return False


def key_env_names(catalogue: dict, provider: str) -> list[str]:
    """Noms des variables d'environnement qui portent les clés d'un fournisseur.

    Une liste explicite dans le catalogue (`keys_env`) prime ; sinon la
    convention `<FOURNISSEUR>_API_KEY_1..N` puis `<FOURNISSEUR>_API_KEY`. La
    rotation s'appuie sur cette liste : c'est elle qui rend l'ajout d'une clé
    possible sans toucher au code.
    """
    spec = provider_spec(catalogue, provider)
    declared = spec.get("keys_env")
    if isinstance(declared, list) and declared:
        return [str(name) for name in declared]
    prefix = provider.upper()
    return [f"{prefix}_API_KEY_{i}" for i in range(1, 10)] + [f"{prefix}_API_KEY"]


def quota_spec(catalogue: dict, provider: str) -> dict:
    """Quota déclaré du fournisseur : `{"window", "limit"}`.

    `limit` valant `None` signifie « non documenté, non vérifié » — le cas de
    Gemini et des fournisseurs payants. On ne l'invente pas : un plafond inventé
    écarte des modèles qui fonctionnent, un plafond absent laisse le disjoncteur
    et le plafond payant faire leur travail.
    """
    spec = provider_spec(catalogue, provider)
    quota = spec.get("quota") or {}
    window = quota.get("window") or WINDOW_DAILY
    if window not in KNOWN_WINDOWS:
        window = WINDOW_DAILY
    limit = quota.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
            if limit <= 0:
                limit = None
        except (TypeError, ValueError):
            limit = None
    return {"window": window, "limit": limit}


def cost_estimate(entry: dict, prompt_tokens: int, output_tokens: int) -> float:
    """Coût estimé en USD d'un appel, d'après le prix du catalogue.

    Estimation **a priori** : elle sert à refuser un appel payant qui ferait
    dépasser le plafond, avant de le lancer. Le coût réellement imputé par le
    fournisseur reste la référence, mais il n'est connu qu'après coup.
    """
    pricing = entry.get("pricing") or {}
    try:
        prompt_price = float(pricing.get("prompt_per_1m") or 0.0)
        completion_price = float(pricing.get("completion_per_1m") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return (prompt_tokens / 1_000_000.0) * prompt_price + (
        output_tokens / 1_000_000.0
    ) * completion_price


def touch(catalogue: dict, *, generator: str) -> dict:
    """Estampille le catalogue avant écriture (traçabilité de la fraîcheur)."""
    catalogue["generated_at"] = time.time()
    catalogue["generated_by"] = generator
    catalogue["version"] = int(catalogue.get("version") or 1)
    return catalogue
