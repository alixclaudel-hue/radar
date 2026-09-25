#!/usr/bin/env python3
"""Schémas des sorties structurées — la garantie JSON du plan (§6, mécanisme 3).

Le plan demande que le JSON soit « réellement garanti » : le format natif du
fournisseur quand il l'accepte, une validation locale sinon, **un** essai de
réparation, puis un échec explicite. Ce module porte les deux moitiés de cette
promesse :

1. les **schémas canoniques** des modes à sortie structurée — le pavage de
   `context`, le choix de `search`, l'enveloppe des modes de code. Écrits une
   seule fois : l'adaptateur en dérive le format natif du fournisseur, le
   courtier s'en sert pour relire ce qui revient ;
2. le **validateur** qui confronte une charge à son schéma, en clair, sans jamais
   lever. C'est lui qui décide si une réponse part sur disque ou repart au modèle.

Deux traductions, deux philosophies :

- [`pour_openai()`](scripts/ai/schema.py:1) rend la forme `response_format` des
  fournisseurs OpenAI-compatibles (OpenRouter, DeepSeek, xAI). Elle est
  *strictement stricte* (`strict: true`, `additionalProperties: false`, toutes les
  propriétés requises) : c'est ce que le fournisseur exige pour contraindre
  réellement la génération, et c'est sans risque puisque le modèle reçoit alors le
  contrat complet. Le validateur local, lui, ne réclame pas autant — tout ce que
  le format natif laisse passer est donc acceptable ici.
- [`pour_gemini()`](scripts/ai/schema.py:1) rend la forme `responseSchema` du
  protocole Gemini, débarrassée de ce qu'il refuse (`additionalProperties`,
  `strict`, `name`) et **typée en majuscules** : le proto attend les noms de son
  énumération (`OBJECT`, `STRING`, `ARRAY`), ceux que la documentation REST écrit
  ainsi. Un type en minuscules est un 400, donc un appel gratuit perdu — le même
  piège qu'ailleurs dans le socle, évité au même endroit.

Le validateur est délibérément **plus tolérant que le schéma natif** : il ne
vérifie que ce que le schéma déclare explicitement, `required` clé par clé et le
type de chaque clé décrite. Une clé en trop est sans danger ; refuser une réponse
exploitable coûterait un tour de réparation puis un échec. Les vérifications
sémantiques — les documents rendus correspondent-ils à ceux envoyés, le chemin
choisi est-il dans la liste fermée — restent au courtier, seul à connaître la
demande.

Ce module n'importe rien du projet : il ne connaît ni le réseau, ni le catalogue,
ni le disque, et ne lève jamais.
"""

from __future__ import annotations

# --- Schémas canoniques ------------------------------------------------------
#
# `required` y est **minimal** : la clé dont l'absence rend la charge
# inexploitable par l'appelant. Pour le pavage, c'est le `path` de chaque
# document — sans lui, aucune analyse ne peut être rattachée au bon fichier (le
# courtier refuse d'ailleurs un appariement partiel). Les autres clés sont
# décrites, donc contrôlées **quand elles sont là**, mais leur absence ne
# condamne pas la réponse : le prompt les demande toutes, la relecture ne joue
# pas la montre avec elles.

_CONTEXTE_DOCUMENT = {
    "type": "object",
    "required": ["path"],
    "properties": {
        "path": {"type": "string"},
        "title": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
        "key_points": {"type": "array", "items": {"type": "string"}},
        "todos": {"type": "array", "items": {"type": "string"}},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "uncertain": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "location": {"type": "string"},
                    "question": {"type": "string"},
                },
            },
        },
    },
}

# Noms de schéma : ils partent au fournisseur (`json_schema.name`) et reviennent
# dans le reçu. Courts, sans accent, stables — un nom qui change casse le cache
# de prompt de certains fournisseurs.
CONTEXTE_NOM = "documents_contexte"
CONTEXTE = {
    "type": "object",
    "required": ["documents"],
    "properties": {"documents": {"type": "array", "items": _CONTEXTE_DOCUMENT}},
}

