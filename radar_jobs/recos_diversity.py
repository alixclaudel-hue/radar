from typing import Any, Dict, Iterable, List, Optional, Union

DEFAULT_MAX_PER_ARTIST = 2
DEFAULT_MAX_PER_LABEL = 3


def norm_key(s: Optional[str]) -> str:
    '''Normalise une clé pour regroupement insensible à la casse et aux espaces.'''
    if s is None:
        return ''
    # Pourquoi split() sans argument : il élimine tous les espaces, tabulations
    # et retours à la ligne en une seule étape, ce qui normalise les variations.
    return ' '.join(str(s).strip().lower().split())


class DiversityTracker:
    '''Compteur de diversité artiste/label pour une file de lecture.'''

    def __init__(
        self,
        existing: Iterable[Dict[str, Any]] = (),
        max_per_artist: int = DEFAULT_MAX_PER_ARTIST,
        max_per_label: int = DEFAULT_MAX_PER_LABEL,
    ) -> None:
        self.max_per_artist = max_per_artist
        self.max_per_label = max_per_label
        self._artist_counts: Dict[str, int] = {}
        self._label_counts: Dict[str, int] = {}
        self.rejected_count = 0
        # Pourquoi compter dès l'initialisation : les éléments déjà présents
        # réduisent la place restante pour les mêmes artistes/labels.
        for item in existing:
            self.add(item.get('artist'), item.get('label'))

    def allows(self, artist: Optional[str], label: Union[str, Iterable[str], None]) -> bool:
        artist_key = norm_key(artist)
        # Pourquoi ignorer les clés vides : une absence d'information ne doit
        # pas limiter la diversité.
        if artist_key and self._artist_counts.get(artist_key, 0) >= self.max_per_artist:
            return False
        for label_key in self._normalize_labels(label):
            if label_key and self._label_counts.get(label_key, 0) >= self.max_per_label:
                return False
        return True

    def add(self, artist: Optional[str], label: Union[str, Iterable[str], None]) -> None:
        artist_key = norm_key(artist)
        if artist_key:
            self._artist_counts[artist_key] = self._artist_counts.get(artist_key, 0) + 1
        for label_key in self._normalize_labels(label):
            if label_key:
                self._label_counts[label_key] = self._label_counts.get(label_key, 0) + 1

    def reject(self) -> None:
        self.rejected_count += 1

    def _normalize_labels(self, label: Union[str, Iterable[str], None]) -> List[str]:
        if label is None:
            return []
        # Pourquoi distinguer str des itérables : un label peut être fourni
        # seul ou dans une collection sans API différente.
        if isinstance(label, str):
            return [norm_key(label)]
        try:
            return [norm_key(part) for part in label]
        except TypeError:
            return []


def dominant_reason(
    d_artist: Optional[float],
    d_label: Optional[float],
    d_style: Optional[float],
) -> Optional[str]:
    '''Renvoie la dimension dominante, en départageant les égalités dans l'ordre artist, label, style.'''
    best: Optional[str] = None
    best_score: Optional[float] = None
    for name, score in (('artist', d_artist), ('label', d_label), ('style', d_style)):
        if score is None:
            continue
        # Pourquoi ne pas remplacer à égalité : l'ordre artist, label, style
        # est une priorité métier volontaire.
        if best_score is None or score > best_score:
            best_score = score
            best = name
    return best
