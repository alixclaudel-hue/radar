#!/usr/bin/env python3
"""Engendre `config/ai_models.json` depuis les API des fournisseurs (plan §4.1).

Le catalogue n'est **jamais** saisi à la main, et aucun identifiant de modèle
n'est écrit en dur dans ce script. Il est lu là où il est vrai — la liste de
modèles de chaque fournisseur — pour la raison mesurée en Phase 0 : un nom de
modèle retiré coûte un aller-retour perdu et un reçu d'échec de plus
(`gemini-3.1-pro-preview` figurait dans la cascade et n'a jamais répondu).

Ce que le rafraîchissement **ne touche pas** — c'est la partie importante : les
décisions de l'opérateur vivent dans le même fichier que les faits techniques, et
elles sont reportées d'une génération à l'autre.

- `policy` : `paid_allowlist`, `paid_modes`, `daily_paid_cap_usd`, `free_first`,
  `max_candidates`, `retry` ;
- par modèle : `paid_ok`, `rank`, `tier`, `pricing` et `label` déclarés à la main.

La raison est concrète : un catalogue engendré écrase ses propres ajouts à chaque
rafraîchissement s'il ne les reporte pas. Un `paid_allowlist` perdu en silence
ferait passer un appel payant de « validé » à « non validé » sans que personne ne
l'ait demandé — ou l'inverse, ce qui serait pire.

Trois sources de vérité, trois traitements :

- **OpenRouter** publie sa liste sans authentification (`/api/v1/models`), avec
  prix et fenêtre de contexte. On n'y garde que le gratuit ;
- **Gemini** publie sa liste avec la clé (`v1beta/models`) et ne publie **aucun**
  prix : tout y est dans le palier gratuit. On n'y garde que les modèles qui
  savent faire du texte.
- **DeepSeek** et **xAI** exigent une clé même pour lister. Sans clé, le bloc
  précédent est conservé tel quel plutôt qu'effacé : un rafraîchissement raté ne
  doit pas amputer la passerelle de fournisseurs qui fonctionnaient.

Usage :

    python3 scripts/ai/refresh_models.py                    # tout ce qui est joignable
    python3 scripts/ai/refresh_models.py --provider gemini  # un seul fournisseur
    python3 scripts/ai/refresh_models.py --dry-run          # montre sans écrire
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# Même amorçage que `scripts/ai_broker.py` : lancé en direct, ce script doit
# pouvoir importer le socle `scripts.ai` sans dépendre du répertoire courant.
_RACINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _RACINE not in sys.path:
    sys.path.insert(0, _RACINE)

from scripts.ai import catalogue as cat_mod  # noqa: E402
from scripts.ai import providers  # noqa: E402

# ---------------------------------------------------------------------------
# Sources : ce qu'on sait du fournisseur, hors modèles
# ---------------------------------------------------------------------------
#
# Ces informations sont des **faits d'API** (point d'entrée, forme du champ de
# raisonnement, fenêtre de quota), pas des identifiants de modèles. Ils sont donc
# légitimes ici ; tout ce qui désigne un modèle vient de la réponse de l'API.

SOURCES: dict[str, dict] = {
    "openrouter": {
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "keys_env": ["OPENROUTER_API_KEY_1", "OPENROUTER_API_KEY_2", "OPENROUTER_API_KEY"],
        # 50 req/jour sous 10 $ de crédit cumulé, 1000 req/jour au-delà. Le
        # chiffre exact est un point de vérification du plan (§11) : tant qu'il
        # n'est pas constaté, on déclare la limite documentée la plus basse.
        "quota": {"window": "daily", "limit": 50},
        "reasoning_style": "reasoning_effort",
        "models_public": True,
        "free_only": True,
        "headers": {"HTTP-Referer": "https://github.com/radar", "X-Title": "radar"},
    },
    "gemini": {
        "kind": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "keys_env": ["GEMINI_API_KEY_1", "GEMINI_API_KEY_2", "GEMINI_API_KEY"],
        # Palier gratuit exprimé en requêtes/minute et par jour selon le modèle :
        # non publié de façon stable, donc non inventé. `None` = non plafonné par
        # nous, le disjoncteur et le fournisseur font leur travail.
        "quota": {"window": "daily", "limit": None},
        # Forme native du budget de réflexion chez Gemini : un niveau symbolique
        # (`thinkingLevel`) pour les Gemini 3, un budget en jetons
        # (`thinkingBudget`) pour les 2.5. C'est un fait d'API, pas un
        # identifiant de modèle. L'échelle n'a que deux crans utiles : `medium`
        # est volontairement absent du map, et n'envoie donc rien.
        "reasoning_style": "thinking_level",
        "reasoning_map": {"low": "low", "high": "high"},
        "models_public": False,
        "free_only": True,
        "headers": {},
    },
    "deepseek": {
        "kind": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "keys_env": ["DEEPSEEK_API_KEY"],
        "quota": {"window": "daily", "limit": None},
        "reasoning_style": "reasoning_effort",
        "models_public": False,
        # Pay-as-you-go : la liste est conservée entière, mais aucun modèle n'est
        # proposé tant qu'il n'est pas validé dans `paid_allowlist`.
        "free_only": False,
        "headers": {},
    },
    "xai": {
        "kind": "openai",
        "base_url": "https://api.x.ai/v1",
        "keys_env": ["XAI_API_KEY"],
        "quota": {"window": "daily", "limit": None},
        "reasoning_style": "effort",
        "models_public": False,
        "free_only": False,
        "headers": {},
    },
}

# Familles qui ne tiennent pas une conversation textuelle. Filtre de **capacité**,
# pas de préférence : envoyer une demande de code à un modèle de musique, d'image
# ou de classement échoue (ou rend une réponse vide), et comme le gratuit passe en
# premier, il échouerait à chaque fois. Les six derniers marqueurs ne sont pas des
# suppositions : ce sont les familles réellement têtes de file du routage gratuit
# relevées le 25/09 — `lyria` (musique, `output_modalities=['text','audio']`),
# `deep-research` et `antigravity` (agents de recherche, pas des complétions),
# `content-safety` (classificateur), `music`, `transcribe` (transcription, qui
# n'annonce aucune modalité de sortie exploitable et passait donc le filtre).
_NON_CHAT_MARKERS = (
    "embedding", "embed-", "aqa", "imagen", "image", "veo", "tts",
    "audio", "live", "whisper", "moderation", "transcribe",
    "lyria", "music", "deep-research", "antigravity", "content-safety",
)

# Modèles qui font eux-mêmes du routage : l'identifiant de réponse ne désigne plus
# le modèle qui a répondu. Le reçu mentirait sur l'attribution, et le coût serait
# attribué au mauvais modèle. Écartés pour cette raison, pas pour qualité.
_META_MODELS = ("openrouter/free",)

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _dev_version(model_id: str) -> float:
    """Version lisible dans un identifiant (`gemini-3.8-flash` → `3.8`).

    Sert uniquement à ordonner : un modèle récent passe avant un ancien à
    qualité supposée égale. Ne sert jamais à inclure ou exclure.
    """
    for brut in _VERSION_RE.findall(model_id or ""):
        try:
            return float(brut)
        except (TypeError, ValueError):
            continue
    return 0.0


def _price_per_1m(value) -> float:
    """Prix par million de jetons, à partir d'un prix par jeton (OpenRouter)."""
    try:
        return float(str(value)) * 1_000_000.0
    except (TypeError, ValueError):
        return 0.0


