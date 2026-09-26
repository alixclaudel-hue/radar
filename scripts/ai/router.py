#!/usr/bin/env python3
"""Routeur — ordre des candidats d'un appel, et raison de chaque écart.

Policy appliquée (plan §4.2), dans cet ordre strict :

1. **gratuit d'abord.** Un modèle gratuit dont le quota reste disponible passe
   avant tout modèle payant. L'ordre de **déclaration des fournisseurs dans le
   catalogue** est l'ordre de préférence (OpenRouter avant Gemini) : l'opérateur
   peut le changer sans toucher au code.
2. **payant ensuite**, et seulement si deux conditions *indépendantes* sont
   réunies : le modèle a été validé (`paid_allowlist` du catalogue, ou
   `"paid_ok": true` sur son entrée) **et** [`quota.paid_allowed()`](quota.py:322)
   autorise la dépense pour ce mode.
3. **jamais de payant silencieux.** Un refus n'écarte pas le modèle en le
   taisant : il l'écarte *avec sa raison*, que [`explain()`](router.py:1) publie
   et que le reçu conserve.
4. **jamais Claude.** Ce module ne connaît aucun fournisseur Claude, et le
   catalogue engendré non plus : la règle est structurelle, pas déclarative.

Deux filtres s'ajoutent, tous deux dictés par la base de mesure :

- un modèle **en cooldown** ou **retiré** ([`health`](health.py:1)) n'est plus
  proposé. C'est ce qui évite de brûler la queue d'une cascade sur un modèle
  mort — gaspillage mesuré 12 fois sur 88 appels réels ;
- un fournisseur dont **aucune clé n'est utilisable** (absente, rejetée 401/403,
  quota épuisé) ne propose aucun de ses modèles. C'est le correctif du 403 qui
  coûtait jusqu'à six appels pour rien.

Le module ne fait **aucun appel réseau** : il décide, il ne parle pas. C'est ce
qui permet de le tester hors ligne, y compris sur ses cas adversariaux.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scripts.ai import catalogue as cat_mod
from scripts.ai import health, providers, quota

__all__ = [
    "Candidate",
    "estimate_cost",
    "explain",
    "format_explain",
    "order_candidates",
]

# Facteur d'estimation des jetons de sortie : on suppose une sortie au moins
# aussi longue que l'entrée. Surestimer est le sens sûr — un appel payant refusé
# à tort ne coûte rien, un appel payant accepté à tort débite un plafond que
# l'opérateur croyait intact.
_OUTPUT_TOKEN_FLOOR = 512


@dataclass
class Candidate:
    """Un appel possible : fournisseur, modèle, clé, coût estimé, et sa raison."""

    provider: str
    model: str
    free: bool
    tier: str = ""
    key_env: str | None = None
    spec: providers.ProviderSpec | None = None
    entry: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    reason: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "free": self.free,
            "tier": self.tier,
            "key_env": self.key_env,
            "cost_usd": self.cost_usd,
            "reason": self.reason,
        }


def estimate_cost(entry: dict, prompt_tokens: int) -> float:
    """Coût a priori d'un appel, en USD, d'après le prix du catalogue.

    Un modèle dont le prix n'est pas publié rend `0.0` : l'appelant doit alors
    **refuser** l'appel payant, jamais le croire gratuit (voir
    [`_evaluate()`](router.py:1), qui traite ce cas comme un refus explicite).
    """
    entree = max(0, int(prompt_tokens or 0))
    sortie = max(_OUTPUT_TOKEN_FLOOR, entree)
    return cat_mod.cost_estimate(entry, entree, sortie)


def _declared_tier(entry: dict) -> str:
    return str(entry.get("tier") or "").strip().lower()


def _tier_rank(entry: dict, tier: str | None) -> int:
    """0 = tier demandé, 1 = tier non déclaré (donc ouvert), 2 = autre tier."""
    declare = _declared_tier(entry)
    if not tier or not declare:
        return 1 if declare else 0
    return 0 if declare == str(tier).strip().lower() else 2


def _context_limit(entry: dict) -> int | None:
    """Fenêtre de contexte déclarée, tous dialectes confondus."""
    haut = entry.get("top_provider")
    candidats = [
        entry.get("context_length"),
        haut.get("context_length") if isinstance(haut, dict) else None,
        entry.get("inputTokenLimit"),
    ]
    for brut in candidats:
        if brut is None:
            continue
        try:
            valeur = int(brut)
        except (TypeError, ValueError):
            continue
        if valeur > 0:
            return valeur
    return None


def _rank(entry: dict) -> int:
    """Rang de préférence déclaré dans le catalogue ; neutre par défaut."""
    try:
        return int(entry.get("rank") or 1000)
    except (TypeError, ValueError):
        return 1000


def _evaluate(
    catalogue: dict,
    *,
    mode: str,
    tier: str | None,
    prompt_tokens: int,
    allow_paid: bool,
    escalate: bool,
    quota_state: dict,
    health_state: dict,
    key_lookup,
    now: float | None,
    explicit_model: str | None,
) -> list[tuple[Candidate | None, dict]]:
    """Évalue **toutes** les entrées du catalogue, retenues ou écartées.

    Rend une paire par modèle : le candidat (ou `None`) et l'information
    d'explication. Séparer les deux permet à `--list-candidates` de montrer les
    modèles écartés *et pourquoi* — un modèle qui disparaît en silence est un
    défaut de diagnostic, pas une discrétion.
    """
    policy = cat_mod.policy(catalogue)
    fournisseurs = list((catalogue.get("providers") or {}).keys())
    rang_fournisseur = {nom: i for i, nom in enumerate(fournisseurs)}
    payants_valides = {str(m) for m in (policy.get("paid_allowlist") or [])}
    modes_payants = {str(m) for m in (policy.get("paid_modes") or [])}
    attendu = int(prompt_tokens or 0)

    resultats: list[tuple[Candidate | None, dict]] = []

    for nom in fournisseurs:
        brut = cat_mod.provider_spec(catalogue, nom)
        info_fournisseur = {
            "provider": nom,
            "provider_rank": rang_fournisseur.get(nom, 1000),
        }

        # --- Le fournisseur est-il utilisable du tout ? ---
        spec = providers.ProviderSpec.from_dict(nom, brut)
        try:
            providers.build_provider(spec)
        except providers.ConfigError as exc:
            raison_fournisseur = str(exc)
        else:
            raison_fournisseur = ""

        sans_auth = str(brut.get("auth") or "").strip().lower() == "none"
        cle: str | None = None
        cle_ok = False
        raison_cle = raison_fournisseur

        if not raison_fournisseur:
            if sans_auth:
                cle_ok, raison_cle = True, "authentification portée par le transport"
            else:
                q = cat_mod.quota_spec(catalogue, nom)
                noms = cat_mod.key_env_names(catalogue, nom)
                presentes = [n for n in noms if key_lookup(n)]
                if not presentes:
                    raison_cle = f"aucune clé présente (attendu : {', '.join(noms[:3])}…)"
                else:
                    choisie = quota.pick_key(
                        quota_state, nom, presentes, q["window"], q["limit"], now
                    )
                    if choisie is None:
                        quota_txt = (
                            f"quota {q['window']} atteint ({q['limit']})"
                            if q["limit"]
                            else "rejetée (401/403)"
                        )
                        raison_cle = f"aucune clé utilisable : {quota_txt}"
                    else:
                        cle, cle_ok, raison_cle = choisie, True, ""

        for entry in cat_mod.models(catalogue, nom):
            modele = str(entry.get("id"))
            gratuit = cat_mod.is_free(entry)
            info = {
                "provider": nom,
                "provider_rank": info_fournisseur["provider_rank"],
                "model": modele,
                "free": gratuit,
                "tier": _declared_tier(entry),
                "key_env": cle,
                "included": False,
                "reason": "",
                "cost_usd": 0.0,
            }

            if not gratuit:
                info["cost_usd"] = estimate_cost(entry, attendu)

            # Un modèle explicite demandé court-circuite tout le reste : les
            # autres entrées sont hors périmètre, et le dire vaut mieux que de
            # les taire.
            if explicit_model and modele != explicit_model:
                info["reason"] = f"hors périmètre : modèle explicite « {explicit_model} »"
                resultats.append((None, info))
                continue

            if raison_fournisseur:
                info["reason"] = raison_fournisseur
            elif not cle_ok:
                info["reason"] = raison_cle
            else:
                info["reason"] = _model_reason(
                    entry=entry,
                    prompt_tokens=attendu,
                    free=gratuit,
                    mode=mode,
                    valid_paid=modele in payants_valides or entry.get("paid_ok") is True,
                    paid_modes=modes_payants,
                    health_state=health_state,
                    quota_state=quota_state,
                    policy=policy,
                    allow_paid=allow_paid,
                    escalate=escalate,
                    now=now,
                    provider=nom,
                    model=modele,
                )

            if info["reason"]:
                resultats.append((None, info))
                continue

            info["included"] = True
            info["reason"] = (
                "gratuit retenu"
                if gratuit
                else (
                    "payant autorisé (mode déclaré payant)"
                    if mode in modes_payants
                    else "payant autorisé (autorisation explicite)"
                )
            )
            resultats.append(
                (
                    Candidate(
                        provider=nom,
                        model=modele,
                        free=gratuit,
                        tier=_declared_tier(entry),
                        key_env=cle,
                        spec=spec,
                        entry=entry,
                        cost_usd=info["cost_usd"],
                        reason=info["reason"],
                    ),
                    info,
                )
            )

    return resultats


def _model_reason(
    *,
    entry: dict,
    prompt_tokens: int,
    free: bool,
    mode: str,
    valid_paid: bool,
    paid_modes: set[str],
    health_state: dict,
    quota_state: dict,
    policy: dict,
    allow_paid: bool,
    escalate: bool,
    now: float | None,
    provider: str,
    model: str,
) -> str:
    """Raison d'écarter ce modèle, ou chaîne vide s'il est retenu."""
    motif = health.reason(health_state, provider, model, now)
    if motif:
        return motif

    limite = _context_limit(entry)
    if limite is not None and prompt_tokens and prompt_tokens > limite:
        return f"contexte insuffisant ({prompt_tokens} jetons pour {limite} déclarés)"

    if free:
        return ""

    if not valid_paid:
        return "payant non validé (absent de paid_allowlist et sans « paid_ok »)"

    cout = estimate_cost(entry, prompt_tokens)
    if cout <= 0.0:
        # Cas dangereux : un modèle payant sans prix publié serait chiffré à
        # zéro et pourrait passer pour gratuit. On refuse tant qu'on ne sait pas.
        return "payant au prix inconnu : impossible de chiffrer l'appel"

    autorise, pourquoi = quota.paid_allowed(
        quota_state,
        policy,
        mode=mode,
        allow_paid=allow_paid,
        escalate=escalate,
        estimated_cost=cout,
        now=now,
    )
    if not autorise:
        return pourquoi
    return ""


def order_candidates(
    catalogue: dict,
    *,
    mode: str,
    tier: str | None = None,
    prompt_tokens: int = 0,
    explicit_model: str | None = None,
    provider: str | None = None,
    allow_paid: bool = False,
    escalate: bool = False,
    quota_state: dict | None = None,
    health_state: dict | None = None,
    key_lookup=None,
    now: float | None = None,
    max_candidates: int | None = None,
) -> list[Candidate]:
    """Candidats retenus, dans l'ordre où il faut les essayer.

    `provider` restreint la liste à un fournisseur. Le filtre est appliqué **avant**
    le plafond `max_candidates` : tronquer d'abord ferait disparaître un
    fournisseur dont les entrées arrivent après le plafond, et l'appel rendrait
    « aucun candidat utilisable » alors que le modèle existe (défaut constaté en
    production le 26/09 : `--provider deepseek` face à 23 modèles gratuits).

    `key_lookup` est injectable pour que le banc reste hors ligne : il reçoit un
    **nom de variable** et rend sa valeur, ou `None`. Le routeur ne manipule donc
    jamais un secret, seulement des noms.
    """
    if key_lookup is None:
        key_lookup = providers.env_value
    if quota_state is None:
        quota_state = quota.load_state()
    if health_state is None:
        health_state = health.load()

    policy = cat_mod.policy(catalogue)
    try:
        plafond_candidats = int(
            max_candidates if max_candidates is not None else policy.get("max_candidates")
        )
    except (TypeError, ValueError):
        plafond_candidats = 6
    if plafond_candidats <= 0:
        plafond_candidats = 6

    paires = _evaluate(
        catalogue,
        mode=mode,
        tier=tier,
        prompt_tokens=prompt_tokens,
        allow_paid=allow_paid,
        escalate=escalate,
        quota_state=quota_state,
        health_state=health_state,
        key_lookup=key_lookup,
        now=now,
        explicit_model=explicit_model,
    )
    retenus = [(c, i) for c, i in paires if c is not None]
    if provider:
        # Filtre **avant** la troncature. `_evaluate` laisse volontairement les
        # autres fournisseurs dans `paires` : `explain()` doit continuer de tout
        # montrer, un modèle qui disparaît en silence étant un défaut de
        # diagnostic — mais la liste des candidats, elle, se restreint ici.
        retenus = [(c, i) for c, i in retenus if c.provider == provider]

    def cle(item: tuple[Candidate, dict]):
        candidat, info = item
        commun = (
            info.get("provider_rank", 1000),
            _tier_rank(candidat.entry, tier),
            _rank(candidat.entry),
            candidat.model,
        )
        # Gratuit d'abord ; à gratuité égale, le moins cher d'abord — c'est ce
        # qui fait choisir DeepSeek plutôt que xAI quand les deux sont validés.
        return (0, 0.0) + commun if candidat.free else (1, candidat.cost_usd) + commun

    retenus.sort(key=cle)
    return [candidat for candidat, _ in retenus[:plafond_candidats]]


def explain(
    catalogue: dict,
    *,
    mode: str,
    tier: str | None = None,
    prompt_tokens: int = 0,
    explicit_model: str | None = None,
    allow_paid: bool = False,
    escalate: bool = False,
    quota_state: dict | None = None,
    health_state: dict | None = None,
    key_lookup=None,
    now: float | None = None,
) -> list[dict]:
    """Table d'explication de **toutes** les entrées du catalogue.

    Sert à `--list-candidates` : on y lit ce qui a été retenu, ce qui a été
    écarté, et le motif exact de chaque écart.
    """
    if key_lookup is None:
        key_lookup = providers.env_value
    if quota_state is None:
        quota_state = quota.load_state()
    if health_state is None:
        health_state = health.load()

    paires = _evaluate(
        catalogue,
        mode=mode,
        tier=tier,
        prompt_tokens=prompt_tokens,
        allow_paid=allow_paid,
        escalate=escalate,
        quota_state=quota_state,
        health_state=health_state,
        key_lookup=key_lookup,
        now=now,
        explicit_model=explicit_model,
    )
    return [info for _, info in paires]


def format_explain(infos: list[dict]) -> str:
    """Rendu texte de [`explain()`](router.py:1), pour la console et les reçus."""
    lignes: list[str] = []
    for info in infos:
        marque = "inclus" if info.get("included") else "écarté"
        if info.get("free"):
            prix = "gratuit"
        else:
            prix = f"{float(info.get('cost_usd') or 0.0):.6f} $"
        motif = str(info.get("reason") or "")
        lignes.append(
            f"[{marque}] {info.get('provider')}:{info.get('model')} ({prix}) {motif}".rstrip()
        )
    return "\n".join(lignes)
