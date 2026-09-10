"""Comparaison de titres par recouvrement de mots.

Partagé par les recherches Bandcamp / Volumo / YouTube et par l'appariement des
vidéos d'une sortie Discogs à sa tracklist : tous cherchent « ce résultat
parle-t-il bien de cet artiste et de ce titre ? » avec la même mesure.
"""
import re
import unicodedata

# Lettres non décomposables par NFKD (pas de forme "lettre de base + accent") :
# sans cette table, un mot comme "Bjørn" est coupé en deux jetons {bj, rn} au
# lieu de matcher la graphie sans accent "Bjorn" qu'utilise souvent YouTube
# (diagnostic 2026-09-10, RECOS RADAR : matchs parfaits rejetés faute de jeton
# commun sur un nom d'artiste scandinave/allemand/français).
_EXTRA_ACCENTS = str.maketrans({
    "ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "ð": "d", "þ": "th", "ł": "l",
})


def _deaccent(s):
    s = unicodedata.normalize("NFKD", (s or "").translate(_EXTRA_ACCENTS))
    return "".join(c for c in s if not unicodedata.combining(c))


def toks(s, drop=()):
    """Mots significatifs d'un libellé (minuscules, sans accents, alphanumériques).

    `drop` retire les mots vides propres à l'appelant (articles, « feat »…)."""
    out = set(re.findall(r"[a-z0-9]+", _deaccent((s or "").lower())))
    return out - set(drop) if drop else out


def overlap(want, cand):
    """Part de `want` couverte par `cand`, entre 0.0 et 1.0.

    Renvoie 0.0 si l'un des deux ensembles est vide : sans mot à comparer, on ne
    conclut pas à une correspondance (et on évite la division par zéro)."""
    if not want or not cand:
        return 0.0
    return len(want & cand) / len(want)
