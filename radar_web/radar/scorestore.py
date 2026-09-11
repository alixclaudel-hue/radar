"""Précalcul asynchrone des scores release/piste (Lot 4 de la refonte scoring,
cf. CLAUDE.md point 37) — stocke, PAR UTILISATEUR, le résultat de
`Ctx.album_score()` pour chaque sortie des labels suivis (Cœur+Aimé), puis
pour chaque piste des sorties les mieux notées.

Séparé de `discogs_dump.py` (le référentiel Discogs PARTAGÉ, remplacé en bloc
une fois par mois) : ces données sont PAR UTILISATEUR (dépendent de son goût,
changent à chaque réglage de `label_categories`/`scoring`) et grossissent par
petits upserts au fil des scans, jamais par un remplacement en bloc —
contrairement à `discogs_dump.py`/`catalog_labelgraph.py`, aucune bascule
`.new`/`os.replace` n'est donc nécessaire ici : chaque écriture est déjà une
transaction SQLite commitée d'un coup, un lecteur ne voit jamais un état à
moitié reconstruit entre deux écritures (rien à masquer, donc rien à
basculer).

Deux tables :
- `release_scores` : score par SORTIE, rafraîchi en entier à chaque passage
  de `job_scorestore_releases` (crate_jobs.py) — recalcul purement local via
  `Ctx.album_score`, aucun appel réseau, donc pas besoin de bookkeeping de
  fraîcheur séparé.
- `track_scores` : score par PISTE, pour les sorties les mieux notées
  seulement — `job_scorestore_tracks` sépare la partie coûteuse (un appel API
  pour récupérer la tracklist réelle, une seule fois par sortie tant qu'elle
  n'a aucune piste connue — le dump local n'en contient pas, cf.
  `discogs_dump.py`) de la partie recalcul de score proprement dite (locale,
  gratuite, refaite à chaque passage pour rester à jour après un changement
  de goût sans jamais re-fetcher une tracklist déjà connue).

Lot 4 : infrastructure + jobs seulement — rien ne lit encore ces tables côté
UI (prévu au Lot 5, cf. CLAUDE.md point 37)."""
import json
import os
import sqlite3
from datetime import datetime

from . import paths


def db_path(uid):
    return os.path.join(paths.user_paths(uid).dir, "scorestore.sqlite3")


def available(uid):
    return os.path.exists(db_path(uid))


