#!/usr/bin/env python3
"""Adaptateurs de fournisseurs : OpenAI-compatibles, et Gemini natif (plan §4.1).

Deux familles suffisent à couvrir quatre fournisseurs :

- `OpenAICompatProvider` — OpenRouter, DeepSeek et xAI parlent tous le même
  dialecte (`POST /chat/completions`, en-tête `Authorization: Bearer`). Un seul
  client, des paramètres par fournisseur (`base_url`, en-têtes additionnels,
  nom du champ de raisonnement).
- `GeminiProvider` — Gemini a son propre protocole (`generateContent`,
  `usageMetadata`, `thinkingConfig`). Il **réutilise** le transport de
  [`scripts/ai_query.py`](../ai_query.py:1) plutôt que de le réécrire : ce code
  est couvert par 318 tests et par un banc dédié, une seconde implémentation du
  même protocole n'apporterait qu'une occasion de diverger.

Responsabilité qui n'est **pas** laissée à l'appelant : le masquage des secrets.
Chaque adaptateur appelle [`redact`](redact.py:1) sur le prompt et sur
l'instruction système, et rend le nombre de masquages dans le résultat. Un
appelant qui oublierait de masquer ne peut donc pas fuiter une clé — il ne
dispose d'aucun chemin qui contourne l'adaptateur.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from scripts import ai_query
from scripts.ai import catalogue as cat_mod, redact, schema

# ---------------------------------------------------------------------------
# Taxonomie d'erreurs — ce que la cascade a le droit de faire ensuite
# ---------------------------------------------------------------------------

# Statuts transitoires : retenter le MÊME modèle peut aboutir.
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# Statuts qui condamnent le modèle courant pour de bon (retiré, renommé).
RETIRED_MODEL_STATUSES = frozenset({404})

# Statuts qui condamnent la CLÉ (ou tous les modèles de ce fournisseur), pas la
# requête : 403 est exactement ce que la Phase 0 a observé 5 fois sur 88 appels
# réels, chaque fois en brûlant des modèles d'une cascade qui ne pouvait pas
# répondre. Un 401/403 sur OpenRouter ne dit rien de DeepSeek : on écarte le
# fournisseur, on continue ailleurs.
PROVIDER_FATAL_STATUSES = frozenset({401, 403})

# Statuts qui condamnent la REQUÊTE telle qu'elle a été écrite pour ce
# fournisseur — un paramètre inconnu, un contexte trop long — mais qu'un autre
# fournisseur peut accepter telle quelle. La version mono-Gemini abandonnait
# tout sur un 400 ; ici on essaie le suivant, sans retenter le même.
REQUEST_REJECT_STATUSES = frozenset({400, 413, 422})


class ProviderError(Exception):
    """Erreur d'appel, indépendante du fournisseur."""

    kind = "error"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        provider: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.provider = provider

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUSES

    @property
    def provider_fatal(self) -> bool:
        return self.status in PROVIDER_FATAL_STATUSES

    @property
    def model_retired(self) -> bool:
        return self.status in RETIRED_MODEL_STATUSES


class HTTPStatusError(ProviderError):
    kind = "http"


class NetworkError(ProviderError):
    """Panne réseau, DNS, TLS ou dépassement de délai : transitoire par nature."""

    kind = "network"

    @property
    def retryable(self) -> bool:
        return True


class ConfigError(ProviderError):
    """Configuration inutilisable : fournisseur sans clé, `base_url` absente."""

    kind = "config"

    @property
    def retryable(self) -> bool:
        return False


@dataclass
class Completion:
    """Réponse normalisée d'un fournisseur."""

    text: str
    model: str
    provider: str
    usage: dict = field(default_factory=dict)
    redactions: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass
