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


# Seuil de recouvrement (artiste+titre vs titre de la vidéo) au-dessus duquel une
# vidéo attachée à une sortie Discogs est considérée comme étant CETTE piste.
# Valeur d'origine de la tracklist de /search (bouton play), reprise telle
# quelle par la playlist RECOS RADAR : une vidéo mal appariée est pire que pas
# de vidéo (même principe que ytcache.MIN_MATCH_SCORE). Les DEUX appelants
# actuels de `best_video_uri` (`app.py::tracklist`, `crate_jobs.py::
# _discogs_release_video`) passent `min_overlap=0` pour le DÉSACTIVER — cf.
# note sous `best_video_uri` — il ne reste donc utile qu'à un futur appelant
# qui comparerait des vidéos non garanties déjà rattachées à la bonne sortie
# (ex. un résultat de recherche YouTube), là où l'artiste redevient le seul
# signal qui distingue un homonyme.
VIDEO_MATCH_MIN = 0.55
# Le titre de la PISTE doit être couvert à lui seul : une sortie porte une
# poignée de vidéos pour des pistes différentes, toutes du même artiste. Sans
# cette condition, « Inland Knights — Inconnue » matchait la vidéo « Inland
# Knights — 12 Till 8 » (2 jetons partagés sur 3, tous venant de l'artiste) et
# héritait de la vidéo d'une AUTRE piste. Relevé de 0.5 à 0.6 (diagnostic VPS
# 2026-09-18) pour compenser la désactivation de VIDEO_MATCH_MIN ci-dessus :
# c'est désormais la SEULE barrière contre un homonyme de piste.
VIDEO_TITLE_MATCH_MIN = 0.6
# Jetons qui signalent une version DIFFÉRENTE de la piste demandée (remix, dub,
# edit…). Une vidéo qui en porte un que le titre de la piste ne contient pas
# ne doit jamais l'emporter, même si elle couvre le titre à 100% — sinon une
# piste "Believe" hérite de la vidéo "Believe (Dub)" faute d'alternative
# (`overlap` est asymétrique : les jetons EN TROP de la vidéo ne pénalisent pas
# la couverture). Diagnostic VPS 2026-09-18, garde-fou explicite plutôt que de
# compter sur le hasard d'un seuil.
_VERSION_MARKER_TOKENS = {"dub", "remix", "instrumental", "edit", "live"}


def best_video_uri(videos, artist, title, min_overlap=VIDEO_MATCH_MIN,
                    min_title_overlap=VIDEO_TITLE_MATCH_MIN):
    """URL de la vidéo qui correspond le mieux à `artist` + `title` parmi les
    vidéos attachées à une sortie Discogs (`release["videos"]`), ou "" si aucune
    ne satisfait les seuils.

    Partagé par la tracklist de /search (bouton play) et par la playlist RECOS
    RADAR, qui cherchent la même chose : Discogs a-t-il déjà la vidéo de cette
    piste, avant de dépenser une recherche YouTube ? Les entrées sans `uri` sont
    ignorées, une liste vide ou `None` renvoie "". Un titre vide ne peut rien
    apparier : sans titre, rien ne distingue les pistes d'une même sortie.

    `min_overlap=0` désactive le seuil global (diagnostic VPS 2026-09-18) : les
    vidéos examinées sont déjà rattachées à LA bonne sortie, donc au bon
    artiste — l'uploadeur répète rarement le nom de l'artiste dans le titre
    (la page Discogs le porte déjà), parfois le remplace par le label ou un
    alias, ce qui plafonnait mécaniquement le recouvrement global bien en
    dessous de VIDEO_MATCH_MIN pour des vidéos par ailleurs parfaites (mesuré :
    7 sorties sur 7 rejetées à tort sur un échantillon réel). Le nom de
    l'artiste n'apporte donc rien sur ce chemin, seule la couverture du titre
    de la piste (`min_title_overlap`) et le garde-fou version ci-dessous
    protègent contre un mauvais appariement."""
    a_toks = toks(artist)
    want = toks(f"{artist or ''} {title or ''}")
    t_toks = toks(title)
    best, best_sc = "", 0.0
    for v in videos or []:
        uri = (v or {}).get("uri")
        if not uri:
            continue
        v_toks = toks(v.get("title"))
        if overlap(t_toks, v_toks) < min_title_overlap:
            continue
        if (v_toks - t_toks - a_toks) & _VERSION_MARKER_TOKENS:
            continue
        sc = overlap(want, v_toks)
        if sc > best_sc:
            best, best_sc = uri, sc
    return best if best_sc >= min_overlap else ""
