"""Module pour générer des URLs de recherche Beatport.

Beatport ne dispose pas d'API publique officielle pour la recherche de pistes.
On se contente donc de construire l'URL de recherche web standard.
"""

from urllib.parse import quote_plus


def search_url(artist: str, title: str) -> str:
    """Renvoie l'URL de recherche Beatport pour un artiste et un titre.

    Args:
        artist: Nom de l'artiste.
        title: Titre du morceau.

    Returns:
        URL de recherche Beatport avec la requête encodée.
    """
    query = f"{artist} {title}".strip()
    encoded = quote_plus(query)
    return f"https://www.beatport.com/search?q={encoded}"
