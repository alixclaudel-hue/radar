"""Graphe label <-> label GLOBAL, construit sur le référentiel Discogs
PARTAGÉ (`discogs_dump.py`) — deux labels sont liés s'ils partagent des
artistes crédités sur une sortie, toutes catégories de goût confondues, sur
TOUT le catalogue importé. Rien à voir avec `labelgraph.py` (sous-graphe
visuel PAR UTILISATEUR, dérivé des graines de goût via `producer_graph.json`
que construit `job_build_graph`) : celui-ci ne dépend d'aucun goût, d'aucun
corpus écouté, d'aucune collection — un seul graphe, partagé par tous les
utilisateurs, reconstruit uniquement quand le dump mensuel change (Lot 2 de
la refonte scoring, cf. CLAUDE.md point 37).

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
            shared INTEGER NOT NULL,
            PRIMARY KEY (a, b)
        )
    """)


def _create_indexes(con):
    # PRIMARY KEY (a, b) indexe déjà les recherches par `a` ; `b` a besoin de
    # son propre index pour qu'une recherche "quels labels sont liés à X"
    # (X peut être en 1re ou 2e position) ne scanne jamais la table entière.
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
    if not counter:
        return
    con.executemany(
        "INSERT INTO label_pairs(a, b, shared) VALUES (?, ?, ?) "
        "ON CONFLICT(a, b) DO UPDATE SET shared = shared + excluded.shared",
        [(a, b, n) for (a, b), n in counter.items()])
    con.commit()
    counter.clear()


def build(dump_con=None, progress_cb=None, stop_cb=None,
          max_labels_per_artist=MAX_LABELS_PER_ARTIST, flush_every=FLUSH_EVERY):
    """Parcourt `release_artists` JOIN `releases` du référentiel Discogs
    partagé (déjà importé, cf. `discogs_dump.py`), groupe par artiste (la
    requête est triée par `artist_id`, donc chaque artiste n'est vu qu'une
    fois d'affilée), et compte pour chaque paire de labels qu'il relie
    combien d'artistes distincts les relient tous les deux.

    `dump_con` : connexion déjà ouverte sur discogs_dump.sqlite3 (repli :
    `discogs_dump.connect_readonly()`) - permet aux tests de passer une base
    en mémoire minimale sans dépendre d'un vrai dump importé.

    Retourne {"n_labels": ..., "n_edges": ..., "n_artists_used": ...,
    "n_artists_skipped_prolific": ...}. Lève RuntimeError si aucun
    référentiel Discogs n'est disponible, InterruptedError si `stop_cb()`
    devient vrai en cours de route (graphe existant conservé tel quel, rien
    n'est basculé).
    """
    owns_con = dump_con is None
    if owns_con:
        from . import discogs_dump as dd
        dump_con = dd.connect_readonly()
        if dump_con is None:
            raise RuntimeError("référentiel Discogs local absent")

    out_con = _open_new_db()
    counter = Counter()
    n_artists_used = 0
    n_artists_skipped = 0

    try:
        cur = dump_con.execute("""
            SELECT ra.artist_id, r.label_key
            FROM release_artists ra
            JOIN releases r ON r.id = ra.release_id
            WHERE r.label_key IS NOT NULL AND r.label_key != ''
            ORDER BY ra.artist_id
        """)
        current_artist, labels_seen = None, set()

        def _process(labels):
            nonlocal n_artists_used, n_artists_skipped
            if len(labels) < 2:
                return
            if len(labels) > max_labels_per_artist:
                n_artists_skipped += 1
                return
            n_artists_used += 1
            for a, b in itertools.combinations(sorted(labels), 2):
                counter[(a, b)] += 1

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
                            progress_cb(n_artists_used + n_artists_skipped)
                        if stop_cb and stop_cb():
                            raise InterruptedError("stop demandé pendant la construction du graphe labels")
                current_artist, labels_seen = artist_id, set()
            labels_seen.add(label_key)
        if current_artist is not None:
            _process(labels_seen)
        _flush(out_con, counter)

        n_edges = out_con.execute("SELECT COUNT(*) FROM label_pairs").fetchone()[0]
        n_labels = out_con.execute(
            "SELECT COUNT(DISTINCT lbl) FROM (SELECT a AS lbl FROM label_pairs UNION SELECT b FROM label_pairs)"
        ).fetchone()[0]
        _finalize_new_db(out_con)
        out_con = None  # déjà fermée par _finalize_new_db
        return {"n_labels": n_labels, "n_edges": n_edges,
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


def neighbors(label_key, min_shared=1, limit=20, con=None):
    """Labels les plus liés à `label_key` sur le catalogue global, triés par
    nombre d'artistes partagés décroissant. [] si le graphe n'est pas encore
    construit ou si le label n'y apparaît pas."""
    if not available() and con is None:
        return []
    owns = con is None
    if owns:
        con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT b AS other, shared FROM label_pairs WHERE a = ? AND shared >= ? "
            "UNION ALL "
            "SELECT a AS other, shared FROM label_pairs WHERE b = ? AND shared >= ? "
            "ORDER BY shared DESC LIMIT ?",
            (label_key, min_shared, label_key, min_shared, limit)).fetchall()
        return [{"label_key": r[0], "shared": r[1]} for r in rows]
    finally:
        if owns:
            con.close()
