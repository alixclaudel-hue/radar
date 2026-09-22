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

BASE_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# Cascades ordonnées par capacité décroissante. Les noms sont ceux renvoyés par
# ListModels (v1beta) — un modèle absent de cette liste répond 404, pas une
# erreur silencieuse : vérifier avant d'en ajouter un.
TIER_CASCADES = {
    # Raisonnement : quota journalier étroit, réservé au code et aux tests.
    "heavy": [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
        "gemini-3.5-flash-lite",  # repli ultime : lent à raisonner mais disponible
    ],
    # Volume : quota large, pour tout ce qui est lecture/reformulation.
    "fast": [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash-lite",
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


def query_gemini(
    prompt: str,
    system_instruction: str = "",
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    temperature: float = 0.2,
    timeout: int = 60,
) -> str:
    """Envoie une requête à l'API Gemini et renvoie le texte généré.

    Sans clé locale (`api_key`/`GEMINI_API_KEY` absents), la requête part
    quand même sans `?key=` : une session cloud avec un identifiant réseau
    configuré sur ce domaine (en-tête `x-goog-api-key` injecté par le proxy
    de l'environnement) s'authentifie au niveau transport, invisible d'ici.
    Exiger la clé ici casserait ce mode, qui est le seul disponible en
    session cloud. Sans clé locale ni identifiant réseau, Gemini répond avec
    une erreur d'authentification explicite (capturée plus bas).
    """
    key = api_key or os.getenv("GEMINI_API_KEY")
    url = BASE_URL_TEMPLATE.format(model=model)
    if key:
        url += f"?key={key}"

    payload: dict = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": temperature,
        },
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
            candidates = result.get("candidates", [])
            if not candidates:
                return ""
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(part.get("text", "") for part in parts).strip()
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
) -> tuple[str, str]:
    """Interroge Gemini en descendant la cascade du tier jusqu'à une réponse.

    Renvoie `(réponse, modèle réellement utilisé)`. Un modèle explicite
    court-circuite la cascade : l'appelant a demandé celui-là, lui substituer
    un autre en silence fausserait toute mesure comparative.
    """
    if explicit_model:
        return query_gemini(
            prompt=prompt,
            system_instruction=system_instruction,
            model=explicit_model,
            api_key=api_key,
            temperature=temperature,
            timeout=timeout,
        ), explicit_model

    models_to_try = TIER_CASCADES.get(tier) or TIER_CASCADES["fast"]

    last_error: Exception | None = None
    for model in models_to_try:
        try:
            res = query_gemini(
                prompt=prompt,
                system_instruction=system_instruction,
                model=model,
                api_key=api_key,
                temperature=temperature,
                timeout=timeout,
            )
            return res, model
        except GeminiHTTPError as err:
            if not is_fallback_status(err.status):
                raise
            sys.stderr.write(
                f"[ai_query] '{model}' indisponible ({err.status}), "
                f"bascule sur le modèle suivant de la cascade '{tier}'...\n"
            )
            last_error = err

    raise RuntimeError(
        f"Tous les modèles de la cascade '{tier}' ont échoué. "
        f"Dernière erreur : {last_error}"
    )


def write_receipt(mode: str, status: str) -> None:
    """Trace l'appel dans `.claude/gemini-receipts.jsonl` (un JSON par ligne).

    C'est la preuve que lit le hook `scripts/hooks/gemini_gate.py` avant
    d'autoriser un commit ou l'écriture d'un premier jet : sans cette trace, la
    délégation reposerait de nouveau sur la seule vigilance du modèle. Un appel
    RATÉ est tracé lui aussi (`status="error"`) — la règle du projet est
    d'épuiser Gemini d'abord, donc une tentative sincère qui échoue (quota,
    panne, 503) rend la main à Claude en toute légitimité.

    N'échoue jamais : tracer est un effet de bord, pas la mission du script.
    """
    try:
        root = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        d = os.path.join(root, ".claude")
        os.makedirs(d, exist_ok=True)
        line = json.dumps({"ts": time.time(), "mode": mode, "status": status},
                          ensure_ascii=False)
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

    try:
        response, used_model = query_with_fallback(
            prompt=full_prompt,
            system_instruction=sys_instruction,
            tier=chosen_tier,
            explicit_model=args.model,
        )
    except Exception as e:
        sys.stderr.write(f"Erreur : {e}\n")
        write_receipt(args.mode, "error")
        return 1

    if args.raw_code or args.mode in CODE_MODES:
        response = extract_raw_code(response)

    # Une réponse vide n'est pas un succès : filtre de sécurité, troncature ou
    # `candidates` vide. Échouer ici couvre aussi bien `-o` qu'une redirection
    # shell `> fichier.py`, que rien ne distinguerait du point de vue appelant.
    if not response.strip():
        sys.stderr.write(f"Erreur : réponse vide de {used_model}, rien à écrire.\n")
        write_receipt(args.mode, "error")
        return 1

    if args.check_syntax:
        try:
            compile(response, "<gemini-output>", "exec")
        except SyntaxError as syn_err:
            sys.stderr.write(
                f"Erreur : le code généré par {used_model} ne compile pas ({syn_err}).\n"
            )
            # 3 et non 2 : argparse réserve déjà 2 aux erreurs de ligne de
            # commande, un appelant doit pouvoir distinguer les deux échecs.
            write_receipt(args.mode, "error")
            return 3

    if args.output:
        with open(args.output, "w", encoding="utf-8") as out_f:
            out_f.write(response + "\n")
        sys.stderr.write(f"[ai_query] Écrit dans {args.output} via {used_model}\n")
    else:
        print(response)

    write_receipt(args.mode, "ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
