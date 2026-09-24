#!/usr/bin/env python3
"""Passerelle polyvalente vers l'API Gemini (Google AI Studio) pour Radar.

Rôle : délester les tâches mécaniques (logs, diffs, premiers jets de code ou
de tests) pour économiser les tokens Claude — cf. `.claude/skills/ask-gemini/`.

- Deux cascades de modèles (`TIER_CASCADES`) : `heavy` pour le raisonnement
  (code, tests), `fast` pour le volume (logs, diffs, docs).
- Repli automatique sur le modèle suivant de la cascade quand le modèle
  courant est inutilisable (quota épuisé, modèle retiré, surcharge).
- Prompts système spécialisés par `--mode` (code, test, diag, summary, pr).
- Extraction de code pur (`--raw-code`) et garde-fou syntaxique
  (`--check-syntax`) pour ne jamais écrire un fichier Python cassé.
- Zéro dépendance externe (urllib/json de la bibliothèque standard).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

_DEFAULT_BASE_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


_GATEWAY_MARKER = "/tmp/radar-gemini-gateway.url"


def _read_gateway_marker() -> str:
    """URL du gateway écrite par `scripts/cloud-gemini-gateway.sh` au SessionStart.

    Repli quand `RADAR_GEMINI_BASE_URL` n'est pas exportée dans l'environnement
    du process appelant (le hook shell ne peut pas exporter dans le shell parent).
    Fichier éphémère volontairement placé sous `/tmp` — jamais commité, disparaît
    à la fin de la session. Absent sur le VPS : chemin direct inchangé.
    """
    try:
        with open(_GATEWAY_MARKER, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _base_url_template() -> str:
    """URL cible pour l'appel Gemini, avec override par `RADAR_GEMINI_BASE_URL`.

    En session cloud (dépôt frais, sans clé Gemini exportée), un mini gateway
    local — `scripts/gemini_gateway.py` — reçoit `RADAR_GEMINI_BASE_URL` sous la
    forme `http://127.0.0.1:8632/v1beta/models/` et ajoute lui-même `?key=`.
    Sur le VPS et en Docker, la variable est absente : appel direct à Google
    inchangé.
    """
    override = (os.environ.get("RADAR_GEMINI_BASE_URL") or "").strip()
    if not override:
        override = _read_gateway_marker()
    if not override:
        return _DEFAULT_BASE_URL_TEMPLATE
    return override.rstrip("/") + "/{model}:generateContent"


BASE_URL_TEMPLATE = _DEFAULT_BASE_URL_TEMPLATE  # rétrocompat pour tests qui l'importaient

# Cascades ordonnées par capacité décroissante. Les noms sont ceux renvoyés par
# ListModels (v1beta) — un modèle absent de cette liste répond 404, pas une
# erreur silencieuse : vérifier avant d'en ajouter un.
TIER_CASCADES = {
    # Raisonnement : quota journalier étroit, réservé au code et aux tests.
    "heavy": [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.1-pro-preview",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    ],
    # Volume : quota large, pour tout ce qui est lecture/reformulation.
    "fast": [
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
    ],
}

DEFAULT_MODEL = TIER_CASCADES["heavy"][0]

# Statuts qui condamnent le modèle courant mais pas la requête.
FALLBACK_STATUSES = frozenset({404, 429})

# Prompts système adaptés aux conventions Radar (cf. CLAUDE.md).
SYSTEM_PROMPTS = {
    "general": "",
    "code": (
        "Tu es un agent expert en développement Python pour l'application vinyle Radar "
        "(FastAPI + SQLite, bibliothèque standard privilégiée). "
        "Génère uniquement du code Python propre, typé et prêt pour la production. "
        "Pas de bavardage introductif ni de conclusion. "
        "Respecte les conventions du projet : commentaires en français et seulement "
        "quand ils expliquent le POURQUOI, aucun appel réseau non sollicité, "
        "aucune dépendance externe ajoutée sans nécessité."
    ),
    "test": (
        "Tu es un ingénieur QA spécialisé en tests Python pour le projet Radar. "
        "Génère une suite de tests avec le module `unittest` de la bibliothèque standard. "
        "Couvre le cas nominal, les cas limites (None, chaîne vide, liste vide, zéro) "
        "et les cas d'erreur. Mocke systématiquement tout appel réseau, tout accès "
        "disque et toute horloge : la suite doit passer hors ligne, sans secret ni "
        "service externe. Nomme les tests en français. "
        "Fournis du code exécutable directement, sans texte autour."
    ),
    "diag": (
        "Tu es un ingénieur SRE qui analyse un incident sur Radar "
        "(Docker, SQLite, jobs de fond, API Discogs/YouTube). "
        "Analyse le journal ou la trace fournie et réponds sous ce format concis :\n"
        "1. ORIGINE : fichier, fonction et ligne exacte de l'erreur.\n"
        "2. CAUSE RACINE : pourquoi l'erreur survient (contrainte BDD, quota, "
        "donnée inattendue, condition de course...).\n"
        "3. PISTE DE FIX : proposition de correction en 3 lignes maximum.\n"
        "Si la trace ne suffit pas à trancher, dis-le explicitement au lieu "
        "d'inventer une cause plausible."
    ),
    "read": (
        "Tu es un agent expert en analyse documentaire pour le projet Radar. "
        "Lis le document fourni et produis une analyse structurée en JSON valide "
        "avec exactement ces clés : "
        "'title' (sujet ou titre principal du document), "
        "'sections' (liste d'objets {'heading', 'summary'} pour chaque section majeure), "
        "'key_points' (liste de 5 à 10 points clés les plus importants), "
        "'todos' (liste des actions en attente, liste vide si aucune), "
        "'dependencies' (fichiers, modules ou systèmes externes mentionnés). "
        "Renvoie uniquement le JSON brut, sans blocs markdown ni texte autour."
    ),
    "summary": (
        "Tu es un compresseur de journaux pour terminal. "
        "Résume les données fournies en 5 puces maximum. "
        "Supprime le bruit normal, isole uniquement les anomalies, les requêtes "
        "lentes et les compteurs clés. Cite les valeurs chiffrées telles quelles, "
        "sans les arrondir ni les extrapoler."
    ),
    "pr": (
        "Tu es l'agent de release git du projet Radar. "
        "À partir du `git diff` fourni en entrée, génère exactement ces 4 sections, "
        "sans fioriture et intégralement en français :\n\n"
        "TITRE COMMIT :\n"
        "<phrase à l'impératif, première lettre en majuscule, sans point final, "
        "72 caractères maximum ; un préfixe « Domaine : » est admis quand il "
        "clarifie le périmètre. N'utilise PAS le format conventional-commits "
        "(pas de `feat:` ni `fix(scope):`) : ce n'est pas la convention du dépôt>\n\n"
        "MESSAGE COMMIT :\n"
        "<2 à 3 phrases expliquant le POURQUOI de la modification, jamais le QUOI "
        "(le diff le dit déjà)>\n\n"
        "CORPS DE PR :\n"
        "## Contexte\n<1 phrase sur le problème résolu>\n\n"
        "## Changements\n<puces synthétiques : fichier touché et rôle du changement>\n\n"
        "## Tests & Validation\n<tests réellement visibles dans le diff ; "
        "n'invente aucune validation que le diff ne prouve pas>\n\n"
        "MAJ CLAUDE.md :\n"
        "- <une seule puce concise, prête à insérer dans le résumé d'état ou le TODO>"
    ),
}

# Modes dont la sortie attendue est du code Python : l'extraction des blocs
# markdown y est systématique, sinon le fichier écrit par `-o` serait inutilisable.
CODE_MODES = frozenset({"code", "test"})


def _auth_hint(status: int, message: str, key: str | None) -> str:
    """Indice à accoler au message d'erreur quand l'authentification est en cause.

    En session cloud `key` est toujours `None` (la clé vit dans le proxy) : cadrer
    sur ce seul critère afficherait l'indice sur n'importe quel 400 — requête trop
    grosse, paramètre invalide — et enverrait vers un faux problème de clé, alors
    que CLAUDE.md prescrit d'abandonner la délégation dans ce cas. On exige donc
    un statut ou un message qui parle vraiment d'authentification.
    """
    if key:
        return ""
    message_auth = any(
        marque in message.lower()
        for marque in ("api key", "unauthenticated", "permission denied", "credential")
    )
    if status not in (401, 403) and not message_auth:
        return ""
    return (
        " (aucune clé locale : vérifie GEMINI_API_KEY, ou l'identifiant "
        "réseau configuré pour generativelanguage.googleapis.com)"
    )


def is_fallback_status(status: int) -> bool:
    """Le modèle suivant de la cascade a-t-il une chance d'aboutir ?

    404 (modèle retiré ou renommé) et 429 (quota épuisé) condamnent ce
    modèle-là, pas la requête. Les 5xx sont des aléas côté serveur, souvent
    propres à un modèle surchargé : un 502 a été observé sur la cascade heavy
    alors que la cascade fast répondait normalement, d'où la famille entière.
    Le reste — 400 (requête invalide), 401/403 (authentification) — échouerait
    à l'identique sur les autres modèles : mieux vaut le remonter tout de suite
    que le masquer derrière trois appels inutiles.
    """
    return status in FALLBACK_STATUSES or 500 <= status <= 599


class GeminiHTTPError(RuntimeError):
    """Erreur HTTP de l'API Gemini, avec le statut exploitable par la cascade.

    Hérite de `RuntimeError` pour rester compatible avec les appelants qui
    attrapaient déjà ce type avant l'introduction des cascades.
    """

    def __init__(self, status: int, detail: str):
        super().__init__(f"Erreur API Gemini ({status}) : {detail}")
        self.status = status
        self.detail = detail


# Balises de langage qui désignent du Python. Un modèle répond indifféremment
# ```python, ```py ou ```python3 : les trois doivent être reconnues.
PYTHON_FENCE_TAGS = frozenset({"python", "py", "python3"})

# Une clôture markdown n'est une clôture que si elle ouvre sa ligne. Des
# backticks cités en pleine phrase ("entoure ta réponse de ```") ne doivent pas
# être pris pour le début d'un bloc, sinon la capture part de travers et le
# vrai code est perdu en silence.
_FENCE_RE = re.compile(r"^```([^\n`]*)\n(.*?)^```", re.DOTALL | re.MULTILINE)


def extract_raw_code(text: str) -> str:
    """Extrait le(s) bloc(s) de code d'une réponse formatée en markdown.

    Quand la réponse mélange des langages — le classique bloc ```bash « pour
    lancer les tests » collé sous le bloc ```python — seuls les blocs Python
    sont retenus : tout concaténer produirait un fichier qui ne compile pas.
    Si aucun bloc n'est étiqueté Python, on garde tout : la balise est souvent
    omise, et le contenu reste la meilleure réponse disponible.
    """
    blocs = _FENCE_RE.findall(text)
    if not blocs:
        return text.strip()

    pythons = [corps for tag, corps in blocs if tag.strip().lower() in PYTHON_FENCE_TAGS]
    retenus = pythons or [corps for _, corps in blocs]
    return "\n\n".join(c.strip() for c in retenus).strip()


def _key_from_dotenv(dotenv_path: str | None = None) -> str | None:
    """Lit `GEMINI_API_KEY` dans le `.env` à la racine du dépôt, en repli.

    Les conteneurs reçoivent la clé via `docker compose` (qui lit `.env`
    nativement) ; un lancement CLI direct sur l'hôte, lui, n'a rien exporté.
    Parseur minimal, zéro dépendance. Toute erreur (fichier absent, illisible)
    renvoie `None` sans lever : l'absence de clé reste un cas géré plus bas.
    `dotenv_path` n'existe que pour les tests ; en usage réel on vise le `.env`
    voisin du dépôt.
    """
    if dotenv_path is None:
        racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dotenv_path = os.path.join(racine, ".env")
    try:
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if not ligne or ligne.startswith("#"):
                    continue
                if ligne.startswith("export "):
                    ligne = ligne[7:].strip()
                cle, sep, valeur = ligne.partition("=")
                if not sep or cle.strip() != "GEMINI_API_KEY":
                    continue
                valeur = valeur.strip()
                if len(valeur) >= 2 and valeur[0] == valeur[-1] and valeur[0] in "\"'":
                    valeur = valeur[1:-1].strip()
                return valeur or None
    except Exception:
        return None
    return None


def query_gemini(
    prompt: str,
    system_instruction: str = "",
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    temperature: float = 0.2,
    timeout: int = 60,
    json_mode: bool = False,
) -> tuple[str, dict]:
    """Envoie une requête à l'API Gemini, renvoie `(texte généré, jetons consommés)`.

    La consommation vient de `usageMetadata`, que l'API rapporte elle-même : le
    tableau de bord de délégation affiche une mesure, jamais une estimation.

    La clé locale vient de `api_key`, sinon de l'environnement, sinon du `.env`
    du dépôt en repli (`_key_from_dotenv`, pour un lancement CLI direct sur
    l'hôte où rien n'est exporté). Si les trois sont vides, la requête part
    quand même sans `?key=` : une session cloud avec un identifiant réseau
    configuré sur ce domaine (en-tête `x-goog-api-key` injecté par le proxy
    de l'environnement) s'authentifie au niveau transport, invisible d'ici.
    Exiger la clé ici casserait ce mode, qui est le seul disponible en
    session cloud. Sans clé locale ni identifiant réseau, Gemini répond avec
    une erreur d'authentification explicite (capturée plus bas).
    """
    # Quand un gateway local prend la relève (session cloud, via
    # `RADAR_GEMINI_BASE_URL` ou le marqueur `/tmp/radar-gemini-gateway.url`),
    # c'est lui qui pose la clé — la joindre ici enverrait la clé injectée par
    # l'environnement (invalide sur cette session) et écraserait celle du
    # gateway côté Google (premier `?key=` prime). Un `api_key` explicitement
    # passé par l'appelant (usage programmatique, tests) court-circuite cette
    # règle : c'est un contrat d'appel qui doit être respecté.
    key = api_key or os.getenv("GEMINI_API_KEY") or _key_from_dotenv()
    gateway_in_charge = (not api_key) and (
        bool((os.environ.get("RADAR_GEMINI_BASE_URL") or "").strip())
        or bool(_read_gateway_marker())
    )
    url = _base_url_template().format(model=model)
    if key and not gateway_in_charge:
        url += f"?key={key}"

    gen_config: dict = {"temperature": temperature}
    if json_mode:
        gen_config["responseMimeType"] = "application/json"

    payload: dict = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": gen_config,
    }

    if system_instruction:
        payload["systemInstruction"] = {
            "parts": [{"text": system_instruction}]
        }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
            usage = _usage(result)
            candidates = result.get("candidates", [])
            if not candidates:
                return "", usage
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(part.get("text", "") for part in parts).strip(), usage
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(err_body)
            msg = err_json.get("error", {}).get("message", err_body)
        except Exception:
            msg = err_body
        raise GeminiHTTPError(e.code, f"{msg}{_auth_hint(e.code, msg, key)}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Erreur réseau Gemini : {e.reason}") from e


def query_with_fallback(
    prompt: str,
    system_instruction: str = "",
    tier: str = "fast",
    explicit_model: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.2,
    timeout: int = 60,
    json_mode: bool = False,
) -> tuple[str, str, dict]:
    """Interroge Gemini en descendant la cascade du tier jusqu'à une réponse.

    Renvoie `(réponse, modèle réellement utilisé, consommation de jetons)`.
    La consommation est celle du SEUL appel qui a abouti : un modèle indisponible
    répond en erreur sans rien facturer, le compter fausserait la mesure.

    Un modèle explicite court-circuite la cascade : l'appelant a demandé
    celui-là, lui substituer un autre en silence fausserait toute mesure
    comparative.
    """
    if explicit_model:
        text, usage = query_gemini(
            prompt=prompt,
            system_instruction=system_instruction,
            model=explicit_model,
            api_key=api_key,
            temperature=temperature,
            timeout=timeout,
            json_mode=json_mode,
        )
        return text, explicit_model, usage

    models_to_try = TIER_CASCADES.get(tier) or TIER_CASCADES["fast"]

    last_error: Exception | None = None
    fallbacks = 0
    for model in models_to_try:
        try:
            text, usage = query_gemini(
                prompt=prompt,
                system_instruction=system_instruction,
                model=model,
                api_key=api_key,
                temperature=temperature,
                timeout=timeout,
                json_mode=json_mode,
            )
            usage["fallbacks"] = fallbacks
            return text, model, usage
        except GeminiHTTPError as err:
            if not is_fallback_status(err.status):
                raise
            sys.stderr.write(
                f"[ai_query] '{model}' indisponible ({err.status}), "
                f"bascule sur le modèle suivant de la cascade '{tier}'...\n"
            )
            last_error = err
            fallbacks += 1
        except RuntimeError as err:
            # Panne réseau (timeout, connexion coupée...) : `query_gemini` la
            # remonte en `RuntimeError` nu, pas en `GeminiHTTPError`, donc elle
            # échappait à ce repli et faisait échouer toute la cascade sur un
            # seul aléa de transport. Même demande, modèle suivant — comme pour
            # un 5xx.
            sys.stderr.write(
                f"[ai_query] '{model}' injoignable ({err}), "
                f"bascule sur le modèle suivant de la cascade '{tier}'...\n"
            )
            last_error = err
            fallbacks += 1

    raise RuntimeError(
        f"Tous les modèles de la cascade '{tier}' ont échoué. "
        f"Dernière erreur : {last_error}"
    )


def _usage(result: dict) -> dict:
    """Consommation de jetons telle que l'API la RAPPORTE (`usageMetadata`).

    Mesure, jamais estimation : c'est ce que `radar_ops` affiche comme volume
    réellement délégué à Gemini. Un champ absent vaut 0 plutôt que None — une
    somme sur une colonne ne doit pas dépendre de la complétude de la réponse.
    """
    u = result.get("usageMetadata") or {}
    return {
        "prompt_tokens": int(u.get("promptTokenCount") or 0),
        "output_tokens": int(u.get("candidatesTokenCount") or 0),
        # `totalTokenCount` inclut aussi les jetons de raisonnement, absents des
        # deux autres compteurs : on le garde tel quel plutôt que de le recalculer.
        "total_tokens": int(u.get("totalTokenCount") or 0),
    }


def write_receipt(mode: str, status: str, **extra) -> None:
    """Trace l'appel dans `.claude/gemini-receipts.jsonl` (un JSON par ligne).

    C'est la preuve que lit le hook `scripts/hooks/gemini_gate.py` avant
    d'autoriser un commit ou l'écriture d'un premier jet : sans cette trace, la
    délégation reposerait de nouveau sur la seule vigilance du modèle. Un appel
    RATÉ est tracé lui aussi (`status="error"`) — la règle du projet est
    d'épuiser Gemini d'abord, donc une tentative sincère qui échoue (quota,
    panne, 503) rend la main à Claude en toute légitimité.

    `extra` porte la mesure lue dans `usageMetadata` (modèle, tier, jetons,
    durée) : c'est la source du tableau de bord de délégation de `radar_ops`.
    Le hook, lui, ne lit toujours que `ts`, `mode` et `status` — un reçu ancien
    sans ces champs reste valide.

    N'échoue jamais : tracer est un effet de bord, pas la mission du script.
    """
    try:
        root = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        d = os.path.join(root, ".claude")
        os.makedirs(d, exist_ok=True)
        row = {"ts": time.time(), "mode": mode, "status": status}
        row.update({k: v for k, v in extra.items() if v is not None})
        line = json.dumps(row, ensure_ascii=False)
        with open(os.path.join(d, "gemini-receipts.jsonl"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def resolve_tier(mode: str, explicit_tier: str | None = None) -> str:
    """Choisit le tier : le code et les tests demandent du raisonnement, le
    reste (logs, diffs, docs) n'est que du volume et part sur le quota large."""
    if explicit_tier:
        return explicit_tier
    return "heavy" if mode in CODE_MODES else "fast"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Passerelle d'orchestration multi-modèles Gemini pour Radar."
    )
    parser.add_argument("prompt", nargs="?", default="", help="Instruction ou prompt.")
    parser.add_argument(
        "-t", "--tier",
        choices=sorted(TIER_CASCADES),
        help="Force le profil : 'fast' (logs/diffs/docs) ou 'heavy' (code/tests). "
             "Par défaut déduit du --mode.",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(SYSTEM_PROMPTS),
        default="general",
        help="Rôle spécialisé : code, test, diag, summary, pr, ou general.",
    )
    parser.add_argument(
        "-f", "--file",
        action="append",
        default=[],
        help="Fichier(s) à injecter en contexte (option répétable).",
    )
    parser.add_argument(
        "-s", "--system",
        default="",
        help="Instruction système sur mesure (remplace celle du --mode).",
    )
    parser.add_argument(
        "-m", "--model",
        help="Force un modèle précis (court-circuite la cascade du tier).",
    )
    parser.add_argument(
        "--raw-code",
        action="store_true",
        help="Ne garde que le contenu des blocs de code (implicite en --mode code/test).",
    )
    parser.add_argument(
        "--check-syntax",
        action="store_true",
        help="Refuse une sortie qui ne compile pas en Python (code de retour 3).",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Lit l'entrée standard (logs, diffs, etc.).",
    )
    parser.add_argument(
        "--json-output",
        action="store_true",
        dest="json_mode",
        help="Force une sortie au format JSON structuré (implicite en --mode read).",
    )
    parser.add_argument(
        "-o", "--output",
        help="Écrit la réponse dans un fichier au lieu de stdout.",
    )

    args = parser.parse_args()
    chosen_tier = resolve_tier(args.mode, args.tier)

    parts = []
    if args.prompt:
        parts.append(args.prompt)

    for filepath in args.file:
        if not os.path.isfile(filepath):
            sys.stderr.write(f"Erreur : fichier introuvable '{filepath}'\n")
            return 1
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            parts.append(f"\n--- Fichier : {filepath} ---\n" + f.read())

    if args.stdin or (not sys.stdin.isatty() and not args.prompt and not args.file):
        stdin_content = sys.stdin.read().strip()
        if stdin_content:
            parts.append("\n--- Entrée standard ---\n" + stdin_content)

    full_prompt = "\n\n".join(parts).strip()
    if not full_prompt:
        sys.stderr.write("Erreur : aucun texte fourni en entrée (prompt, fichier ou stdin).\n")
        return 1

    sys_instruction = args.system or SYSTEM_PROMPTS.get(args.mode, "")

    started = time.time()
    # Taille de la demande, connue même quand l'appel échoue : c'est le volume
    # que Claude n'a pas eu à ingérer, et donc l'information utile d'un échec.
    meta = {"tier": chosen_tier, "prompt_chars": len(full_prompt),
            "session": os.environ.get("CLAUDE_SESSION_ID")}

    is_json = args.json_mode or args.mode == "read"

    try:
        response, used_model, usage = query_with_fallback(
            prompt=full_prompt,
            system_instruction=sys_instruction,
            tier=chosen_tier,
            explicit_model=args.model,
            json_mode=is_json,
        )
    except Exception as e:
        sys.stderr.write(f"Erreur : {e}\n")
        err_msg = str(e)
        if "cascade" in err_msg.lower():
            meta["error_type"] = "cascade_exhausted"
        elif isinstance(e, GeminiHTTPError):
            meta["error_type"] = f"http_{e.status}"
        else:
            meta["error_type"] = "api_error"
        meta["error_detail"] = err_msg[:200]
        write_receipt(args.mode, "error",
                      elapsed_ms=round((time.time() - started) * 1000), **meta)
        return 1

    meta.update(usage, model=used_model,
                elapsed_ms=round((time.time() - started) * 1000))

    if args.raw_code or args.mode in CODE_MODES:
        response = extract_raw_code(response)

    # Une réponse vide n'est pas un succès : filtre de sécurité, troncature ou
    # `candidates` vide. Échouer ici couvre aussi bien `-o` qu'une redirection
    # shell `> fichier.py`, que rien ne distinguerait du point de vue appelant.
    if not response.strip():
        sys.stderr.write(f"Erreur : réponse vide de {used_model}, rien à écrire.\n")
        meta["error_type"] = "empty_response"
        write_receipt(args.mode, "error", **meta)
        return 1

    if args.check_syntax:
        try:
            compile(response, "<gemini-output>", "exec")
        except SyntaxError as syn_err:
            sys.stderr.write(
                f"Erreur : le code généré par {used_model} ne compile pas ({syn_err}).\n"
            )
            meta["error_type"] = "syntax_error"
            meta["error_detail"] = str(syn_err)[:200]
            write_receipt(args.mode, "error", **meta)
            return 3

    if args.output:
        with open(args.output, "w", encoding="utf-8") as out_f:
            out_f.write(response + "\n")
        sys.stderr.write(f"[ai_query] Écrit dans {args.output} via {used_model}\n")
    else:
        print(response)

    write_receipt(args.mode, "ok", output=args.output, **meta)
    return 0


if __name__ == "__main__":
    sys.exit(main())