class ProviderSpec:
    """Paramètres d'un fournisseur, tels que lus dans le catalogue."""

    name: str
    kind: str
    base_url: str = ""
    headers: dict = field(default_factory=dict)
    reasoning_style: str = ""
    reasoning_map: dict = field(default_factory=dict)
    timeout: int = 60

    @classmethod
    def from_dict(cls, name: str, data: dict | None) -> "ProviderSpec":
        data = data or {}
        try:
            timeout = int(data.get("timeout") or 60)
        except (TypeError, ValueError):
            timeout = 60
        headers = data.get("headers")
        reasoning_map = data.get("reasoning_map")
        return cls(
            name=name,
            kind=str(data.get("kind") or "").strip().lower(),
            base_url=str(data.get("base_url") or "").strip(),
            headers=dict(headers) if isinstance(headers, dict) else {},
            reasoning_style=str(data.get("reasoning_style") or "").strip().lower(),
            reasoning_map=dict(reasoning_map) if isinstance(reasoning_map, dict) else {},
            timeout=timeout,
        )


# ---------------------------------------------------------------------------
# Lecture des secrets : environnement, puis `.env` du dépôt
# ---------------------------------------------------------------------------


def repo_root() -> str:
    return cat_mod.repo_root()


def _dotenv_lookup(name: str, dotenv_path: str | None = None) -> str | None:
    """Cherche `name` dans le `.env` du dépôt, en repli de l'environnement.

    Les conteneurs reçoivent leurs variables par `docker compose`, qui lit
    `.env` nativement ; un lancement CLI direct sur l'hôte, lui, n'a rien
    exporté. Parseur minimal et tolérant, jamais bloquant.
    """
    if dotenv_path is None:
        dotenv_path = os.path.join(repo_root(), ".env")
    try:
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if not ligne or ligne.startswith("#"):
                    continue
                if ligne.startswith("export "):
                    ligne = ligne[7:].strip()
                cle, sep, valeur = ligne.partition("=")
                if not sep or cle.strip() != name:
                    continue
                valeur = valeur.strip()
                if len(valeur) >= 2 and valeur[0] == valeur[-1] and valeur[0] in "\"'":
                    valeur = valeur[1:-1].strip()
                return valeur or None
    except Exception:
        return None
    return None


def env_value(name: str) -> str | None:
    """Valeur d'une variable : environnement d'abord, `.env` ensuite."""
    if not name:
        return None
    valeur = (os.environ.get(name) or "").strip()
    if valeur:
        return valeur
    return _dotenv_lookup(name)


def _retry_after(headers) -> float | None:
    """`Retry-After` en secondes, quand le fournisseur le publie."""
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def _http_detail(exc: urllib.error.HTTPError) -> str:
    """Message d'erreur lisible, extrait du corps JSON quand il y en a un."""
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return str(exc)
    try:
        parsed = json.loads(body)
    except Exception:
        return body[:300] or str(exc)
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return error
        if parsed.get("message"):
            return str(parsed["message"])
    return body[:300] or str(exc)


def _openai_usage(data: dict) -> dict:
    """Jetons rapportés par un fournisseur OpenAI-compatible.

    Mesure, jamais estimation : c'est ce que le tableau de bord affiche. Un
    champ absent vaut 0 plutôt que `None`, pour qu'une somme sur une colonne ne
    dépende pas de la complétude de la réponse.
    """
    usage = data.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    if prompt_tokens is None:
        prompt_tokens = usage.get("input_tokens")
    output_tokens = usage.get("completion_tokens")
    if output_tokens is None:
        output_tokens = usage.get("output_tokens")
    prompt_tokens = int(prompt_tokens or 0)
    output_tokens = int(output_tokens or 0)
    total = int(usage.get("total_tokens") or (prompt_tokens + output_tokens))
    return {
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total,
    }


def _extract_openai_text(data: dict) -> str:
    """Texte d'une réponse OpenAI-compatible, y compris en contenu multi-parties."""
    choices = data.get("choices") or []
    if not choices:
        return ""
    premier = choices[0] or {}
    message = premier.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        morceaux = []
        for part in content:
            if isinstance(part, dict):
                morceaux.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                morceaux.append(part)
        return "".join(morceaux).strip()
    fallback = premier.get("text")
    return str(fallback).strip() if fallback else ""


