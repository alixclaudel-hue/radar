"""Règles partagées sur les pistes et sorties Discogs : artistes placeholder,
mixes continus, crédit d'artiste par piste, clé d'identité d'une sortie.
"""

import re

from radar_jobs.common import style_key


_PLACEHOLDER_ARTISTS = {"various", "various artists", "va", "v/a", "unknown artist",
                        "unknown", "no artist"}


def _is_placeholder_artist(name):
    """Un placeholder Discogs ("Various", "Unknown Artist", "No Artist"...) ne
    vaut pas mieux qu'une absence de crédit. Set DÉDIÉ, volontairement distinct
    d'ARTIST_STOPWORDS (graphe labels/artistes) — un essai de fusion le 15/09
    a été annulé après vérif VPS : ARTIST_STOPWORDS contient aussi "release"/
    "progressive classics"/"traxsource", du bruit propre au graphe co-crédits,
    pas des marqueurs "sans artiste" — un vrai artiste Discogs nommé "Release"
    (release réelle, ex. "Release (22) — Walk Away") se serait retrouvé écarté
    à tort des candidats RECOS. Suffixe de désambiguïsation Discogs '(N)'
    retiré avant comparaison (`_strip_discogs_suffix`, existait déjà pour un
    autre usage) : diagnostic VPS 2026-09-15, 137 pistes 'No Artist' (111),
    'Various Artists (N)' (14), 'Unknown Artist (N)' (8), 'Unknown' (3)
    passaient au travers de l'ancien set + suffixe jamais retiré."""
    n = _strip_discogs_suffix((name or "").strip()).casefold()
    return n in _PLACEHOLDER_ARTISTS


_CONTINUOUS_MIX_PATTERNS = (
    "continuous mix", "continuous dj mix", "continuous dj-mix",
    "full continuous mix", "dj mix", "megamix",
)


def _is_continuous_mix(title):
    """Megamix/DJ mix continu d'une sortie (une seule "piste" de 30-60min sans
    artiste unique identifiable, ex. "(Continuous DJ Mix)", "(Full Continuous
    Mix)", "Megamix" — 430+83 titres en base owner, diagnostic VPS 2026-09-15) :
    n'a rien à faire comme candidat RECOS RADAR. Ne PAS réutiliser dans
    scoring.real_tracks() : partagée avec la page sortie (app.py) où
    l'utilisateur veut voir la piste megamix listée."""
    t = (title or "").lower()
    return any(p in t for p in _CONTINUOUS_MIX_PATTERNS)


def _track_credit_artist(t, fallback):
    """Artiste réel d'une piste Discogs : `tracklist[].artists` (crédit par piste,
    rempli seulement quand il diffère de l'artiste de la sortie — cas des
    compilations « Various ») sinon `fallback` (artiste de la sortie). Corrige la
    recherche YouTube RECOS RADAR qui recevait "Various" comme artiste — pénalisé
    à tort par ytcache._best_match (mot absent des métadonnées vidéo), rejetant
    quasi toutes les pistes de compilation (retour utilisateur 2026-09-10).

    Un placeholder ("Various"...) est traité comme une absence de crédit, que
    ce soit CAS A (Discogs ne fournit aucun crédit par piste, ex. release
    17828710 : le fallback release est alors lui-même "Various") ou CAS B (une
    piste précise est littéralement créditée "Various" au sein d'une sortie
    par ailleurs bien créditée, ex. release 16062985 piste 1 = megamix,
    pistes 2-8 = vrais artistes) — diagnostic VPS 2026-09-15. Retourne ""
    plutôt que le placeholder, à charge de l'appelant d'écarter une piste sans
    artiste exploitable."""
    arts = [(a.get("anv") or a.get("name") or "").strip() for a in (t.get("artists") or [])]
    arts = [a for a in arts if a and not _is_placeholder_artist(a)]
    if arts:
        return ", ".join(arts)
    return "" if _is_placeholder_artist(fallback) else (fallback or "")


def _strip_discogs_suffix(name):
    """Retire les suffixes de désambiguïsation Discogs ("iO (12)", "The Vision (16)",
    "Rhythm (2)") d'un crédit, artiste par artiste. Ces numéros n'apparaissent dans
    aucune métadonnée YouTube : ils polluent la requête et pénalisent le scoring de
    ytcache._best_match. Appliqué à la recherche seulement — l'identité de piste
    stockée pour le dédoublonnage garde le nom Discogs d'origine."""
    parts = [re.sub(r"\s*\(\d+\)\s*$", "", p).strip() for p in (name or "").split(",")]
    return ", ".join(p for p in parts if p)


def _release_identity_key(artist, title):
    """Identité d'une SORTIE ("artiste||titre" normalisés), sur le même principe
    que l'identité de PISTE du dédoublonnage RECOS (cf. CLAUDE.md point 19).
    Sert à reconnaître un disque déjà possédé même sous un autre pressage : la
    collection Discogs porte un release_id précis, la découverte RECOS peut en
    remonter un autre (repress, édition CD/vinyle, réédition) pour le même
    album. Le suffixe de désambiguïsation Discogs ("Rhythm (2)") est retiré de
    l'artiste, jamais du titre (un titre peut légitimement finir par "(2)")."""
    a = style_key(_strip_discogs_suffix(artist))
    t = style_key(title)
    # les deux moitiés sont exigées : une clé à artiste vide rapprocherait deux
    # disques homonymes sans crédit, un faux positif exclurait une reco à tort.
    return f"{a}||{t}" if a and t else ""