def _create_schema(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS release_scores (
            release_id INTEGER PRIMARY KEY,
            label_key TEXT,
            label TEXT,
            artist TEXT,
            title TEXT,
            year INTEGER,
            styles TEXT,
            score INTEGER,
            detail_json TEXT,
            computed_at TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_release_scores_label_key ON release_scores(label_key)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_release_scores_score ON release_scores(score)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS track_scores (
            release_id INTEGER,
            track_no INTEGER,
            artist TEXT,
            title TEXT,
            score INTEGER,
            computed_at TEXT,
            PRIMARY KEY (release_id, track_no)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_track_scores_score ON track_scores(score)")


def open_db(uid):
    """Connexion en écriture, schéma créé si absent (`user_paths` a déjà créé
    le dossier de l'utilisateur). WAL : un futur lecteur (Lot 5) ne bloque
    jamais sur un job en train d'écrire."""
    con = sqlite3.connect(db_path(uid))
    con.execute("PRAGMA journal_mode=WAL")
    _create_schema(con)
    return con


def connect_readonly(uid):
    return sqlite3.connect(db_path(uid)) if available(uid) else None


def prune_labels(con, keep_label_keys):
    """Retire les sorties (et leurs pistes) dont le label n'est plus suivi —
    sinon un label retiré de `label_categories` laisserait ses sorties (et
    leurs scores, désormais hors de propos) grossir indéfiniment le fichier.
    Commite lui-même (appelé en tout début de `job_scorestore_releases`,
    avant toute autre écriture de la même transaction logique)."""
    keep = list(keep_label_keys)
    if not keep:
        con.execute("DELETE FROM track_scores")
        con.execute("DELETE FROM release_scores")
        con.commit()
        return
    qmarks = ",".join("?" * len(keep))
    con.execute(
        f"DELETE FROM track_scores WHERE release_id IN "
        f"(SELECT release_id FROM release_scores WHERE label_key NOT IN ({qmarks}))", keep)
    con.execute(f"DELETE FROM release_scores WHERE label_key NOT IN ({qmarks})", keep)
    con.commit()


def upsert_release_score(con, row):
    """`row` : {release_id, label_key, label, artist, title, year, styles,
    score, detail} — `score`/`detail` peuvent être `None` (sortie
    non-notable, ex. aucun label/artiste/style reconnu) ; stocké tel quel
    plutôt que silencieusement omis (même principe que N4 : "pas de donnée"
    doit rester distinct de "valeur basse")."""
    con.execute(
        "INSERT OR REPLACE INTO release_scores "
        "(release_id, label_key, label, artist, title, year, styles, score, detail_json, computed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (row["release_id"], row["label_key"], row["label"], row["artist"], row["title"],
         row.get("year"), row.get("styles"), row.get("score"), json.dumps(row.get("detail") or {}),
         datetime.now().isoformat(timespec="seconds")))


def top_releases(con, min_score=0, without_tracks_only=False, limit=100):
    """[{release_id, label, artist, title, year, styles, score}] triées par
    score décroissant (une sortie sans score, `score IS NULL`, est exclue par
    la comparaison SQL — jamais confondue avec un score bas). Utilisé par
    `job_scorestore_tracks` pour choisir les sorties dont il faut encore
    récupérer la tracklist réelle (`without_tracks_only=True`)."""
    where = "score >= ?"
    params = [min_score]
    if without_tracks_only:
        where += " AND release_id NOT IN (SELECT DISTINCT release_id FROM track_scores)"
    rows = con.execute(
        f"SELECT release_id, label, artist, title, year, styles, score FROM release_scores "
        f"WHERE {where} ORDER BY score DESC LIMIT ?", params + [limit]).fetchall()
    keys = ("release_id", "label", "artist", "title", "year", "styles", "score")
    return [dict(zip(keys, r)) for r in rows]


def releases_with_tracks(con):
    """[{release_id, label, styles}] — sorties ayant déjà au moins une piste
    connue, quel que soit leur score courant (une sortie tombée sous le seuil
    depuis garde ses pistes déjà récupérées à jour, cf. docstring du module)
    — cible de la passe de rafraîchissement gratuite de `job_scorestore_tracks`."""
    rows = con.execute(
        "SELECT DISTINCT rs.release_id, rs.label, rs.styles FROM release_scores rs "
        "JOIN track_scores ts ON ts.release_id = rs.release_id").fetchall()
    keys = ("release_id", "label", "styles")
    return [dict(zip(keys, r)) for r in rows]


def upsert_track_scores(con, release_id, tracks):
    """`tracks` : [{track_no, artist, title, score}, ...] — remplace toutes
    les pistes listées de cette sortie (upsert par `(release_id, track_no)`,
    jamais un remplacement en bloc de la sortie entière : une piste absente
    de `tracks` mais déjà connue reste inchangée)."""
    now = datetime.now().isoformat(timespec="seconds")
    con.executemany(
        "INSERT OR REPLACE INTO track_scores (release_id, track_no, artist, title, score, computed_at) "
        "VALUES (?,?,?,?,?,?)",
        [(release_id, t["track_no"], t["artist"], t["title"], t.get("score"), now) for t in tracks])


def tracks_for_release(con, release_id):
    rows = con.execute(
        "SELECT track_no, artist, title, score FROM track_scores "
        "WHERE release_id = ? ORDER BY track_no", (release_id,)).fetchall()
    keys = ("track_no", "artist", "title", "score")
    return [dict(zip(keys, r)) for r in rows]


def stats(con):
    n_releases = con.execute("SELECT COUNT(*) FROM release_scores").fetchone()[0]
    n_tracks = con.execute("SELECT COUNT(*) FROM track_scores").fetchone()[0]
    n_releases_with_tracks = con.execute(
        "SELECT COUNT(DISTINCT release_id) FROM track_scores").fetchone()[0]
    return {"n_releases": n_releases, "n_tracks": n_tracks,
            "n_releases_with_tracks": n_releases_with_tracks}