def _text_capable(raw: dict, ident: str) -> bool:
    """Le modèle tient-il une conversation **textuelle**, et rien d'autre ?

    Deux critères, dans cet ordre :

    1. les modalités de sortie publiées doivent être *exactement* `text`. Le test
       « au moins une modalité texte » laissait passer `google/lyria-3-pro-preview`
       (`output_modalities=['text','audio']`), qui arrivait donc en tête du routage
       gratuit : un modèle qui rend aussi de l'audio n'est pas un modèle de
       conversation, et une demande de code y reviendrait au mieux amputée ;
    2. à défaut de modalités publiées — Gemini n'en publie pas — les marqueurs de
       l'identifiant décident. Un modèle écarté ici ne rentre pas : retirer un
       marqueur demande une mesure, pas une intuition.
    """
    bas = (ident or "").lower()
    if any(marque in bas for marque in _META_MODELS):
        return False
    modalites = raw.get("outputModalities") or raw.get("output_modalities")
    if isinstance(modalites, list) and modalites:
        sorties = {str(m).strip().upper() for m in modalites}
        if sorties - {"TEXT", "TEXTE"}:
            return False
    return not any(marque in bas for marque in _NON_CHAT_MARKERS)


def _supported_parameters(raw: dict) -> dict:
    """Paramètres que le modèle accepte, quand le fournisseur les publie.

    `supports` **absent** veut dire « non publié, donc inconnu » — jamais « aucun ».
    L'adaptateur ne s'en sert que pour *omettre* un champ non déclaré : envoyer
    `response_format` à un modèle qui ne le publie pas (`cohere/north-mini-code:free`
    par exemple) est un 400 garanti, donc un appel gratuit perdu à chaque fois.
    """
    params = raw.get("supported_parameters")
    if isinstance(params, list) and params:
        return {"supports": sorted({str(p).strip() for p in params if str(p).strip()})}
    return {}


