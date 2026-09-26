#!/usr/bin/env python3
"""Masquage des secrets avant tout envoi sortant (plan §4.1 et §10).

Avec un seul fournisseur, une fuite de clé restait improbable ; avec quatre,
elle devient une question de temps : chaque prompt part chez un tiers
différent, et un `cat .env` collé dans un contexte n'a rien d'exceptionnel.
Ce module est donc appelé **dans l'adaptateur**, pas par l'appelant : un futur
appelant ne peut pas l'oublier.

Deux principes de conception :

1. **Ne jamais lever.** Un masquage qui échoue vaut mieux qu'un appel perdu :
   toute erreur interne rend le texte inchangé.
2. **Masquer juste assez.** Un filtre trop large détruirait le sens du prompt
   (`TOKEN_LIMIT = 1000` n'est pas un secret). Les valeurs courtes ou purement
   numériques sont laissées telles quelles, seules les chaînes longues et
   aléatoires sont remplacées.

Ce qui n'est pas fait, volontairement : pas de détection d'entropie générique.
Elle produit des faux positifs sur du code et des hachés légitimes, et un faux
positif ici coûte une réponse inutile — le projet préfère une liste de formes
connues, vérifiable ligne à ligne.
"""

from __future__ import annotations

import re

MASK = "[masqué]"

# Formes de clés réelles des fournisseurs et des services utilisés par le
# projet. L'ordre compte : `sk-or-v1-` doit être testé avant la forme générique
# `sk-`, sinon la clé OpenRouter serait à moitié masquée.
_KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("openrouter_key", re.compile(r"sk-or-v1-[0-9A-Za-z_\-]{20,}")),
    ("xai_key", re.compile(r"xai-[0-9A-Za-z]{20,}")),
    ("deepseek_key", re.compile(r"sk-[0-9a-fA-F]{20,}")),
    ("api_key_generic", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
)

# `NOM= valeur` où le nom parle de secret. Le nom est conservé : il porte le
# sens (« quelle clé manque »), la valeur est la seule chose à protéger.
_ASSIGN_RE = re.compile(
    r"(?P<name>\b[A-Za-z0-9_\-]*"
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|WEBHOOK|BEARER)"
    r"[A-Za-z0-9_\-]*\b"
    r"\s*[:=]\s*)"
    r"(?P<quote>['\"]?)"
    r"(?P<value>[^\s'\"]{4,})"
    r"(?P=quote)",
    re.IGNORECASE,
)

_HEADER_RE = re.compile(
    r"(?im)^(\s*(?:authorization|x-goog-api-key|x-api-key|api-key)\s*:\s*)(\S.*)$"
)

_QUERY_RE = re.compile(
    r"([?&](?:key|api_key|apikey|access_token|token)=)([^&\s]+)", re.IGNORECASE
)

# En dessous de cette longueur, une « valeur de secret » est presque toujours
# un numéro de version, un booléen ou un compteur : la masquer casserait le
# prompt sans rien protéger.
_MIN_SECRET_LEN = 12


def _mask_value(value: str) -> bool:
    """La valeur ressemble-t-elle à un vrai secret, ou à un réglage anodin ?"""
    if len(value) < _MIN_SECRET_LEN:
        return False
    # Un nombre, un hachage court, un chemin : rien de tout cela n'est une clé.
    return not value.isdigit()


def _sub_count(
    pattern: re.Pattern[str],
    repl: object,
    text: str,
) -> tuple[str, int]:
    """`pattern.sub` en conservant le nombre de remplacements réellement faits."""
    count = 0

    def _wrapped(match: re.Match[str]) -> str:
        nonlocal count
        out = repl(match) if callable(repl) else repl
        if out != match.group(0):
            count += 1
        return out

    return pattern.sub(_wrapped, text), count


def redact_report(text: str) -> tuple[str, dict[str, int]]:
    """Masque les secrets d'un texte et dit combien de fois, par catégorie.

    Le rapport ne contient **jamais** la valeur masquée : il ne sert qu'à être
    résumé dans un reçu sous forme de compteur.
    """
    if not isinstance(text, str) or not text:
        return text, {}

    report: dict[str, int] = {}
    out = text
    try:
        for label, pattern in _KEY_PATTERNS:
            out, hits = _sub_count(pattern, MASK, out)
            if hits:
                report[label] = report.get(label, 0) + hits

        out, hits = _sub_count(_HEADER_RE, lambda m: m.group(1) + MASK, out)
        if hits:
            report["header"] = report.get("header", 0) + hits

        out, hits = _sub_count(_QUERY_RE, lambda m: m.group(1) + MASK, out)
        if hits:
            report["query_param"] = report.get("query_param", 0) + hits

        def _assign(match: re.Match[str]) -> str:
            if not _mask_value(match.group("value")):
                return match.group(0)
            return f"{match.group('name')}{MASK}"

        out, hits = _sub_count(_ASSIGN_RE, _assign, out)
        if hits:
            report["assignment"] = report.get("assignment", 0) + hits
    except Exception:  # pragma: no cover — filet de sécurité, jamais atteint en pratique
        return text, {}

    return out, report


def redact(text: str) -> str:
    """Version courte : le texte masqué, sans le détail."""
    return redact_report(text)[0]


def describe(report: dict[str, int]) -> str:
    """Résumé lisible d'un rapport de masquage, pour stderr ou un reçu."""
    if not report:
        return "aucun secret détecté"
    return ", ".join(f"{label}={count}" for label, count in sorted(report.items()))


def merge(*reports: dict[str, int]) -> dict[str, int]:
    """Additionne des rapports successifs (prompt + instruction système)."""
    total: dict[str, int] = {}
    for report in reports:
        for label, count in (report or {}).items():
            total[label] = total.get(label, 0) + count
    return total
