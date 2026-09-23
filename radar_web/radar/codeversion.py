"""Version du code en cours d'exécution.

Sert à estampiller un précalcul : savoir que des scores ont été produits par
une version antérieure du code explique une différence de résultat qu'aucune
comparaison de données ne révélerait.

`.git` est exclu de l'image Docker (`.dockerignore`), donc en production la
valeur vient de `RADAR_CODE_SHA`, posée au déploiement. Le repli sur `.git`
n'existe que pour le développement local et les sessions cloud, où la variable
n'est pas définie mais le dépôt est là.
"""
import os

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _from_git():
    head = os.path.join(_REPO, ".git", "HEAD")
    try:
        with open(head, encoding="utf-8") as f:
            ref = f.read().strip()
    except OSError:
        return None
    if not ref.startswith("ref:"):
        return ref                       # HEAD détaché : le SHA est écrit tel quel
    path = os.path.join(_REPO, ".git", ref.split(" ", 1)[1].strip())
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def sha():
    """SHA complet, ou None si la version est indéterminable. Jamais une valeur
    inventée : « inconnu » est une information, un faux SHA est un piège."""
    return os.environ.get("RADAR_CODE_SHA") or _from_git() or None


def short():
    s = sha()
    return s[:7] if s else None
