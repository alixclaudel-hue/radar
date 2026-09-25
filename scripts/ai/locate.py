#!/usr/bin/env python3
"""Recherche locale bornée — le socle du mode `search`.

Le mode `search` du plan (§6) répond « localement par défaut, modèle sollicité
seulement pour désambiguïser ». Ce module porte la partie locale : il parcourt
l'arborescence, classe les correspondances et dit si le premier rang est
**contesté**. Aucune requête réseau, aucune exception qui remonte : un répertoire
illisible est un fichier de moins, pas un échec — même contrat que
[`context_cache`](context_cache.py:1) et [`health`](health.py:1).

Le classement est volontairement grossier et **déterministe** : pas de distance
d'édition, pas de score flottant. Le même arbre doit rendre la même liste dans le
même ordre d'un appel à l'autre, sinon la décision « ambigu ou non » dépendrait
du système de fichiers et le banc ne pourrait rien épingler. D'où le tri explicite
des dossiers et des fichiers à chaque niveau d'`os.walk`.

Deux bornes protègent l'appelant : le nombre de fichiers visités et le nombre de
correspondances conservées. Quand une borne mord, `truncated` est vrai — la
réponse reste exploitable, mais l'appelant sait qu'il regarde une fenêtre et non
l'arbre entier.
"""

from __future__ import annotations

import os

# Dossiers jamais parcourus : ils ne portent pas de source du projet et
# représentent l'essentiel du volume (dépôt git, dépendances, caches de tout
# poil). Les traverser ne coûterait pas un octet de réseau mais noierait les
# vraies correspondances sous du bruit — et ce bruit-là se paierait : on
# déclencherait un appel de désambiguïsation pour départager des caches.
DOSSIERS_IGNORES = frozenset({
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    ".next",
})

# Score par étage. Les valeurs sont largement espacées pour qu'aucun cumul ne
# fasse remonter une correspondance de contenu au-dessus d'une correspondance de
# nom : trouver un fichier *nommé* comme la requête est un signal bien plus fort
# que trouver le mot dans son texte, et l'inverse ferait désambiguïser à tort.
SCORE_NOM_EXACT = 100
SCORE_NOM_SANS_EXTENSION = 90
SCORE_NOM_CONTIENT = 70
SCORE_CHEMIN_CONTIENT = 50
SCORE_CONTENU = 30

MAX_FICHIERS_DEFAUT = 20000
MAX_HITS_DEFAUT = 20
MAX_CANDIDATS_DEFAUT = 8

# Au-delà, le fichier est ignoré pour la recherche **de contenu** : un dump de
# plusieurs mégaoctets se lit intégralement pour rien, alors qu'il ne peut
# contenir au mieux qu'une ligne de plus. La borne ne s'applique pas à la
# correspondance de nom, qui ne lit rien.
MAX_OCTETS_FICHIER = 2_000_000

# Longueur maximale du texte de ligne cité dans une correspondance de contenu :
# de quoi juger la pertinence, pas de quoi recopier le fichier dans le prompt.
MAX_TEXTE_LIGNE = 200


def _nettoyer(chemin: str) -> str:
    """`normpath` — un chemin rendu deux fois doit être comparable tel quel."""
    return os.path.normpath(chemin)


def _score_nom(chemin: str, requete: str) -> tuple[int, str] | None:
    """Meilleur étage de correspondance sur le *nom* du fichier, ou `None`.

    Quatre étages, du plus sûr au plus large : le nom exact, puis le nom privé de
    son extension (chercher `quota` doit trouver `quota.py`), puis le nom qui
    contient la requête, puis le chemin entier. Ce dernier étage sert aux
    requêtes qui visent un dossier autant qu'un fichier (`scripts/ai`).
    """
    base = os.path.basename(chemin).lower()
    radical = os.path.splitext(base)[0]
    attribut = requete.lower()
    if base == attribut:
        return SCORE_NOM_EXACT, "nom-exact"
    if radical == attribut:
        return SCORE_NOM_SANS_EXTENSION, "nom-sans-extension"
    if attribut in base:
        return SCORE_NOM_CONTIENT, "nom-contient"
    if attribut in chemin.lower():
        return SCORE_CHEMIN_CONTIENT, "chemin-contient"
    return None


def _ligne_contenu(chemin: str, requete: str) -> tuple[int, str] | None:
    """Numéro et texte de la **première** ligne qui contient la requête.

    Une seule ligne est citée par fichier : au-delà, on ne désambiguïse plus, on
    recopie. Les fichiers illisibles, binaires ou trop gros rendent `None` sans
    bruit — c'est le cas normal d'un arbre qui contient aussi des images.
    """
    try:
        if os.path.getsize(chemin) > MAX_OCTETS_FICHIER:
            return None
    except OSError:
        return None
    try:
        with open(chemin, "r", encoding="utf-8", errors="replace") as handle:
            for numero, ligne in enumerate(handle, 1):
                if requete in ligne:
                    return numero, ligne.strip()[:MAX_TEXTE_LIGNE]
    except OSError:
        return None
    return None