def _entry_openrouter(raw: dict) -> dict | None:
    """Entrée de catalogue d'un modèle OpenRouter gratuit, ou `None`."""
    ident = str(raw.get("id") or "").strip()
    if not ident:
        return None
    # Ce filtre manquait : `_entry_gemini` et `_entry_openai_paid` l'appelaient,
    # pas ici — c'est pourquoi la liste des gratuits OpenRouter était menée par
    # `google/lyria-3-*` (sortie `['text','audio']`) et par `openrouter/free`.
    # C'est le seul point d'entrée des modèles OpenRouter au catalogue.
    if not _text_capable(raw, ident):
        return None
    tarifs = raw.get("pricing") or {}
    prompt_prix = _price_per_1m(tarifs.get("prompt"))
    completion_prix = _price_per_1m(tarifs.get("completion"))
    gratuit = ident.endswith(":free") or (prompt_prix == 0.0 and completion_prix == 0.0)
    if not gratuit:
        return None
    contexte = None
    haut = raw.get("top_provider")
    for candidat in (
        raw.get("context_length"),
        haut.get("context_length") if isinstance(haut, dict) else None,
    ):
        try:
            valeur = int(candidat)
        except (TypeError, ValueError):
            continue
        if valeur > 0:
            contexte = valeur
            break
    return {
        "id": ident,
        "free": True,
        "label": str(raw.get("name") or ident),
        "context_length": contexte,
        "pricing": {"prompt_per_1m": prompt_prix, "completion_per_1m": completion_prix},
        # Contexte plus large = rang meilleur, borné : un modèle à 1 M de jetons
        # n'a pas à écraser dix places d'un coup. OpenRouter ne publie aucun indice
        # de qualité — le contexte est le seul fait mesurable, et l'opérateur peut
        # surclasser un modèle à la main via `rank`, que le rafraîchissement reporte.
        "rank": 1000 - min(900, int((contexte or 0) / 1000)),
        **_supported_parameters(raw),
    }


def _entry_gemini(raw: dict) -> dict | None:
    """Entrée de catalogue d'un modèle Gemini textuel, ou `None`."""
    nom = str(raw.get("name") or "").strip()
    ident = nom.split("/", 1)[1] if nom.startswith("models/") else nom
    if not ident:
        return None
    methodes = raw.get("supportedGenerationMethods") or []
    if isinstance(methodes, list) and methodes:
        if "generateContent" not in methodes:
            return None
    if not _text_capable(raw, ident):
        return None
    contexte = None
    try:
        valeur = int(raw.get("inputTokenLimit"))
        if valeur > 0:
            contexte = valeur
    except (TypeError, ValueError):
        contexte = None
    return {
        "id": ident,
        "free": True,
        "label": str(raw.get("displayName") or ident),
        "context_length": contexte,
        # Gemini ne publie pas de prix : gratuit par construction du palier.
        "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
        "rank": 1000 - int(_dev_version(ident) * 10),
    }


