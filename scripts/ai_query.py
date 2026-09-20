#!/usr/bin/env python3
"""Passerelle légère vers l'API Gemini (Google AI Studio).

Utilise uniquement la bibliothèque standard (urllib) pour ne pas ajouter
de dépendance externe. Conçu pour compresser des logs volumineux, prétraiter
des données ou générer des squelettes de code sans consommer de tokens Claude.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_MODEL = "gemini-2.0-flash"
API_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
)


def query_gemini(
    prompt: str,
    system_instruction: str = "",
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    temperature: float = 0.2,
    timeout: int = 60,
) -> str:
    """Envoie une requête à l'API Gemini et renvoie le texte généré."""
    key = api_key or os.getenv("GEMINI_API_KEY")
    if not key:
        raise ValueError(
            "GEMINI_API_KEY manquante. Définis la variable d'environnement GEMINI_API_KEY."
        )

    url = API_URL_TEMPLATE.format(model=model, key=key)

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
        raise RuntimeError(f"Erreur API Gemini ({e.code}) : {msg}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Erreur réseau Gemini : {e.reason}") from e


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Interroge Gemini (gratuit) via Google AI Studio."
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default="",
        help="Prompt ou instruction principale.",
    )
    parser.add_argument(
        "-f", "--file",
        help="Chemin d'un fichier à joindre au prompt (ex: logs, diff).",
    )
    parser.add_argument(
        "-s", "--system",
        default="",
        help="Instruction système optionnelle.",
    )
    parser.add_argument(
        "-m", "--model",
        default=DEFAULT_MODEL,
        help=f"Modèle Gemini à utiliser (défaut: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Lire du texte additionnel depuis l'entrée standard.",
    )

    args = parser.parse_args()

    parts = []
    if args.prompt:
        parts.append(args.prompt)

    if args.file:
        if not os.path.isfile(args.file):
            sys.stderr.write(f"Erreur: fichier introuvable '{args.file}'\n")
            return 1
        with open(args.file, "r", encoding="utf-8", errors="replace") as f:
            parts.append(f"\n--- Contenu de {args.file} ---\n" + f.read())

    if args.stdin or (not sys.stdin.isatty() and not args.prompt and not args.file):
        stdin_content = sys.stdin.read().strip()
        if stdin_content:
            parts.append("\n--- Entrée standard ---\n" + stdin_content)

    full_prompt = "\n\n".join(parts).strip()
    if not full_prompt:
        sys.stderr.write("Erreur: aucun texte fourni en entrée (prompt, fichier ou stdin).\n")
        return 1

    try:
        response = query_gemini(
            prompt=full_prompt,
            system_instruction=args.system,
            model=args.model,
        )
        print(response)
        return 0
    except Exception as e:
        sys.stderr.write(f"Erreur: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