def chercher(
    requete: str,
    roots: list[str] | None = None,
    *,
    max_hits: int = MAX_HITS_DEFAUT,
    max_fichiers: int = MAX_FICHIERS_DEFAUT,
    contenu: bool = True,
) -> dict:
    """Correspondances de `requete` sous `roots`, classées — ne lève jamais.

    Rend `{"query", "roots", "scanned", "truncated", "hits"}`. `hits` est une
    liste d'objets `{"path", "score", "kind", "line", "text"}` triés par score
    décroissant, puis par chemin croissant, puis par numéro de ligne : deux
    exécutions sur le même arbre rendent la même liste, ce qui est la condition
    pour qu'« ambigu » veuille dire quelque chose.

    Les chemins sont normalisés (`normpath`) mais **non absolus** : c'est la
    graphie que l'opérateur a donnée qui revient, et un appelant qui rouvre le
    chemin retenu travaille depuis le même répertoire que la recherche.
    """
    requete = (requete or "").strip()
    racines = [_nettoyer(racine) for racine in (roots or ["."])]
    resultat = {
        "query": requete,
        "roots": list(racines),
        "scanned": 0,
        "truncated": False,
        "hits": [],
    }
    if not requete:
        return resultat

    trouves: dict[str, dict] = {}
    for racine in racines:
        if not os.path.isdir(racine):
            continue
        for dossier, sous_dossiers, fichiers in os.walk(racine, followlinks=False):
            # `os.walk` ne promet aucun ordre : sans ce tri, deux machines
            # pourraient rendre deux classements différents à scores égaux.
            sous_dossiers[:] = sorted(
                nom for nom in sous_dossiers if nom not in DOSSIERS_IGNORES
            )
            for nom in sorted(fichiers):
                if resultat["scanned"] >= max_fichiers:
                    resultat["truncated"] = True
                    break
                resultat["scanned"] += 1
                chemin = _nettoyer(os.path.join(dossier, nom))
                numero: int | None = None
                texte: str | None = None
                trouve = _score_nom(chemin, requete)
                if trouve is None and contenu:
                    ligne = _ligne_contenu(chemin, requete)
                    if ligne is not None:
                        trouve = (SCORE_CONTENU, "contenu")
                        numero, texte = ligne
                if trouve is None:
                    continue
                score, genre = trouve
                # Un fichier n'apparaît qu'une fois : le meilleur étage gagne.
                # Sans cela, un fichier qui contient dix fois le mot pousserait
                # dix lignes dans la liste et paraîtrait dix fois plus ambigu.
                if chemin in trouves:
                    continue
                trouves[chemin] = {
                    "path": chemin,
                    "score": score,
                    "kind": genre,
                    "line": numero,
                    "text": texte,
                }
            if resultat["truncated"]:
                break
        if resultat["truncated"]:
            break

    resultat["hits"] = sorted(
        trouves.values(),
        key=lambda hit: (-hit["score"], hit["path"], hit["line"] or 0),
    )[: max(0, int(max_hits))]
    return resultat


def ambigue(resultat: dict, *, max_candidats: int = MAX_CANDIDATS_DEFAUT) -> list[dict] | None:
    """Candidats à départager, ou `None` si le premier rang est seul.

    « Ambigu » ne veut pas dire « plusieurs résultats » : une recherche qui
    trouve trois fois le mot dans le **même** fichier n'a rien à demander à
    personne. L'ambiguïté commence quand **deux fichiers distincts** partagent le
    score du premier rang — là seulement, la réponse locale ne peut pas trancher
    et un appel se justifie. C'est toute l'économie du mode : zéro requête tant
    que le disque répond seul.
    """
    hits = resultat.get("hits") or []
    if not hits:
        return None
    meilleur = hits[0]["score"]
    tete = [hit for hit in hits if hit["score"] == meilleur]
    if len({hit["path"] for hit in tete}) < 2:
        return None
    return tete[: max(2, int(max_candidats))]


def meilleur(resultat: dict) -> dict | None:
    """Correspondance de tête, ou `None` si la recherche n'a rien trouvé."""
    hits = resultat.get("hits") or []
    return hits[0] if hits else None


def prompt_desambiguisation(
    resultat: dict,
    candidats: list[dict],
    *,
    requete: str = "",
) -> str:
    """Prompt du **seul** appel du mode `search` : choisir dans une liste fermée.

    La liste part entière avec, pour chaque candidat, l'étage de correspondance
    et la ligne citée quand il y en a une : le modèle n'a pas à parcourir le
    dépôt, il a à lire huit lignes et désigner la bonne. Un prompt qui laisserait
    le modèle chercher lui-même coûterait un aller-retour de plus pour une
    réponse moins vérifiable.
    """
    lignes = [
        f"Requête de recherche : {requete or resultat.get('query', '')}",
        "",
        "Candidats trouvés localement, du plus proche au moins proche :",
    ]
    for rang, hit in enumerate(candidats, 1):
        detail = ""
        if hit.get("line"):
            detail = f" — ligne {hit['line']} : {hit.get('text') or ''}"
        lignes.append(f"{rang}. {hit['path']} [{hit['kind']}]{detail}")
    lignes.append("")
    lignes.append(
        "Désigne l'unique fichier qui répond le mieux à la requête. "
        'Réponds par un unique objet JSON, sans texte avant ni après : '
        '{"path": "<chemin copié exactement depuis la liste>", "reason": "<une phrase>"}. '
        "Le chemin doit être copié caractère pour caractère depuis la liste : "
        "aucun chemin inventé, aucun chemin hors liste."
    )
    return "\n".join(lignes)


__all__ = [
    "DOSSIERS_IGNORES",
    "MAX_CANDIDATS_DEFAUT",
    "MAX_FICHIERS_DEFAUT",
    "MAX_HITS_DEFAUT",
    "SCORE_CHEMIN_CONTIENT",
    "SCORE_CONTENU",
    "SCORE_NOM_CONTIENT",
    "SCORE_NOM_EXACT",
    "SCORE_NOM_SANS_EXTENSION",
    "ambigue",
    "chercher",
    "meilleur",
    "prompt_desambiguisation",
]