def _entry_openai_paid(raw: dict, provider: str) -> dict | None:
    """Entrée de catalogue d'un modèle payant OpenAI-compatible.

    Aucun filtre de version : c'est `paid_allowlist` qui décide, pas ce script.
    Le prix est repris quand le fournisseur le publie (OpenRouter le fait, xAI et
    DeepSeek non) — sans prix, le routeur refusera l'appel, ce qui est le
    comportement voulu : un plafond qu'on ne sait pas chiffrer n'est pas un
    plafond.
    """
    ident = str(raw.get("id") or "").strip()
    if not ident or not _text_capable(raw, ident):
        return None
    tarifs = raw.get("pricing") or {}
    contexte = None
    for candidat in (raw.get("context_length"), raw.get("context_window")):
        try:
            valeur = int(candidat)
        except (TypeError, ValueError):
            continue
        if valeur > 0:
            contexte = valeur
            break
    return {
        "id": ident,
        "free": False,
        "label": str(raw.get("name") or ident),
        "context_length": contexte,
        "pricing": {
            "prompt_per_1m": _price_per_1m(tarifs.get("prompt")),
            "completion_per_1m": _price_per_1m(tarifs.get("completion")),
        },
        "rank": 1000,
        "provider_family": provider,
        **_supported_parameters(raw),
    }


_NORMALIZERS = {
    "openrouter": _entry_openrouter,
    "deepseek": lambda raw: _entry_openai_paid(raw, "deepseek"),
    "xai": lambda raw: _entry_openai_paid(raw, "xai"),
    "gemini": _entry_gemini,
}


# ---------------------------------------------------------------------------
# Report des décisions de l'opérateur
# ---------------------------------------------------------------------------

_PRESERVED_ENTRY_FIELDS = ("paid_ok", "rank", "tier", "label", "pricing")
_PRESERVED_POLICY_FIELDS = (
    "free_first",
    "paid_modes",
    "paid_allowlist",
    "daily_paid_cap_usd",
    "max_candidates",
    "retry",
)


def _previous_entries(catalogue: dict, provider: str) -> dict[str, dict]:
    return {
        str(entry.get("id")): entry for entry in cat_mod.models(catalogue or {}, provider)
    }


def _merge_entry(
    neuf: dict, ancien: dict | None, *, preserve_capabilities: bool = False
) -> dict:
    """Reporte sur l'entrée fraîche ce que l'opérateur avait décidé.

    `preserve_capabilities` est réservé aux fournisseurs dont l'API ne publie
    pas les capacités par modèle (Gemini) : sans lui, un rafraîchissement
    effacerait la liste écrite à la main au catalogue. Il reste faux partout
    ailleurs, pour ne jamais figer ce que l'API publie (OpenRouter).
    """
    if not ancien:
        return neuf
    if preserve_capabilities and ancien.get("supports") and not neuf.get("supports"):
        neuf["supports"] = ancien["supports"]
    for champ in _PRESERVED_ENTRY_FIELDS:
        if champ not in ancien or ancien.get(champ) is None:
            continue
        if champ == "pricing":
            # Une grille déclarée à la main survit à une API muette sur les prix.
            if not (neuf.get("pricing") or {}).get("prompt_per_1m") and not (
                neuf.get("pricing") or {}
            ).get("completion_per_1m"):
                neuf["pricing"] = ancien["pricing"]
            continue
        neuf[champ] = ancien[champ]
    return neuf


