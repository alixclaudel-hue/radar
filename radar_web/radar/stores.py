"""Liens de recherche vers les boutiques DJ protégées par Cloudflare
(Beatport, Traxsource).

Pourquoi ce module ne scrape PAS
--------------------------------
Beatport et Traxsource sont derrière Cloudflare. Un scraping côté serveur
depuis le VPS échoue systématiquement : Cloudflare voit une IP de datacenter
(ASN hébergeur), un navigateur headless aux empreintes TLS/JA3 typiques de
Playwright, et un rythme de requêtes non humain. Le challenge est alors
inévitable — aucun modèle de langage n'y change rien, un LLM ne navigue pas et
ne résout pas un challenge Cloudflare.

Le besoin réel n'est pas d'extraire des données mais de **générer une
recherche pour acheter une track**. On construit donc une URL de recherche
publique et on redirige le **navigateur de l'utilisateur** (IP résidentielle,
session et cookies Cloudflare déjà validés) : c'est un humain qui clique, la
protection ne bloque rien. Même pattern que `bandcamp.search_url()` et
`scoring.yt_search_url()`, consommé par les routes `/bc/go` et `/yt/first`.

Aucun appel réseau ici : uniquement de la construction d'URL, donc testable
hors ligne et sans dépendance.
"""
from urllib.parse import quote_plus

# Beatport : la recherche accepte `?q=` (le frontend moderne route vers
# /search?q=). On y ajoute le label quand il est connu — il désambiguïse les
# remixes homonymes, qui sont légion sur les tracks house/techno.
BEATPORT_SEARCH = "https://www.beatport.com/search"

# Traxsource : même convention `?q=`.
TRAXSOURCE_SEARCH = "https://www.traxsource.com/search"


def _query(artist, title, label):
    """Concatène artiste / titre / label en une requête, en ignorant les vides.

    L'ordre artiste → titre → label est celui qui donne les meilleurs résultats
    sur les deux moteurs : le titre est le signal le plus discriminant, mais
    l'artiste en tête évite les collisions de titres génériques (« Untitled »,
    « Groove »…)."""
    parts = [(artist or "").strip(), (title or "").strip(), (label or "").strip()]
    return " ".join(p for p in parts if p)


def beatport_search_url(artist="", title="", label=""):
    """URL de recherche Beatport pour (artiste, titre, label).

    Renvoie toujours une URL exploitable : si les trois champs sont vides, on
    renvoie la page de recherche nue plutôt qu'une URL `?q=` vide."""
    q = _query(artist, title, label)
    return f"{BEATPORT_SEARCH}?q={quote_plus(q)}" if q else BEATPORT_SEARCH


def traxsource_search_url(artist="", title="", label=""):
    """URL de recherche Traxsource pour (artiste, titre, label). Cf.
    `beatport_search_url` pour la construction de la requête."""
    q = _query(artist, title, label)
    return f"{TRAXSOURCE_SEARCH}?q={quote_plus(q)}" if q else TRAXSOURCE_SEARCH
