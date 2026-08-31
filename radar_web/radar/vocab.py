"""Vocabulaire canonique Discogs pour la recherche ciblée.

GENRES : l'ensemble fixe des genres Discogs.
STYLES : sélection canonique orientée crate-digging (l'API Discogs attend
l'orthographe exacte). Les styles des catégories de goût de l'utilisateur
sont ajoutés dynamiquement dans la route /search.
"""

GENRES = [
    "Electronic", "Rock", "Pop", "Jazz", "Funk / Soul", "Hip Hop", "Classical",
    "Reggae", "Latin", "Blues", "Folk, World, & Country", "Non-Music",
    "Stage & Screen", "Brass & Military", "Children's",
]

STYLES = [
    "House", "Deep House", "Tech House", "Progressive House", "Garage House",
    "Chicago House", "Italo-House", "Hip-House", "Ghetto House", "Acid House",
    "Minimal", "Techno", "Detroit Techno", "Dub Techno", "Acid", "Electro",
    "Electro House", "Disco", "Italo-Disco", "Nu-Disco", "Boogie", "Funk",
    "Soul", "Jazz-Funk", "Fusion", "Future Jazz", "Broken Beat", "Downtempo",
    "Trip Hop", "Ambient", "Leftfield", "Dub", "Drum n Bass", "Jungle",
    "Breakbeat", "Breaks", "UK Garage", "Bass Music", "Grime", "Synth-pop",
    "New Wave", "EBM", "Afro", "Balearic", "Trance", "Prog Rock",
    "Experimental", "Abstract",
]
