"""Graphe label <-> label GLOBAL, construit sur le référentiel Discogs
PARTAGÉ (`discogs_dump.py`) — sur TOUT le catalogue importé, indépendamment
du goût ou du corpus d'un utilisateur quelconque. Rien à voir avec
`labelgraph.py` (sous-graphe visuel PAR UTILISATEUR, dérivé des graines de
goût via `producer_graph.json` que construit `job_build_graph`) : un seul
graphe ici, partagé par tous les utilisateurs, reconstruit uniquement quand
le dump mensuel change (Lot 2 de la refonte scoring, cf. CLAUDE.md point 38).

Deux origines de lien entre deux labels, distinguées par la colonne `kind` de
`label_pairs` (jamais mélangées dans le même total) :
- `"artist"` : les deux labels partagent au moins un artiste crédité (toutes
  sorties confondues) — lien NON dirigé, `weight` = nombre d'artistes
  distincts qui les relient tous les deux, `a`/`b` triés alphabétiquement.
- `"parent"` : relation d'appartenance déclarée explicitement par Discogs
  lui-même (`<sublabels>` du dump labels.xml, cf. `discogs_dump.import_labels`
  — PAS un genre/style, ce champ n'existe pas dans labels.xml, cf. échange du
  11/09) — lien DIRIGÉ, `a` = label enfant, `b` = label parent, toujours,
  `weight` = 1 (fait administratif, pas un comptage). `neighbors()` restitue
  ce sens via son champ `role`.

Stocké dans son propre fichier SQLite sous SHARED_DIR (pas dans
`discogs_dump.sqlite3` lui-même) : la reconstruction peut ainsi être
interrompue ou relancée indépendamment de l'import du dump, sans jamais
toucher au référentiel Discogs pendant qu'il sert des lectures. Même
principe de bascule atomique que `discogs_dump.py` (`.new` puis
`os.replace`) : l'ancien graphe reste lisible jusqu'à la dernière seconde.
"""
import itertools
import os
import sqlite3
from collections import Counter

from . import paths
from .store import load, save

DB_PATH = os.path.join(paths.SHARED_DIR, "catalog_labelgraph.sqlite3")
META_PATH = os.path.join(paths.SHARED_DIR, "catalog_labelgraph_meta.json")

# Un artiste crédité sur un nombre déraisonnable de labels distincts (ex.
# alias générique mal résolu, ou carrière de session-man sur des décennies)
# générerait C(n,2) paires pour une seule ligne d'entrée — coût quadratique
# sans signal supplémentaire utile. Même logique de garde-fou que
# `scoring.DEFAULT_SCORING["graph"]["max_credits"]`/`node_cap` côté graphe
# par-utilisateur : on saute l'artiste plutôt que de le tronquer au hasard.
MAX_LABELS_PER_ARTIST = 40
FLUSH_EVERY = 20_000  # artistes traités entre deux écritures de l'accumulateur

KIND_ARTIST = "artist"
KIND_PARENT = "parent"


def get_meta():
    return load(META_PATH, {})


def save_meta(d):
    save(META_PATH, d)


def available():
    return os.path.exists(DB_PATH)


def needs_rebuild(dump_meta):
    """True si `dump_meta` (le meta.json de discogs_dump, dump courant)
    décrit un dump différent de celui sur lequel ce graphe a été construit
    la dernière fois — ou si le graphe n'existe pas encore alors qu'un dump,
    lui, est disponible."""
    dump_date = (dump_meta or {}).get("dump_date")
    if not dump_date:
        return False
    return get_meta().get("dump_date") != dump_date


def _create_schema(con):
    con.execute("""
        CREATE TABLE label_pairs (
            a TEXT NOT NULL,
            b TEXT NOT NULL,
            kind TEXT NOT NULL,
            weight INTEGER NOT NULL,
            PRIMARY KEY (a, b, kind)
        )
    """)


def _create_indexes(con):
    # PRIMARY KEY (a, b, kind) indexe déjà les recherches par `a` (et par
    # `a`+`kind`) ; `b` a besoin de son propre index pour qu'une recherche
    # "quels labels sont liés à X" (X peut être en 1re ou 2e position,
    # notamment pour kind="parent" où la position porte le sens enfant/parent)
    # ne scanne jamais la table entière.
    con.execute("CREATE INDEX IF NOT EXISTS idx_label_pairs_b ON label_pairs(b)")


