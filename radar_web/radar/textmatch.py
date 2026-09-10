"""Comparaison de titres par recouvrement de mots.

Partagé par les recherches Bandcamp / Volumo / YouTube et par l'appariement des
vidéos d'une sortie Discogs à sa tracklist : tous cherchent « ce résultat
parle-t-il bien de cet artiste et de ce titre ? » avec la même mesure.
"""
import re


def toks(s, drop=()):
    """Mots significatifs d'un libellé (minuscules, alphanumériques).

    `drop` retire les mots vides propres à l'appelant (articles, « feat »…)."""
    out = set(re.findall(r"[a-z0-9]+", (s or "").lower()))
    return out - set(drop) if drop else out


def overlap(want, cand):
    """Part de `want` couverte par `cand`, entre 0.0 et 1.0.

    Renvoie 0.0 si l'un des deux ensembles est vide : sans mot à comparer, on ne
    conclut pas à une correspondance (et on évite la division par zéro)."""
    if not want or not cand:
        return 0.0
    return len(want & cand) / len(want)
