#!/usr/bin/env python3
"""Enveloppe JSON des modes de code — la garantie de sortie de `code` et `test`.

Le plan de délégation demande deux choses que les modes `code` et `test` ne
faisaient pas :

1. une **explication** séparée du code, pour que l'appelant puisse lire le
   raisonnement sans deviner où finit la prose et où commence le programme ;
2. une **garantie** sur le code lui-même : il doit être du Python compilable, et
   l'enveloppe est ce qui rend la vérification possible *avant* d'écrire un
   fichier ou de rendre la main à l'appelant.

Ce module ne connaît ni le réseau ni le catalogue : il demande le format, le
relit, et fabrique la relance quand la relecture révèle une erreur de syntaxe.
Le courtier s'occupe du reste — c'est ce qui rend ces trois fonctions testables
sans aucun fournisseur.

Le repli compte autant que le succès : un modèle qui ignore la consigne et rend
un bloc markdown classique ne doit **pas** transformer l'appel en échec. On
retombe alors sur l'extraction historique d'`ai_query`, explication vide. C'est
ce qui garde les modes de code utilisables avec les fournisseurs qui ne savent
pas obéir à un format.
"""

from __future__ import annotations

import json

from scripts.ai import jsonout
from scripts.ai_query import extract_raw_code

# Demandée en fin de prompt utilisateur : les modèles qui suivent le mieux une
# consigne de format sont ceux à qui on la répète au dernier moment, juste avant
# la génération, plutôt que dans l'instruction système seule.
ENVELOPE_INSTRUCTION = (
    "Réponds par un **unique objet JSON**, sans texte avant ni après, de la forme :\n"
    '{"explication": "<ce que fait le programme et pourquoi ces choix>", '
    '"code": "<le programme Python complet>"}\n'
    "Le champ `code` contient le programme entier, prêt à être enregistré dans un "
    "fichier `.py` : pas de balise Markdown, pas de commentaire d'accompagnement "
    "hors du programme. Le champ `explication` dit en prose ce que fait le "
    "programme et quels compromis ont été faits. Les deux champs sont obligatoires."
)

# Le repérage de l'objet dans une réponse bavarde vit dans
# [`jsonout`](jsonout.py:1), partagé avec le mode `context` : un seul décodeur,
# donc une seule correction à faire le jour où un modèle surprend.


def _enveloppe_valide(charge) -> bool:
    """Un objet JSON n'est une enveloppe que s'il porte un `code` non vide.

    Le `code` vide est le cas piège : un modèle qui a « compris » le format mais
    rendu `{"explication": "...", "code": ""}` produirait un fichier vide. C'est
    précisément l'échec que la refonte doit empêcher, donc il ne compte pas comme
    une enveloppe lisible.
    """
    return (
        isinstance(charge, dict)
        and isinstance(charge.get("code"), str)
        and bool(charge["code"].strip())
    )


def extract_envelope(texte: str) -> dict | None:
    """Objet `{explication, code}` lu dans la réponse, ou `None`.

    Les candidats viennent de [`jsonout.candidats()`](jsonout.py:75), qui essaie
    la réponse entière, puis les blocs clôturés, puis les objets équilibrés. La
    première lecture qui donne un `code` non vide gagne — on ne mélange jamais
    deux candidats, un code à moitié d'une source et à moitié d'une autre ne
    compilerait pas.
    """
    for candidat in jsonout.candidats(texte):
        try:
            charge = json.loads(candidat)
        except ValueError:
            continue
        if _enveloppe_valide(charge):
            return charge
    return None


def split_answer(texte: str) -> tuple[str, str]:
    """`(code, explication)` — enveloppe si lisible, extraction historique sinon.

    Rend toujours deux chaînes : l'appelant n'a pas à savoir laquelle des deux
    lectures a servi. L'explication est vide quand le modèle n'a pas suivi le
    format, ce qui est un état normal et non une erreur.
    """
    charge = extract_envelope(texte)
    if charge is not None:
        explication = charge.get("explication")
        return charge["code"].strip(), (explication if isinstance(explication, str) else "").strip()
    return extract_raw_code(texte), ""


def repair_prompt(code: str, erreur: BaseException) -> str:
    """Message de relance : le code fautif et l'erreur de compilation, rien d'autre.

    L'erreur est recopiée **telle quelle**, jamais résumée : un `SyntaxError`
    Python porte le numéro de ligne, et `str()` sur l'exception garde le message
    exact (`invalid syntax`). C'est l'information la plus utile à la réparation,
    et c'est la première chose qu'un résumé ferait disparaître.

    Le code est renvoyé dans un bloc ```python : il replace le modèle devant sa
    propre sortie, sans quoi il « corrige » un programme qu'il ne voit plus.
    """
    lignes = [
        "Le programme ci-dessous ne compile pas.",
        "",
        f"Erreur de compilation : {type(erreur).__name__}",
        str(erreur),
    ]
    ligne_fautive = getattr(erreur, "lineno", None)
    if ligne_fautive:
        lignes.append(f"Ligne fautive : {ligne_fautive}")
    texte_fautif = getattr(erreur, "text", None)
    if texte_fautif:
        lignes.append(f"Texte de cette ligne : {texte_fautif.strip()}")
    lignes += [
        "",
        "Programme fautif :",
        "```python",
        code,
        "```",
        "",
        "Corrige ce qui empêche la compilation et rends l'objet JSON complet "
        "(`explication` et `code`). Ne change rien d'autre.",
    ]
    return "\n".join(lignes)