def _reconduire(
    bloc: dict,
    anciens: dict[str, dict],
    rapport: dict,
    raison: str,
    *,
    preserve_capabilities: bool = False,
) -> tuple[dict, dict]:
    """Garde le bloc précédent, marqué périmé, et rend le couple attendu.

    Une liste indisponible, absente ou vide n'est pas un fait : c'est un
    incident. Vider le bloc rendrait la passerelle muette sans raison. Les
    quatre reconductions passent par ici pour que la règle n'existe qu'une fois.
    """
    rapport["reason"] = raison
    bloc["models"] = sorted(
        [
            _merge_entry(
                dict(e),
                anciens.get(str(e.get("id"))),
                preserve_capabilities=preserve_capabilities,
            )
            for e in anciens.values()
        ],
        key=lambda e: (int(e.get("rank") or 1000), str(e.get("id"))),
    )
    rapport["kept"] = len(bloc["models"])
    rapport["stale"] = True
    return bloc, rapport


def _merge_policy(catalogue: dict) -> dict:
    """Politique écrite : celle du fichier existant, complétée par les défauts."""
    base = cat_mod.default_policy()
    brut = (catalogue or {}).get("policy")
    if isinstance(brut, dict):
        for champ in _PRESERVED_POLICY_FIELDS:
            if champ in brut and brut.get(champ) is not None:
                base[champ] = brut[champ]
    return base


# ---------------------------------------------------------------------------
# Rafraîchissement
# ---------------------------------------------------------------------------


def refresh_provider(
    name: str,
    source: dict,
    previous_catalogue: dict,
    *,
    key_lookup=providers.env_value,
    timeout: int = 30,
) -> tuple[dict, dict]:
    """Engendre (ou reconduit) le bloc d'un fournisseur. Ne lève jamais.

    Rend `(bloc, rapport)`. Le rapport porte le nombre de modèles conservés, la
    raison d'un échec éventuel, et le fait que le bloc vient du rafraîchissement
    ou du catalogue précédent.
    """
    anciens = _previous_entries(previous_catalogue, name)
    rapport = {"provider": name, "refreshed": False, "kept": 0, "reason": ""}

    # Les capacités écrites à la main ne survivent à un rafraîchissement que
    # pour les fournisseurs qui n'en publient pas (Gemini).
    preserve_capabilities = not source.get("models_public")

    bloc: dict = {
        "kind": source["kind"],
        "base_url": source["base_url"],
        "keys_env": list(source.get("keys_env") or []),
        "quota": dict(source.get("quota") or {}),
        "reasoning_style": source.get("reasoning_style") or "",
        "reasoning_map": dict(source.get("reasoning_map") or {}),
        "headers": dict(source.get("headers") or {}),
        "models": [],
    }

    # Clé du fournisseur : le premier nom déclaré qui porte une valeur.
    cle = None
    for nom in bloc["keys_env"]:
        valeur = key_lookup(nom)
        if valeur:
            cle = valeur
            break

    if cle is None and not source.get("models_public"):
        return _reconduire(
            bloc,
            anciens,
            rapport,
            "aucune clé présente : bloc précédent conservé "
            f"(attendu : {', '.join(bloc['keys_env'])})",
            preserve_capabilities=preserve_capabilities,
        )

    try:
        adaptateur = providers.build_provider(
            providers.ProviderSpec.from_dict(name, {**bloc, "timeout": timeout})
        )
        bruts = adaptateur.list_models(api_key=cle, timeout=timeout)
    except providers.ProviderError as exc:
        return _reconduire(
            bloc,
            anciens,
            rapport,
            f"liste indisponible ({exc}) : bloc précédent conservé",
            preserve_capabilities=preserve_capabilities,
        )
    except Exception as exc:  # pragma: no cover - filet de sécurité réseau
        return _reconduire(
            bloc,
            anciens,
            rapport,
            f"échec inattendu ({exc}) : bloc précédent conservé",
            preserve_capabilities=preserve_capabilities,
        )

    normaliseur = _NORMALIZERS.get(name)
    vus: dict[str, dict] = {}
    for brut in bruts:
        if not isinstance(brut, dict):
            continue
        entree = normaliseur(brut) if normaliseur else None
        if not entree:
            continue
        ident = str(entree.get("id"))
        if source.get("free_only") and not entree.get("free"):
            continue
        vus[ident] = _merge_entry(
            entree, anciens.get(ident), preserve_capabilities=preserve_capabilities
        )

    if not vus and anciens:
        # Une liste qui se vide n'est pas un fait : c'est un incident côté
        # fournisseur. Vider le bloc rendrait la passerelle muette sans raison.
        return _reconduire(
            bloc,
            anciens,
            rapport,
            "liste vide renvoyée : bloc précédent conservé",
            preserve_capabilities=preserve_capabilities,
        )

    bloc["models"] = sorted(
        vus.values(), key=lambda e: (int(e.get("rank") or 1000), str(e.get("id")))
    )
    rapport["refreshed"] = True
    rapport["kept"] = len(bloc["models"])
    if source.get("free_only"):
        rapport["reason"] = f"{len(bruts)} publiés, {len(vus)} gratuits conservés"
    else:
        rapport["reason"] = f"{len(bruts)} publiés, {len(vus)} conservés"
    return bloc, rapport