RECHERCHE_NOM = "choix_recherche"
RECHERCHE = {
    "type": "object",
    "required": ["path", "reason"],
    "properties": {
        "path": {"type": "string"},
        "reason": {"type": "string"},
    },
}

ENVELOPPE_NOM = "enveloppe_code"
ENVELOPPE = {
    "type": "object",
    "required": ["explication", "code"],
    "properties": {
        "explication": {"type": "string"},
        "code": {"type": "string"},
    },
}


# --- Traduction vers le format natif de chaque fournisseur -------------------


def _strict(noeud):
    """Copie du schéma, rendue stricte pour un fournisseur qui l'exige.

    Deux transformations, appliquées à **tout** nœud objet, y compris imbriqué :

    - `additionalProperties: false` : une clé hors contrat serait refusée par le
      fournisseur, pas par nous ; autant la refuser là où elle naît ;
    - `required = sorted(properties)` : `strict: true` exige que *toutes* les
      propriétés soient requises. C'est plus contraignant que le validateur local,
      et c'est voulu : ce que le modèle produit alors passe forcément la relecture
      locale, qui ne réclame que le minimum.
    """
    if not isinstance(noeud, dict):
        return noeud
    resultat = dict(noeud)
    proprietes = resultat.get("properties")
    if resultat.get("type") == "object" and isinstance(proprietes, dict):
        copie = {nom: _strict(valeur) for nom, valeur in proprietes.items()}
        resultat["properties"] = copie
        resultat["required"] = sorted(copie)
        resultat["additionalProperties"] = False
    elif isinstance(resultat.get("items"), dict):
        resultat["items"] = _strict(resultat["items"])
    return resultat


def pour_openai(nom: str, schema: dict) -> dict:
    """`response_format` strict d'un fournisseur OpenAI-compatible.

    Rend la charge complète (`{"type": "json_schema", "json_schema": {...}}`) et
    non le seul schéma : c'est ce que le champ `response_format` attend, et le
    laisser assembler par l'appelant était le meilleur moyen d'oublier `strict`.
    """
    return {
        "type": "json_schema",
        "json_schema": {"name": nom, "strict": True, "schema": _strict(schema)},
    }


# Champs refusés par `responseSchema` : absents du proto `Schema`, et un champ
# inconnu dans le corps d'une requête Gemini est un 400 (pas un avertissement).
_GEMINI_REFUSES = frozenset({"additionalProperties", "strict", "name", "$schema"})


def _gemini_noeud(noeud):
    """Nœud adapté au proto Gemini : types en majuscules, champs refusés ôtés."""
    if isinstance(noeud, list):
        return [_gemini_noeud(item) for item in noeud]
    if not isinstance(noeud, dict):
        return noeud
    sortie: dict = {}
    for cle, valeur in noeud.items():
        if cle in _GEMINI_REFUSES:
            continue
        if cle == "required" and not valeur:
            # Une liste `required` vide ne dit rien à Gemini, et il la refuse.
            continue
        if cle == "type" and isinstance(valeur, str):
            sortie[cle] = valeur.upper()
            continue
        sortie[cle] = _gemini_noeud(valeur)
    return sortie


def pour_gemini(schema: dict) -> dict:
    """`responseSchema` du protocole Gemini, dérivé du même schéma canonique.

    Le transport (`ai_query.query_gemini`) force `responseMimeType` dès qu'un
    schéma est fourni : Gemini refuse l'un sans l'autre, et le refus est un 400.
    """
    return _gemini_noeud(schema)


# --- Validation locale -------------------------------------------------------