# ---------------------------------------------------------------------------
# Adaptateur OpenAI-compatible
# ---------------------------------------------------------------------------


class OpenAICompatProvider:
    """OpenRouter, DeepSeek, xAI : un client, des paramètres par fournisseur."""

    def __init__(self, spec: ProviderSpec):
        self.spec = spec

    def endpoint(self, path: str) -> str:
        base = self.spec.base_url.rstrip("/")
        if not base:
            raise ConfigError(
                f"fournisseur '{self.spec.name}' sans base_url dans le catalogue",
                provider=self.spec.name,
            )
        return f"{base}{path}"

    # Graphie du champ de raisonnement, par style déclaré au catalogue. Un style
    # inconnu n'envoie rien : on n'invente pas un nom de champ.
    _REASONING_CHAMPS: dict[str, tuple[str, ...]] = {
        "effort": ("reasoning",),
        "reasoning_effort": ("reasoning_effort",),
    }

    def _reasoning_field(self, effort: str, supported: list[str] | None) -> dict:
        """Champ de raisonnement dans la graphie du fournisseur, ou rien.

        Deux graphies coexistent : `reasoning: {"effort": …}` (xAI) et
        `reasoning_effort: …` (OpenRouter, DeepSeek). La graphie déclarée par le
        fournisseur prime, mais elle est **filtrée par `supports`** : envoyer un
        champ non déclaré est un 400, donc un candidat gratuit perdu au premier
        essai de chaque délégation. Une autre graphie n'est employée que si le
        modèle la déclare nommément — `supports` absent ne suffit pas, ce serait
        deviner.
        """
        style = self.spec.reasoning_style
        champs = self._REASONING_CHAMPS.get(style)
        if champs and self._accepts(supported, *champs):
            return self._reasoning_payload(style, effort)
        declares = (
            {str(p) for p in supported}
            if isinstance(supported, (list, tuple, set))
            else set()
        )
        for autre, champs_autre in self._REASONING_CHAMPS.items():
            if autre != style and declares.intersection(champs_autre):
                return self._reasoning_payload(autre, effort)
        return {}

    @staticmethod
    def _reasoning_payload(style: str, effort: str) -> dict:
        if style == "reasoning_effort":
            return {"reasoning_effort": effort}
        return {"reasoning": {"effort": effort}}

    @staticmethod
    def _accepts(supported: list[str] | None, *champs: str) -> bool:
        """Le modèle accepte-t-il au moins un de ces champs ? Inconnu ⇒ oui.

        On n'ajoute jamais un champ que le catalogue ne déclare pas, mais on ne
        retire rien quand le fournisseur ne publie pas la liste : `supports` absent
        veut dire « non publié », pas « refuse tout ».
        """
        if not isinstance(supported, (list, tuple, set)):
            return True
        connus = {str(p) for p in supported}
        if not connus:
            return True
        return any(champ in connus for champ in champs)

    @staticmethod
    def _declares(supported: list[str] | None, *champs: str) -> bool:
        """Le catalogue déclare-t-il explicitement un de ces champs ? Inconnu ⇒ non.

        Miroir **strict** d'[`_accepts()`](scripts/ai/providers.py:343), et la
        dissymétrie est volontaire. Un schéma JSON strict n'est pas un drapeau
        répandu comme `response_format` : c'est un contrat complet, que beaucoup de
        modèles gratuits refusent. L'envoyer à un modèle dont les capacités ne sont
        pas publiées, c'est un 400 sur le **premier** candidat — le gratuit — donc
        un appel perdu à chaque délégation. Ici, « non publié » veut dire « je n'en
        sais rien », et on s'abstient.
        """
        if not isinstance(supported, (list, tuple, set)):
            return False
        connus = {str(p) for p in supported}
        return any(champ in connus for champ in champs)

    def build_payload(
        self,
        model: str,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        json_mode: bool = False,
        effort: str | None = None,
        supported_parameters: list[str] | None = None,
        json_schema: dict | None = None,
        schema_name: str = "reponse",
    ) -> dict:
        """Corps de requête, isolé pour être inspectable par le banc hors ligne.

        `supported_parameters` vient du catalogue, quand le fournisseur publie ce
        que le modèle accepte. Il ne sert qu'à **omettre** un champ non déclaré :
        `cohere/north-mini-code:free` ne publie pas `response_format`, et lui en
        envoyer un est un 400 garanti — donc un appel gratuit perdu au premier
        candidat de chaque délégation, puisque le gratuit passe en premier.

        `json_schema` est le contrat fort : quand le modèle **déclare**
        `structured_outputs`, il part en `response_format: json_schema` strict et le
        fournisseur contraint réellement la génération. Sinon on retombe sur
        `json_object`, qui demande du JSON valide sans dire lequel — c'est le
        premier étage de la garantie du plan (§6, mécanisme 3), la relecture locale
        étant le second.
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_schema and self._declares(supported_parameters, "structured_outputs"):
            # Le contrat fort d'abord : s'il est déclaré, il remplace le drapeau
            # faible, il ne s'y ajoute pas (deux `response_format` n'en font pas un).
            payload["response_format"] = schema.pour_openai(schema_name, json_schema)
        elif json_mode and self._accepts(
            supported_parameters, "response_format", "structured_outputs"
        ):
            payload["response_format"] = {"type": "json_object"}
        if effort:
            # Le nom du champ de raisonnement n'est PAS supposé : il est déclaré
            # par fournisseur dans le catalogue, et relevé au moment de
            # l'intégration (plan §11 point 5). Il est en plus filtré par les
            # capacités publiées du modèle, comme `response_format`.
            payload.update(self._reasoning_field(effort, supported_parameters))
        return payload

    def _headers(self, api_key: str | None) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(self.spec.headers)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _open(self, request: urllib.request.Request, timeout: int):
        return urllib.request.urlopen(request, timeout=timeout)

    def chat(
        self,
        model: str,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        json_mode: bool = False,
        effort: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
        supported_parameters: list[str] | None = None,
        json_schema: dict | None = None,
        schema_name: str = "reponse",
    ) -> Completion:
        prompt_propre, rapport_prompt = redact.redact_report(prompt)
        systeme_propre, rapport_systeme = redact.redact_report(system)
        payload = self.build_payload(
            model=model,
            prompt=prompt_propre,
            system=systeme_propre,
            temperature=temperature,
            json_mode=json_mode,
            effort=effort,
            supported_parameters=supported_parameters,
            json_schema=json_schema,
            schema_name=schema_name,
        )
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint("/chat/completions"),
            data=data,
            headers=self._headers(api_key),
            method="POST",
        )
        delai = timeout or self.spec.timeout
        try:
            with self._open(request, delai) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise HTTPStatusError(
                f"{self.spec.name} HTTP {exc.code} : {_http_detail(exc)}",
                status=exc.code,
                retry_after=_retry_after(exc.headers),
                provider=self.spec.name,
            ) from exc
        except urllib.error.URLError as exc:
            raise NetworkError(
                f"réseau {self.spec.name} : {exc.reason}", provider=self.spec.name
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise NetworkError(
                f"réseau {self.spec.name} : {exc}", provider=self.spec.name
            ) from exc

        try:
            parsed = json.loads(body)
        except Exception as exc:
            raise NetworkError(
                f"{self.spec.name} : réponse illisible ({str(exc)})",
                provider=self.spec.name,
            ) from exc

        return Completion(
            text=_extract_openai_text(parsed),
            model=model,
            provider=self.spec.name,
            usage=_openai_usage(parsed),
            redactions=redact.merge(rapport_prompt, rapport_systeme),
            raw=parsed,
        )

    def list_models(self, api_key: str | None = None, timeout: int = 30) -> list[dict]:
        """Liste des modèles publiée par le fournisseur (sert au rafraîchissement)."""
        request = urllib.request.Request(
            self.endpoint("/models"),
            headers=self._headers(api_key),
            method="GET",
        )
        try:
            with self._open(request, timeout) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise HTTPStatusError(
                f"{self.spec.name} HTTP {exc.code} : {_http_detail(exc)}",
                status=exc.code,
                retry_after=_retry_after(exc.headers),
                provider=self.spec.name,
            ) from exc
        except urllib.error.URLError as exc:
            raise NetworkError(
                f"réseau {self.spec.name} : {exc.reason}", provider=self.spec.name
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise NetworkError(
                f"réseau {self.spec.name} : {exc}", provider=self.spec.name
            ) from exc

        data = parsed.get("data")
        if isinstance(data, list):
            return [entry for entry in data if isinstance(entry, dict)]
        models = parsed.get("models")
        if isinstance(models, list):
            return [entry for entry in models if isinstance(entry, dict)]
        return []


# ---------------------------------------------------------------------------
# Adaptateur Gemini
# ---------------------------------------------------------------------------


class GeminiProvider:
    """Gemini natif, porté par le transport déjà testé d'`ai_query`.

    `api_key` explicitement fourni court-circuite le gateway local d'`ai_query` :
    c'est le comportement voulu ici, puisque le broker fait lui-même sa rotation
    de clés et ne doit pas être redirigé vers un proxy qui poserait la sienne.
    """

    def __init__(self, spec: ProviderSpec):
        self.spec = spec or ProviderSpec(name="gemini", kind="gemini")

    # Vocabulaire du courtier (low/medium/high) → forme native de Gemini. Le
    # catalogue déclare la forme ET les correspondances : un Gemini 3 utilise un
    # niveau symbolique (`thinkingLevel`), un 2.5 un budget en jetons
    # (`thinkingBudget`). Un cran absent de la table (`medium` sur l'échelle
    # binaire) n'envoie rien plutôt que d'inventer une valeur intermédiaire.
    _THINKING_FIELDS: dict[str, tuple[str, str]] = {
        "thinking_level": ("thinkingLevel", "str"),
        "thinking_budget": ("thinkingBudget", "int"),
    }

    def _thinking_config(
        self, effort: str | None, supported: list[str] | None
    ) -> dict | None:
        """`thinkingConfig` du modèle, ou `None` s'il n'en accepte pas.

        La lecture est l'inverse de `_accepts` : ici, l'absence de déclaration
        veut dire « ne pense pas ». Le catalogue Gemini contient des modèles qui
        ne raisonnent pas du tout (Gemma) ; leur envoyer un `thinkingConfig` est
        un 400 sur le premier candidat gratuit de chaque délégation. C'est
        pourquoi `thinking` doit être présent dans la liste **écrite à la main**
        du catalogue, et non simplement absente de `supports`.
        """
        if not effort:
            return None
        declares = (
            {str(p) for p in supported}
            if isinstance(supported, (list, tuple, set))
            else set()
        )
        if "thinking" not in declares:
            return None
        champ_type = self._THINKING_FIELDS.get(self.spec.reasoning_style)
        if not champ_type:
            return None
        champ, genre = champ_type
        valeur = self.spec.reasoning_map.get(str(effort).strip().lower())
        if valeur is None:
            return None
        if genre == "int":
            try:
                return {champ: int(valeur)}
            except (TypeError, ValueError):
                return None
        return {champ: str(valeur)}

    def chat(
        self,
        model: str,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        json_mode: bool = False,
        effort: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
        supported_parameters: list[str] | None = None,
        json_schema: dict | None = None,
        schema_name: str = "reponse",
    ) -> Completion:
        # `schema_name` n'a pas d'équivalent chez Gemini : le proto ne nomme pas les
        # schémas. Le paramètre existe pour que les deux adaptateurs aient la même
        # signature, et il est donc ignoré ici en connaissance de cause.
        #
        # `supported_parameters` ne porte pas, pour Gemini, la liste publiée par
        # l'API (il n'y en a pas) mais la liste **écrite à la main** au catalogue :
        # elle dit quels modèles peuvent penser. Elle n'est donc pas ignorée.
        thinking = self._thinking_config(effort, supported_parameters)
        # Le schéma part déjà traduit pour le proto Gemini (types en majuscules,
        # champs qu'il refuse ôtés). Le transport posera `responseMimeType` en le
        # recevant : Gemini refuse `responseSchema` sans lui, et ce refus est un 400.
        reponse_schema = schema.pour_gemini(json_schema) if json_schema else None
        prompt_propre, rapport_prompt = redact.redact_report(prompt)
        systeme_propre, rapport_systeme = redact.redact_report(system)
        delai = timeout or self.spec.timeout
        try:
            # La traduction de l'effort en forme native est faite ici ; le
            # transport, lui, pose `thinkingConfig` tel quel sans connaître notre
            # vocabulaire (voir `ai_query.query_gemini`).
            text, usage = ai_query.query_gemini(
                prompt=prompt_propre,
                system_instruction=systeme_propre,
                model=model,
                api_key=api_key,
                temperature=temperature,
                timeout=delai,
                # Un schéma implique le mode JSON : `json_mode` le dit au transport,
                # qui n'a pas à déduire l'un de l'autre.
                json_mode=json_mode or bool(reponse_schema),
                thinking_config=thinking,
                response_schema=reponse_schema,
            )
        except ai_query.GeminiHTTPError as exc:
            raise HTTPStatusError(
                f"gemini HTTP {exc.status} : {exc.detail}",
                status=exc.status,
                provider="gemini",
            ) from exc
        except RuntimeError as exc:
            raise NetworkError(f"gemini : {exc}", provider="gemini") from exc

        return Completion(
            text=text,
            model=model,
            provider="gemini",
            usage=usage,
            redactions=redact.merge(rapport_prompt, rapport_systeme),
        )

    def list_models(self, api_key: str | None = None, timeout: int = 30) -> list[dict]:
        """Liste des modèles Gemini (v1beta `models`, pagination ignorée).

        Pas de clé locale : l'appel part quand même, exactement comme
        `query_gemini` le fait, pour rester utilisable derrière un proxy qui
        injecte l'authentification au niveau transport.
        """
        base = self.spec.base_url.rstrip("/") or (
            "https://generativelanguage.googleapis.com/v1beta"
        )
        url = f"{base}/models?pageSize=200"
        if api_key:
            url += f"&key={api_key}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise HTTPStatusError(
                f"gemini HTTP {exc.code} : {_http_detail(exc)}",
                status=exc.code,
                provider="gemini",
            ) from exc
        except urllib.error.URLError as exc:
            raise NetworkError(f"réseau gemini : {exc.reason}", provider="gemini") from exc
        except (TimeoutError, OSError) as exc:
            raise NetworkError(f"réseau gemini : {exc}", provider="gemini") from exc

        models = parsed.get("models")
        return [entry for entry in models if isinstance(entry, dict)] if isinstance(models, list) else []


# ---------------------------------------------------------------------------
# Fabrique
# ---------------------------------------------------------------------------

_PROVIDER_CLASSES = {
    "openai": OpenAICompatProvider,
    "gemini": GeminiProvider,
}


def build_provider(spec: ProviderSpec):
    """Instancie l'adaptateur correspondant au `kind` déclaré au catalogue."""
    classe = _PROVIDER_CLASSES.get(spec.kind)
    if classe is None:
        raise ConfigError(
            f"fournisseur '{spec.name}' : kind inconnu '{spec.kind or '(vide)'}' "
            f"(attendu : {', '.join(sorted(_PROVIDER_CLASSES))})",
            provider=spec.name,
        )
    return classe(spec)


def build_from_catalogue(catalogue: dict, provider: str):
    """Construit l'adaptateur d'un fournisseur décrit par le catalogue."""
    spec = ProviderSpec.from_dict(provider, cat_mod.provider_spec(catalogue, provider))
    return build_provider(spec)
