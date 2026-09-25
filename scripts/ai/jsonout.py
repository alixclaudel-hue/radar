#!/usr/bin/env python3
"""Lecture d'un objet JSON dans une réponse de modèle — un seul décodeur.

Deux besoins distincts réclamaient exactement le même code : l'enveloppe des
modes de code ([`envelope.py`](envelope.py:1)) et le mode `context`, qui attend
un objet `{"documents": [...]}`. Le recopier une troisième fois pour la garantie
JSON du plan (§6, mécanisme 3) aurait installé trois lecteurs qui divergent à la
première correction. Il vit donc ici, et personne ne le recopie.

La difficulté n'est pas de lire du JSON, c'est de le **trouver** : les modèles
bavards encadrent l'objet de prose, ou l'enferment dans un bloc clôturé. Trois
lectures, de la plus stricte à la plus tolérante, sont donc tentées dans l'ordre,
et la première qui donne un objet dictionnaire gagne.

Ce module ne lève **jamais** : une réponse illisible est un état normal de la
délégation, que l'appelant traduit en échec explicite. Il ne connaît ni le
réseau, ni le catalogue, ni le disque.
"""

from __future__ import annotations

import json
import re

# Une clôture n'ouvre un bloc que si elle commence sa ligne : des backticks cités
# en pleine phrase ne doivent pas lancer la capture au mauvais endroit. Même
# expression que dans `ai_query`, volontairement dupliquée là-bas pour que le
# transport Gemini reste lisible sans dépendre de ce module.
_FENCE_RE = re.compile(r"^```([^\n`]*)\n(.*?)^```", re.DOTALL | re.MULTILINE)

# Balises acceptées pour un bloc qui contient l'objet. La liste est
# **restreinte** : un bloc ```python est du code, pas du JSON, et doit tomber
# dans l'extraction de code. Une balise de langage lucide (`yaml`, `toml`) ne
# doit pas non plus être lue comme du JSON.
FENCE_TAGS_JSON = frozenset({"", "json", "jsonc", "json5"})


def objets_equilibres(texte: str) -> list[str]:
    """Tranches `{...}` équilibrées du texte, dans l'ordre d'apparition.

    Sert au cas le plus courant d'un modèle bavard : « Voici le résultat :
    {...} ». On ne peut pas se contenter de couper au premier `}` — un champ
    texte contient lui-même des accolades (un dictionnaire littéral dans un
    programme, une accolade citée en prose) et une coupe naïve rendrait l'objet
    illisible. Les accolades à l'intérieur d'une chaîne JSON ne comptent donc pas
    dans la profondeur.
    """
    tranches: list[str] = []
    debut: int | None = None
    profondeur = 0
    dans_chaine = False
    echappe = False

    for index, caractere in enumerate(texte):
        if dans_chaine:
            if echappe:
                echappe = False
            elif caractere == "\\":
                echappe = True
            elif caractere == '"':
                dans_chaine = False
            continue
        if caractere == '"':
            dans_chaine = True
            continue
        if caractere == "{":
            if profondeur == 0:
                debut = index
            profondeur += 1
        elif caractere == "}":
            if profondeur:
                profondeur -= 1
                if profondeur == 0 and debut is not None:
                    tranches.append(texte[debut : index + 1])
                    debut = None
    return tranches


def candidats(texte: str) -> list[str]:
    """Tranches susceptibles de contenir l'objet, de la plus stricte à la plus large.

    Trois lectures, dans cet ordre : la réponse entière si elle commence par une
    accolade, puis chaque bloc clôturé de balise vide/`json`, puis chaque objet
    équilibré trouvé dans le texte. L'appelant essaie dans l'ordre et s'arrête au
    premier qui convient — on ne mélange jamais deux candidats, un objet à moitié
    d'une source et à moitié d'une autre serait inexploitable.
    """
    if not texte or not texte.strip():
        return []

    candidats_: list[str] = []
    brut = texte.strip()
    if brut.startswith("{"):
        candidats_.append(brut)
    for balise, corps in _FENCE_RE.findall(texte):
        if balise.strip().lower() in FENCE_TAGS_JSON:
            candidats_.append(corps.strip())
    candidats_.extend(objets_equilibres(texte))
    return candidats_


def extract_json_object(texte: str) -> dict | None:
    """Premier objet JSON dictionnaire lisible dans la réponse, ou `None`.

    `None` couvre trois cas qu'il ne faut pas confondre plus loin mais qui se
    traitent pareil ici : réponse vide, JSON invalide, et JSON valide qui n'est
    pas un objet (une liste ou un scalaire, qu'aucun de nos schémas n'attend).
    """
    for candidat in candidats(texte):
        try:
            charge = json.loads(candidat)
        except ValueError:
            continue
        if isinstance(charge, dict):
            return charge
    return None


__all__ = [
    "FENCE_TAGS_JSON",
    "candidats",
    "extract_json_object",
    "objets_equilibres",
]