def _type_ok(valeur, attendu: str) -> bool:
    """Le type JSON de `valeur` est-il celui annoncé ?

    `bool` est un `int` en Python : sans la garde explicite, `True` passerait pour
    un entier. C'est le seul piège du genre, et il compte ici — un `key_points`
    valant `true` n'est pas une liste de points clés.
    """
    if attendu == "object":
        return isinstance(valeur, dict)
    if attendu == "array":
        return isinstance(valeur, list)
    if attendu == "string":
        return isinstance(valeur, str)
    if attendu == "boolean":
        return isinstance(valeur, bool)
    if attendu == "integer":
        return isinstance(valeur, int) and not isinstance(valeur, bool)
    if attendu == "number":
        return isinstance(valeur, (int, float)) and not isinstance(valeur, bool)
    if attendu == "null":
        return valeur is None
    # Type inconnu du validateur : on ne se prononce pas plutôt que de refuser
    # une réponse sur un vocabulaire qu'on ne comprend pas.
    return True


def _nom_type(valeur) -> str:
    """Nom du type observé, pour un message d'erreur lisible."""
    if valeur is None:
        return "null"
    if isinstance(valeur, bool):
        return "boolean"
    if isinstance(valeur, dict):
        return "object"
    if isinstance(valeur, list):
        return "array"
    if isinstance(valeur, int):
        return "integer"
    if isinstance(valeur, float):
        return "number"
    if isinstance(valeur, str):
        return "string"
    return type(valeur).__name__


def valider(valeur, attendu: dict, chemin: str = "") -> list[str]:
    """Erreurs de `valeur` contre `attendu`, à plat et en clair.

    Ne vérifie que ce que le schéma déclare : chaque clé de `required`, et le type
    de chaque clé décrite. Ni clé en trop refusée, ni `additionalProperties`
    imposé — c'est la relecture qui déclenche un tour de réparation, et refuser
    une réponse exploitable coûterait plus cher que la laisser passer.

    Rend une liste de messages, vide si la charge est conforme. Un défaut de type
    arrête la descente à cet endroit : décrire l'intérieur d'un objet qui n'en est
    pas un ne veut rien dire. Les chemins sont en clair
    (`réponse.documents[0].path`) parce que ce texte part dans le prompt de
    réparation : c'est ce que le modèle doit corriger.
    """
    if not isinstance(attendu, dict):
        return []
    emplacement = chemin or "réponse"
    type_attendu = attendu.get("type")
    if isinstance(type_attendu, str) and not _type_ok(valeur, type_attendu):
        return [f"{emplacement} : attendu {type_attendu}, reçu {_nom_type(valeur)}"]

    erreurs: list[str] = []
    if type_attendu == "object" and isinstance(valeur, dict):
        for cle in attendu.get("required") or []:
            if cle not in valeur:
                erreurs.append(f"{emplacement} : clé « {cle} » absente")
        for cle, sous_schema in (attendu.get("properties") or {}).items():
            if cle in valeur:
                erreurs += valider(valeur[cle], sous_schema, f"{emplacement}.{cle}")
    elif type_attendu == "array" and isinstance(valeur, list):
        sous_schema = attendu.get("items")
        if isinstance(sous_schema, dict):
            for index, item in enumerate(valeur):
                erreurs += valider(item, sous_schema, f"{emplacement}[{index}]")
    return erreurs


def resume(erreurs: list[str], max_erreurs: int = 5) -> str:
    """Les premières erreurs en une ligne : de quoi écrire un reçu et une relance.

    Bornée parce qu'un modèle qui se trompe de structure se trompe sur tout : la
    dixième erreur n'apprend rien de plus et noierait celle qui compte. Le nombre
    d'erreurs restantes est dit, pour que le lecteur sache que la liste est
    tronquée.
    """
    if not erreurs:
        return ""
    texte = "; ".join(erreurs[:max_erreurs])
    reste = len(erreurs) - max_erreurs
    if reste > 0:
        texte += f" (+{reste} autre(s))"
    return texte


__all__ = [
    "CONTEXTE",
    "CONTEXTE_NOM",
    "ENVELOPPE",
    "ENVELOPPE_NOM",
    "RECHERCHE",
    "RECHERCHE_NOM",
    "pour_gemini",
    "pour_openai",
    "resume",
    "valider",
]
