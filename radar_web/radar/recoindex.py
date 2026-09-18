"""Cache disque de `Ctx.reco_index` — {label_key: score de reco}, PAR UTILISATEUR.

Pourquoi (brief VPS du 18/09, M1 couche 3) : le calcul complet met 183,5 s à
froid sur les données réelles (465 284 labels classés), et il est payé par la
PREMIÈRE recherche qui suit chaque redémarrage du conteneur. Les couches 1 et 2
du brief règlent la mémoire, pas ce temps-là.

Même modèle que `scorestore.py` (Lot 4) : construit par le worker hors de tout
chemin HTTP, relu tel quel par les requêtes. Deux différences assumées :

- **JSON, pas SQLite** : `reco_index` est consommé comme un vrai `dict` par
  `album_score`, `_base_labels_ranked` et la page graphe de labels, et le tri de
  `/univers/labels/table` en a besoin en entier. 12 à 20 Mo relus d'un coup
  coûtent moins qu'une couche d'accès paresseuse à réécrire partout.
- **Deux fichiers** : l'index et son méta (signature, horodatage, taille).
  Le worker doit pouvoir dire « ce cache est-il périmé ? » sans parser les
  20 Mo — il ne lit alors que le méta, comme `catalog_labelgraph_meta.json`.

Invalidation : la signature stockée est celle de `scoring.derived_key(uid, cfg)`,
exactement la clé du cache mémoire `_DERIVED`. Tout ce dont l'index dépend y
entre (config de goût, collection, corpus, graphe producteur, profils de labels,
référentiel Discogs, graphe catalogue) ; un cache produit sous une autre
signature n'est jamais servi.

Le cache est un RACCOURCI, jamais une dépendance : `Ctx._compute_reco_index`
retombe sur le calcul en ligne s'il est absent, périmé ou illisible. Une
première installation, ou un réglage de goût qui vient de changer, est ralentie,
jamais cassée.
"""
import hashlib
import json
import os
from datetime import datetime

from . import paths


def index_path(uid):
    return os.path.join(paths.user_paths(uid).dir, "reco_index.json")


def meta_path(uid):
    return os.path.join(paths.user_paths(uid).dir, "reco_index_meta.json")


def sig_of(key):
    """Empreinte courte de `scoring.derived_key(...)` — le tuple contient des
    mtimes en nanosecondes, illisibles et inutiles à conserver tels quels."""
    return hashlib.sha1(repr(key).encode()).hexdigest()


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def read_meta(uid):
    """{'sig', 'built_at', 'n'} ou None. Lecture volontairement minuscule :
    c'est ce que le worker interroge à chaque tour de boucle."""
    meta = _read_json(meta_path(uid))
    return meta if isinstance(meta, dict) else None


def is_fresh(uid, key):
    """True si un cache existe ET a été produit sous cette signature."""
    meta = read_meta(uid)
    return bool(meta) and meta.get("sig") == sig_of(key) and os.path.exists(index_path(uid))


def load(uid, key):
    """{label_key: score} si le cache correspond à `key`, sinon None (l'appelant
    recalcule). None aussi sur fichier tronqué ou illisible : un cache douteux
    ne doit jamais l'emporter sur un calcul juste."""
    if not is_fresh(uid, key):
        return None
    data = _read_json(index_path(uid))
    return data if isinstance(data, dict) else None


def save(uid, key, index):
    """Écrit l'index puis son méta, chacun par fichier temporaire + `os.replace`
    (atomique) : le worker écrit pendant que le conteneur web lit. Le méta est
    écrit EN DERNIER — un index à moitié remplacé n'est donc jamais annoncé
    comme valide, et le pire cas est un cache déclaré périmé à tort."""
    P = paths.user_paths(uid)
    os.makedirs(P.dir, exist_ok=True)
    ipath, mpath = index_path(uid), meta_path(uid)
    tmp = ipath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, separators=(",", ":"))
    os.replace(tmp, ipath)
    meta = {"sig": sig_of(key), "built_at": datetime.now().isoformat(timespec="seconds"),
            "n": len(index)}
    tmp = mpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    os.replace(tmp, mpath)
    return meta
