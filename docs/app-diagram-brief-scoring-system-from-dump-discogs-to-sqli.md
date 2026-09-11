# Diagram Brief: Scoring pipeline — Discogs dump to scored SQLite

**Layout**: top-to-bottom
**Flow summary**: Discogs's monthly XML dump is parsed into a shared SQLite catalog; an optional parallel job builds a shared label graph from that same catalog (not yet consumed by scoring); the live scoring engine (Ctx) combines the catalog with one user's own taste config to score anything on demand; a batch job runs that same engine ahead of time over every followed label and persists the result into a per-user scored SQLite store that RECOS RADAR reads.

---

## Elements

Create a group labeled "Layer 2 — Catalog Build" that contains: Import Job, Catalog DB.

Create a group labeled "Layer 3 — Label Graph (optional, parallel)" that contains: Graph Build Job, Label Graph DB.

Create a group labeled "Layer 4 — Live Scoring" that contains: Ctx Engine, User Taste Inputs.

Create a group labeled "Layer 5 — Persisted Scoring" that contains: Scorestore Releases Job, Scorestore Tracks Job, Scorestore DB.

Create a box labeled "Discogs Monthly Dump". Note: releases.xml.gz + labels.xml.gz + artists.xml.gz from data.discogs.com, ~18-20M releases.

Create a box labeled "Import Job". Note: discogs_dump.py — streaming XML parse (iterparse), checksum verify, builds in a sibling .new file, atomic os.replace swap.

Create a box labeled "Catalog DB". Note: discogs_dump.sqlite3 (SHARED, one copy for every user) — releases/release_styles/release_artists/labels/artists, all formats since 2026-09-11.

Create a box labeled "Graph Build Job". Note: catalog_labelgraph.py — builds shared-artist edges + Discogs-declared parent/sublabel edges; opt-in RADAR_CATALOG_LABELGRAPH=1, triggered on a newer dump_date.

Create a box labeled "Label Graph DB". Note: catalog_labelgraph.sqlite3 (SHARED) — table label_pairs(a, b, kind, weight).

Create a box labeled "Ctx Engine". Note: scoring.py's class Ctx — rebuilt fresh per request, memoised by NODE_DEPS topological order; computes wmap, ascore, reco_rows/reco_index, album_score.

Create a box labeled "User Taste Inputs". Note: per-user config.json (label_categories, scoring weights via store.py) + corpus.json/collection.json/graph.json.

Create a box labeled "Scorestore Releases Job". Note: job_scorestore_releases — calls Ctx.album_score() for every release of every followed (Coeur+Aime) label.

Create a box labeled "Scorestore Tracks Job". Note: job_scorestore_tracks — free local refresh of known tracks + budgeted tracklist fetch (fetches_per_run, default 20) for top-scored releases missing one.

Create a box labeled "Scorestore DB". Note: scorestore.sqlite3 (PER USER) — release_scores + track_scores, small incremental upserts, no atomic swap needed.

Create a box labeled "job_scan_recos (boundary)". Note: only its scorestore read is in scope — selects RECOS RADAR candidates from track_scores JOIN release_scores.

Create a box labeled "Live Discogs API". Note: external — the one network call in this pipeline, used only to fetch a release's real tracklist.

Create a box labeled "app.py routes / UI". Note: external — search, Reglages, Mes labels & artistes call Ctx on render.

Create a box labeled "RECOS RADAR playlist logic". Note: external — job_publish_recos, YouTube search, FIFO playlist, feedback loop.

Create a box labeled "YouTube/Spotify/Bandcamp ingestion". Note: external — job_ingest_* jobs that populate corpus.json.

Draw an arrow from Discogs Monthly Dump to Import Job. Label it "download + verify checksum".

Draw an arrow from Import Job to Catalog DB. Label it "stream-parse, build .new, atomic swap".

Draw an arrow from Catalog DB to Graph Build Job. Label it "triggered when dump_date is newer".

Draw an arrow from Graph Build Job to Label Graph DB. Label it "shared-artist + parent/sublabel edges".

Draw an arrow from Catalog DB to Ctx Engine. Label it "search_local, label_style_counts, artist_ids_for_labels".

Draw an arrow from User Taste Inputs to Ctx Engine. Label it "label_categories, scoring weights, corpus/collection/graph".

Draw an arrow from Ctx Engine to Scorestore Releases Job. Label it "album_score() per release".

Draw an arrow from Scorestore Releases Job to Scorestore Tracks Job. Label it "chained".

Draw an arrow from Scorestore Tracks Job to Scorestore DB. Label it "upsert release_scores / track_scores".

Draw an arrow from Scorestore Tracks Job to Live Discogs API. Label it "fetch tracklist (budgeted, top-scored only)".

Draw an arrow from Scorestore DB to job_scan_recos (boundary). Label it "track_scores JOIN release_scores".

Draw an arrow from job_scan_recos (boundary) to RECOS RADAR playlist logic. Label it "candidates".

Draw an arrow from Ctx Engine to app.py routes / UI. Label it "on-demand scoring".

Draw an arrow from YouTube/Spotify/Bandcamp ingestion to User Taste Inputs. Label it "feeds corpus.json".

Mark Discogs Monthly Dump as entry point.

Mark Catalog DB as database.

Mark Label Graph DB as database.

Mark Scorestore DB as database.

Mark Live Discogs API as external.

Mark app.py routes / UI as external.

Mark RECOS RADAR playlist logic as external.

Mark YouTube/Spotify/Bandcamp ingestion as external.

---

## Notes

- Boundary: only discogs_dump.py, catalog_labelgraph.py, scoring.py, scorestore.py and the jobs/worker triggers that drive them are drawn in detail. Everything marked "external" is a single box, not opened up.
- Deliberate gap, not a bug: there is no arrow from Label Graph DB to Ctx Engine. As of this writing `scoring.py` does not read `catalog_labelgraph.sqlite3` at all — it exists purely as infrastructure for a scoring signal planned but not yet wired in.
- Not drawn, to avoid confusion: `radar_web/radar/labelgraph.py` is a *different*, per-user, taste-derived label graph (built from `producer_graph.json`/`job_build_graph`), unrelated to the catalog-wide `catalog_labelgraph.py` shown here.
- Each of the three SQLite files uses its own storage strategy: Catalog DB and Label Graph DB are rebuilt wholesale via `.new` + atomic `os.replace` (never a partial file exposed to readers); Scorestore DB grows by small already-committed upserts and is never rebuilt from scratch (schema changes handled instead by an idempotent `ALTER TABLE` run on open).
- "User Taste Inputs" and the jobs that populate it (fetch_collection, build_graph, canonicalize, profile_labels) beyond ingestion are themselves outside this pipeline's detailed scope — shown only as the config/data Ctx reads.
