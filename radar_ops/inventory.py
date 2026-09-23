"""Inventaire LECTURE SEULE du contenu de `/data` : fichiers JSON et bases SQLite.

Deux précautions qui ne sont pas du confort :

- les bases sont ouvertes en `mode=ro` par URI, jamais par chemin : une
  ouverture normale CRÉE un fichier vide si le chemin n'existe pas, et ce
  module ne doit rien créer sur le volume de production ;
- `SELECT COUNT(*)` n'est pas lancé sur une base volumineuse. Le référentiel
  Discogs local compte plusieurs millions de lignes : le compte exact est un
  parcours complet de table qui bloquerait la page pour une information
  d'appoint.
"""
import json
import os
import sqlite3
import time

MAX_PARSE_B = 8 * 1024 * 1024          # au-delà, on ne parse pas le JSON
MAX_COUNT_B = 200 * 1024 * 1024        # au-delà, on ne compte pas les lignes
_JSON_EXT = ".json"
_SQLITE_EXT = ".sqlite3"

# Résultat d'un parsage mémorisé sous (mtime_ns, taille) : la page se
# rafraîchit souvent et relire un cache de 8 Mo à chaque affichage serait
# payé par l'utilisateur sans rien lui apprendre de neuf.
_parse_cache = {}


def _walk(root, suffix):
    for dirpath, _, filenames in os.walk(root):
        for fn in sorted(filenames):
            if fn.endswith(suffix):
                full = os.path.join(dirpath, fn)
                yield full, os.path.relpath(full, root)


def _stat(path):
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime, st.st_mtime_ns
    except OSError:
        return None


def _parse_shape(path, size_b, sig):
    hit = _parse_cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            shape = ("liste", len(data))
        elif isinstance(data, dict):
            shape = ("objet", len(data))
        else:
            shape = ("valeur", None)
    except (OSError, ValueError):
        shape = ("illisible", None)
    _parse_cache[path] = (sig, shape)
    return shape


def json_files(root, max_parse_b=MAX_PARSE_B):
    now, out = time.time(), []
    for full, rel in _walk(root, _JSON_EXT):
        st = _stat(full)
        if st is None:
            continue
        size_b, mtime, mtime_ns = st
        if size_b > max_parse_b:
            kind, count = "trop gros", None
        else:
            kind, count = _parse_shape(full, size_b, (mtime_ns, size_b))
        out.append({"path": rel, "size_b": size_b, "mtime": mtime,
                    "age_s": max(0.0, now - mtime), "kind": kind, "count": count})
    return out


def _table_rows(con, name, count_rows):
    if not count_rows:
        return None
    # Identifiant SQL : il vient de sqlite_master, pas de l'utilisateur, mais on
    # l'échappe quand même — une table nommée avec un guillemet casserait la requête.
    escaped = name.replace('"', '""')
    try:
        return con.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
    except sqlite3.Error:
        return None


def sqlite_files(root, max_count_b=MAX_COUNT_B):
    now, out = time.time(), []
    for full, rel in _walk(root, _SQLITE_EXT):
        st = _stat(full)
        if st is None:
            continue
        size_b, mtime, _ = st
        entry = {"path": rel, "size_b": size_b, "mtime": mtime,
                 "age_s": max(0.0, now - mtime), "tables": [], "error": None,
                 "counted": size_b <= max_count_b}
        con = None
        try:
            uri = "file:" + full.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=2.0)
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            entry["tables"] = [{"name": n, "rows": _table_rows(con, n, entry["counted"])}
                               for n in names]
        except sqlite3.Error as e:
            entry["error"] = str(e)
        finally:
            if con is not None:
                con.close()               # `with sqlite3.connect(...)` ouvre une
                                          # transaction, il ne ferme PAS la connexion
        out.append(entry)
    return out


def summary(root):
    js, sq = json_files(root), sqlite_files(root)
    return {"json": js, "sqlite": sq,
            "total_size_b": sum(x["size_b"] for x in js) + sum(x["size_b"] for x in sq)}