def refresh_all(
    previous_catalogue: dict,
    *,
    only: list[str] | None = None,
    key_lookup=providers.env_value,
    timeout: int = 30,
) -> tuple[dict, list[dict]]:
    """Engendre le catalogue complet, fournisseur par fournisseur."""
    catalogue = cat_mod.empty_catalogue()
    catalogue["policy"] = _merge_policy(previous_catalogue)
    rapports: list[dict] = []
    for name, source in SOURCES.items():
        if only and name not in only:
            ancien = cat_mod.provider_spec(previous_catalogue or {}, name)
            if ancien:
                # Non rafraîchi mais présent avant : on le reconduit tel quel,
                # sinon demander un seul fournisseur effacerait les autres.
                bloc = dict(ancien)
                catalogue["providers"][name] = bloc
                rapports.append(
                    {
                        "provider": name,
                        "refreshed": False,
                        "kept": len(cat_mod.models(catalogue, name)),
                        "reason": "non demandé : bloc précédent conservé",
                        "stale": True,
                    }
                )
            continue
        bloc, rapport = refresh_provider(
            name, source, previous_catalogue, key_lookup=key_lookup, timeout=timeout
        )
        catalogue["providers"][name] = bloc
        rapports.append(rapport)
    return cat_mod.touch(catalogue, generator="scripts/ai/refresh_models.py"), rapports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Engendre config/ai_models.json depuis les API des fournisseurs."
    )
    parser.add_argument(
        "--provider",
        action="append",
        default=[],
        choices=sorted(SOURCES),
        help="Ne rafraîchir que ce fournisseur (option répétable).",
    )
    parser.add_argument("--output", help="Chemin du catalogue à écrire.")
    parser.add_argument("--timeout", type=int, default=30, help="Délai réseau, en secondes.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Montre le résultat sans écrire le fichier.",
    )
    args = parser.parse_args(argv)

    chemin = cat_mod.catalogue_path(args.output)
    ancien = cat_mod.load(args.output)
    catalogue, rapports = refresh_all(
        ancien, only=args.provider or None, timeout=args.timeout
    )

    for rapport in rapports:
        etat = "rafraîchi" if rapport.get("refreshed") else "reconduit"
        print(
            f"- {rapport['provider']:11s} {etat:10s} {rapport['kept']:3d} modèles  "
            f"{rapport.get('reason') or ''}".rstrip()
        )

    total = len(cat_mod.all_models(catalogue))
    print(f"\n{total} modèles au total — {chemin}")

    if args.dry_run:
        print("(--dry-run : rien n'a été écrit)")
        return 0

    if not cat_mod.save(catalogue, args.output):
        sys.stderr.write(f"Erreur : écriture impossible dans {chemin}\n")
        return 1

    # Un catalogue écrit mais vide de tout modèle utilisable n'est pas un succès :
    # le dire ici évite de le découvrir au premier appel, en session.
    if total == 0:
        sys.stderr.write(
            "Avertissement : aucun modèle au catalogue. Vérifiez les clés "
            f"({', '.join(sorted(SOURCES))}) et relancez.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