def _open_new_db():
    os.makedirs(paths.SHARED_DIR, exist_ok=True)
    new_path = DB_PATH + ".new"
    for p in (new_path, new_path + "-wal", new_path + "-shm"):
        try:
            os.remove(p)
        except OSError:
            pass
    con = sqlite3.connect(new_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-131072")  # 128 Mo
    _create_schema(con)
    return con


def _finalize_new_db(con):
    _create_indexes(con)
    con.execute("ANALYZE")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    new_path = DB_PATH + ".new"
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(new_path + suffix)
        except OSError:
            pass
    os.replace(new_path, DB_PATH)


def _flush(con, counter):
    """`counter` : {(a, b, kind): poids}."""
    if not counter:
        return
    con.executemany(
        "INSERT INTO label_pairs(a, b, kind, weight) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(a, b, kind) DO UPDATE SET weight = weight + excluded.weight",
        [(a, b, kind, n) for (a, b, kind), n in counter.items()])
    con.commit()
    counter.clear()


def _build_artist_edges(dump_con, out_con, progress_cb, stop_cb, max_labels_per_artist, flush_every):
    """Parcourt `release_artists` JOIN `releases` (requête triée par
    `artist_id`, donc chaque artiste n'est vu qu'une fois d'affilée), et
    compte pour chaque paire de labels qu'il relie combien d'artistes
    distincts les relient tous les deux -> arêtes kind="artist"."""
    counter = Counter()
    n_used, n_skipped = 0, 0
    cur = dump_con.execute("""
        SELECT ra.artist_id, r.label_key
        FROM release_artists ra
        JOIN releases r ON r.id = ra.release_id
        WHERE r.label_key IS NOT NULL AND r.label_key != ''
        ORDER BY ra.artist_id
    """)
    current_artist, labels_seen = None, set()

    def _process(labels):
        nonlocal n_used, n_skipped
        if len(labels) < 2:
            return
        if len(labels) > max_labels_per_artist:
            n_skipped += 1
            return
        n_used += 1
        for a, b in itertools.combinations(sorted(labels), 2):
            counter[(a, b, KIND_ARTIST)] += 1

    since_flush = 0
    for artist_id, label_key in cur:
        if artist_id != current_artist:
            if current_artist is not None:
                _process(labels_seen)
                since_flush += 1
                if since_flush >= flush_every:
                    _flush(out_con, counter)
                    since_flush = 0
                    if progress_cb:
                        progress_cb(n_used + n_skipped)
                    if stop_cb and stop_cb():
                        raise InterruptedError("stop demandé pendant la construction du graphe labels")
            current_artist, labels_seen = artist_id, set()
        labels_seen.add(label_key)
    if current_artist is not None:
        _process(labels_seen)
    _flush(out_con, counter)
    return n_used, n_skipped


def _build_parent_edges(dump_con, out_con):
    """Relation d'appartenance label enfant -> label parent, déclarée
    explicitement par Discogs (`<sublabels>` de labels.xml, déjà résolue en
    `labels.parent_id` par `discogs_dump.import_labels`) -> arêtes
    kind="parent", dirigées (a=enfant, b=parent, weight=1 toujours — fait
    administratif, pas un comptage)."""
    counter = Counter()
    rows = dump_con.execute("""
        SELECT c.name_key, p.name_key
        FROM labels c
        JOIN labels p ON c.parent_id = p.id
        WHERE c.parent_id IS NOT NULL AND c.name_key IS NOT NULL AND p.name_key IS NOT NULL
    """)
    n_edges = 0
    for child_key, parent_key in rows:
        if not child_key or not parent_key or child_key == parent_key:
            continue
        counter[(child_key, parent_key, KIND_PARENT)] += 1
        n_edges += 1
    _flush(out_con, counter)
    return n_edges


def build(dump_con=None, progress_cb=None, stop_cb=None,
          max_labels_per_artist=MAX_LABELS_PER_ARTIST, flush_every=FLUSH_EVERY):
    """Reconstruit entièrement `label_pairs` (arêtes "artist" puis "parent",
    cf. docstring du module) à partir du référentiel Discogs partagé (déjà
    importé, cf. `discogs_dump.py`).

    `dump_con` : connexion déjà ouverte sur discogs_dump.sqlite3 (repli :
    `discogs_dump.connect_readonly()`) - permet aux tests de passer une base
    en mémoire minimale sans dépendre d'un vrai dump importé.

    Retourne {"n_labels", "n_edges", "n_artist_edges", "n_parent_edges",
    "n_artists_used", "n_artists_skipped_prolific"}. Lève RuntimeError si
    aucun référentiel Discogs n'est disponible, InterruptedError si
    `stop_cb()` devient vrai en cours de route (graphe existant conservé tel
    quel, rien n'est basculé — la passe "parent", rapide, n'est alors jamais
    atteinte)."""
    owns_con = dump_con is None
    if owns_con:
        from . import discogs_dump as dd
        dump_con = dd.connect_readonly()
        if dump_con is None:
            raise RuntimeError("référentiel Discogs local absent")

    out_con = _open_new_db()
    try:
        n_artists_used, n_artists_skipped = _build_artist_edges(
            dump_con, out_con, progress_cb, stop_cb, max_labels_per_artist, flush_every)
        n_parent_edges = _build_parent_edges(dump_con, out_con)

        n_edges = out_con.execute("SELECT COUNT(*) FROM label_pairs").fetchone()[0]
        n_artist_edges = n_edges - n_parent_edges
        n_labels = out_con.execute(
            "SELECT COUNT(DISTINCT lbl) FROM (SELECT a AS lbl FROM label_pairs UNION SELECT b FROM label_pairs)"
        ).fetchone()[0]
        _finalize_new_db(out_con)
        out_con = None  # déjà fermée par _finalize_new_db
        return {"n_labels": n_labels, "n_edges": n_edges,
                "n_artist_edges": n_artist_edges, "n_parent_edges": n_parent_edges,
                "n_artists_used": n_artists_used, "n_artists_skipped_prolific": n_artists_skipped}
    finally:
        if out_con is not None:
            out_con.close()
            try:
                os.remove(DB_PATH + ".new")
            except OSError:
                pass
        if owns_con:
            dump_con.close()


def neighbors(label_key, kinds=None, min_weight=1, limit=20, con=None):
    """Labels les plus liés à `label_key` sur le catalogue global, toutes
    origines confondues et triés par poids décroissant, sauf si `kinds`
    restreint (ex. `{"artist"}` ou `{"parent"}`). [] si le graphe n'est pas
    encore construit ou si le label n'y apparaît pas.

    Chaque résultat porte `role` : pour `kind="parent"`, `"child"` si
    `label_key` est le label ENFANT de `other` (`other` est alors son
    parent), `"parent"` si c'est l'inverse (`other` est alors un de ses
    sous-labels) — `None` pour `kind="artist"` (lien non dirigé)."""
    if not available() and con is None:
        return []
    owns = con is None
    if owns:
        con = sqlite3.connect(DB_PATH)
    try:
        kind_filter, extra = "", []
        if kinds:
            kind_filter = f" AND kind IN ({','.join('?' for _ in kinds)})"
            extra = list(kinds)
        rows = con.execute(
            f"SELECT b AS other, kind, weight, 'a' AS key_col FROM label_pairs "
            f"WHERE a = ? AND weight >= ?{kind_filter} "
            f"UNION ALL "
            f"SELECT a AS other, kind, weight, 'b' AS key_col FROM label_pairs "
            f"WHERE b = ? AND weight >= ?{kind_filter} "
            f"ORDER BY weight DESC LIMIT ?",
            [label_key, min_weight, *extra, label_key, min_weight, *extra, limit]).fetchall()
        out = []
        for other, kind, weight, key_col in rows:
            role = None
            if kind == KIND_PARENT:
                role = "child" if key_col == "a" else "parent"
            out.append({"label_key": other, "kind": kind, "weight": weight, "role": role})
        return out
    finally:
        if owns:
            con.close()
