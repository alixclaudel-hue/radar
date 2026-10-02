from urllib.parse import quote_plus


def search_url(artist: str, title: str) -> str:
    """
    Renvoie l'URL de recherche Traxsource pour un artiste et un titre.

    Traxsource ne propose pas d'API publique connue ; on construit donc
    l'URL de la page de recherche web classique.
    """
    query = f"{artist} {title}"
    encoded = quote_plus(query)
    return f"https://www.traxsource.com/search?term={encoded}"
