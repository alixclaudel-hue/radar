# Radar - the scoring pipeline: Discogs dump to scored SQLite

This document covers one slice of the Radar app: the full pipeline that
turns Discogs's monthly catalog dump into a local SQLite catalog, optionally
a label-to-label graph on top of it, then a live scoring engine, and finally
a persisted, per-user SQLite table of scores ready to be consumed by
features like RECOS RADAR. It is not an overview of the whole app.

## 0. Boundary - what is inside this document, what is treated as external

**Inside (described in detail below), all under `radar_web/radar/`:**
- `discogs_dump.py` - downloads/parses the monthly Discogs XML dump into a
  local SQLite catalog (releases, labels, artists, credits, styles) and
  serves read-only lookups from it.
- `catalog_labelgraph.py` - builds a label<->label graph from that same
  catalog: labels that share a credited artist, and labels declared as
  parent/sublabel by Discogs itself. Its own separate SQLite file.
- `scoring.py`, class `Ctx` - the scoring engine. Reads the catalog above
  plus the user's own config/corpus/collection/graph files, and computes
  every score the app uses (`wmap`, affinity, `ascore`, `label_tier_map`,
  `reco_rows`/`reco_index`, `graph_rescore`, `album_score`).
- `scorestore.py` - precomputes `Ctx.album_score()` for releases and tracks
  and persists the result per user in its own SQLite file (`release_scores`,
  `track_scores`).
- The jobs in `crate_jobs.py` that drive these four modules:
  `job_import_discogs_dump`, `job_build_catalog_labelgraph`,
  `job_scorestore_releases`, `job_scorestore_tracks`, and `job_scan_recos`
  (described here only as the first consumer of `scorestore`, since it reads
  `track_scores`/`release_scores` directly rather than computing anything
  itself).
- The `radar_web/worker.py` triggers that decide *when* those jobs run
  (`_maybe_monthly_dump_sync`, `_maybe_catalog_labelgraph_build`,
  `_maybe_scorestore_build`), and the relevant slice of `store.py`
  (`label_categories`, `DEFAULT_SCORING`) that these modules read as
  configuration input.

**Outside (mentioned only to situate the pipeline, not described in depth):**
- `radar_web/app.py` (FastAPI+HTMX routes) - every page that calls `Ctx` on
  render (search, "Mes labels & artistes", Reglages) or reads `scorestore`
  for display. Treated purely as a consumer.
- RECOS RADAR's own logic beyond reading `scorestore`: `job_publish_recos`
  (the YouTube search + FIFO playlist logic), the `/reco-radar` page, the
  feedback/learning loop. Only `job_scan_recos`'s read of `scorestore` is in
  scope; what it does with the resulting candidates is not.
- YouTube/Spotify/Bandcamp ingestion (`job_ingest_youtube` etc.) - these feed
  `Ctx.corpus`, which the scoring engine reads, but the ingestion pipelines
  themselves (API calls, dedup, corpus merge) are external to this document.
- The live Discogs API (`radar_web/radar/discogs.py`) - genuinely external,
  except where `job_scorestore_tracks` falls back to it for one thing the
  local dump never contains: a release's actual tracklist.
- `radar_web/radar/labelgraph.py` - a *different*, per-user, taste-derived
  label graph built from `producer_graph.json`/`job_build_graph`. Not to be
  confused with `catalog_labelgraph.py` (catalog-wide, shared by everyone).
  Mentioned only to avoid that confusion.

No surprises on size or existence: all four modules described here exist
pretty much as the project's `CLAUDE.md` describes them, and the pipeline
really does chain the way the task asked - dump -> catalog SQLite -> label
graph SQLite (parallel, optional) -> in-memory `Ctx` scoring -> persisted
per-user scores SQLite. Nothing here needed guessing.

## 1. What this pipeline does

Discogs (the reference database record collectors use to catalog releases)
publishes a complete snapshot of its whole catalog once a month, as three
huge compressed XML files: one row per release ever listed, one per label,
one per artist. This pipeline downloads that snapshot, turns it into a
local SQLite file Radar can query instantly and offline, uses it (together
with the user's own listening history and manually curated taste) to give
every release, artist, and label a personal "how much would I like this"
score, and then saves those scores to disk so the rest of the app (in
particular the RECOS RADAR feature, which behaves like a personal
radio/discovery playlist) can use them without recomputing anything or
calling any API on the spot.

## 2. Notable techniques (no traditional "framework" here)

This slice of the app is plain Python with two off-the-shelf libraries that
shape its structure - there is no web framework, ORM, or JS toolchain in
this part of the codebase.

- **SQLite (via Python's built-in `sqlite3`)**: a full relational database
  engine that lives in a single file on disk, no server process needed. Used
  three times here, each time as its own file: `discogs_dump.sqlite3` (the
  catalog), `catalog_labelgraph.sqlite3` (the label graph), and
  `scorestore.sqlite3` (the persisted per-user scores, one file per user
  directory). Chosen because the data is read far more often than written
  and fits comfortably on one machine's disk.
- **`xml.etree.ElementTree.iterparse` (streaming XML parsing)**: reads the
  multi-gigabyte decompressed dump XML one element at a time instead of
  loading it all into memory, clearing each `<release>`/`<label>`/`<artist>`
  element (and the parent root) right after it's read. Without this, parsing
  ~18-20M releases would exhaust memory.
- **`gzip` streaming + a custom `_RootWrappedStream`**: the dump files arrive
  gzip-compressed and, depending on the export, may lack a single enclosing
  root XML tag; this small wrapper class fakes one on the fly so
  `iterparse` never sees invalid XML, without ever holding the decompressed
  file in memory at once.
- **SQLite WAL (Write-Ahead Logging) mode + atomic `.new` -> `os.replace`
  swap**: `discogs_dump.py` and `catalog_labelgraph.py` both build their
  *entire* replacement database in a sibling `*.new` file (fast, throwaway
  `synchronous=OFF`/`journal_mode=WAL` settings while building), then flip
  `journal_mode` back to `DELETE` and use one `os.replace()` to swap it into
  place atomically. Readers never see a half-rebuilt file, and the old file
  stays fully valid until the very last instant.

## 3. File structure

```
radar_web/radar/
  discogs_dump.py        -> downloads + parses the monthly Discogs XML dump
                             into discogs_dump.sqlite3 (the catalog).
                             Also the read API other modules use:
                             search_local, resolve_name, label_style_counts,
                             label_ids_for_artists, artist_ids_for_labels,
                             lookup_release, suggest_labels.
  catalog_labelgraph.py  -> builds a global label<->label graph from that
                             catalog (shared-artist edges + parent/sublabel
                             edges) into its own catalog_labelgraph.sqlite3.
  scoring.py              -> the scoring engine: class Ctx, NODE_DEPS (the
                             explicit dependency graph of its computed
                             values), topological_order().
  scorestore.py           -> persists Ctx.album_score() results per user
                             into scorestore.sqlite3 (release_scores,
                             track_scores tables).
  store.py                -> (partially in scope) load_config/read_config:
                             label_categories (Coeur/Aime tiers),
                             artist_categories, taste_categories,
                             DEFAULT_SCORING (all the tunable weights Ctx
                             and scorestore read).
  paths.py                -> (mentioned only) SHARED_DIR (catalog + label
                             graph files, one copy for everyone) vs.
                             user_paths(uid) (config/corpus/scorestore,
                             one copy per user).

crate_jobs.py (jobs that drive the pipeline, selected functions)
  job_import_discogs_dump()       -> runs discogs_dump.py's download/parse/
                                      finalize cycle; also supports a
                                      params["limit"] TEST mode that writes
                                      to a separate discogs_dump_test.sqlite3.
  job_build_catalog_labelgraph()  -> runs catalog_labelgraph.build(); same
                                      params["test"] escape hatch.
  job_scorestore_releases()       -> scores every release of every followed
                                      (Coeur+Aime) label via Ctx.album_score,
                                      writes scorestore.release_scores.
  job_scorestore_tracks()         -> refreshes scores of tracks already
                                      known (free, no network) and fetches
                                      real tracklists (Discogs API) for the
                                      top-scored releases that have none yet.
  job_scan_recos()                -> (boundary: only its scorestore read is
                                      in scope) selects RECOS RADAR
                                      candidates straight from
                                      track_scores JOIN release_scores.

radar_web/worker.py (background triggers, selected functions)
  _maybe_monthly_dump_sync()        -> enqueues job_import_discogs_dump when
                                        a new monthly dump is published
                                        (opt-in RADAR_DISCOGS_DUMP_SYNC=1).
  _maybe_catalog_labelgraph_build() -> enqueues job_build_catalog_labelgraph
                                        when the catalog has a newer dump
                                        than the graph was built from
                                        (opt-in RADAR_CATALOG_LABELGRAPH=1).
  _maybe_scorestore_build()         -> enqueues job_scorestore_releases on a
                                        fixed 6h cadence (opt-in
                                        RADAR_SCORESTORE=1).
```

## 4. How it works - the main flow

```
Discogs (data.discogs.com)
  monthly XML dump: releases.xml.gz, labels.xml.gz, artists.xml.gz
       |
       v  job_import_discogs_dump  (crate_jobs.py)
  discogs_dump.py : download -> verify checksum -> stream-parse ->
                     build in *.new -> atomic swap
       |
       v
  discogs_dump.sqlite3   (SHARED, one copy for every user)
    releases / release_styles / release_artists / labels / artists /
    artist_aliases
       |
       |----------------------------------------------.
       |                                               |
       v  job_build_catalog_labelgraph (optional,      |
       |   parallel branch - not required for scoring) |
  catalog_labelgraph.py : shared-artist edges +         |
                            parent/sublabel edges        |
       |                                                |
       v                                                |
  catalog_labelgraph.sqlite3  (SHARED)                  |
    label_pairs (a, b, kind, weight)                    |
    -- infrastructure only: nothing in scoring.py        |
       reads this yet (see Key Design Decisions)         |
                                                          |
       .--------------------------------------------------
       |
       v
  scoring.py : class Ctx  (rebuilt fresh on every call, memoised per
               request by a (mtime,size) signature of its input files)
    reads:
      - discogs_dump.sqlite3  (search_local, label_style_counts,
        label_ids_for_artists, artist_ids_for_labels)
      - the user's config.json (label_categories, artist_categories,
        taste_categories, scoring weights)      [store.py]
      - the user's corpus.json / collection.json / graph.json
    computes (in NODE_DEPS topological order):
      wmap -> seed_category_weight -> graph_rescore -> ascore ->
      label_artist_signal / label_db_signal -> reco_rows/reco_index ->
      album_score(release-or-track dict) -> (score, detail)
       |
       v  job_scorestore_releases -> job_scorestore_tracks (chained)
  scorestore.py : calls Ctx.album_score() once per release (then once per
                   track of the top-scored releases) and upserts the result
       |
       v
  scorestore.sqlite3   (PER USER, one file per user directory)
    release_scores (release_id, label, artist, title, score, detail_json)
    track_scores   (release_id, track_no, artist, title, score, detail_json)
       |
       v   (outside this document's scope from here on)
  job_scan_recos reads track_scores JOIN release_scores directly ->
  RECOS RADAR candidates -> YouTube search -> playlist (job_publish_recos)
```

Four distinct stages, each backed by its own storage:

1. **Catalog build** (`discogs_dump.py`): a monthly, all-at-once
   reconstruction of a shared SQLite file from Discogs's own dump. Nothing
   here is per-user - every Radar user reads the same catalog.
2. **Label graph build** (`catalog_labelgraph.py`): an optional, parallel
   enrichment of the same catalog into a second shared SQLite file,
   triggered whenever the catalog gets a newer dump. It does not feed back
   into stage 1, and (as of this writing) nothing in stage 3 reads it yet -
   it exists as pure infrastructure for a future scoring signal.
3. **Live scoring** (`scoring.py`'s `Ctx`): an in-memory, per-request
   computation - never itself persisted directly - that combines the shared
   catalog with one user's own taste config and listening history to
   produce a score for any given release, track, artist, or label. `Ctx` is
   cheap to rebuild (small files) and memoises its internal computations
   under a signature of its input files' mtimes/sizes, so calling it
   repeatedly in the same process for the same inputs is nearly free.
4. **Persisted scoring** (`scorestore.py`): a batch job that calls stage 3's
   `Ctx.album_score()` for every release of every label the user follows
   (then for every track of the best-scoring releases) and writes the
   result to a small per-user SQLite file, so that a feature like RECOS
   RADAR can read pre-computed, per-track scores without ever calling `Ctx`
   or touching the network at read time.

## 5. Data files

| File | Scope | Built/updated by | Contains |
|---|---|---|---|
| `discogs_dump.sqlite3` (`SHARED_DIR`) | shared, all users | `job_import_discogs_dump`, once a month (or on-demand `force`) | `releases` (id, title, artist, artist_key, label, label_key, catno, year, country, format, genres, styles, master_id, is_vinyl - one row per release, **all formats** since 2026-09-11, not vinyl-only), `release_styles` (release_id, style - one row per style, indexable), `release_artists` (release_id, artist_id, role - credits limited to roles that matter for scoring: Main/Producer/Remix/Written-By/Featuring), `labels` (id, name, name_key, parent, parent_id), `artists` (id, name, name_key, real_name), `artist_aliases` (name_key -> artist_id, from `<namevariations>`), and a materialized `label_styles` table (label_key, style, n - the exhaustive style profile of every label, built once at the end of each import). ~18-20M releases at full scale (not yet re-imported since the all-formats widening; a `limit`-bounded TEST import of 5000 releases has been validated). |
| `discogs_dump_meta.json` | shared | `job_import_discogs_dump` | `{dump_date, imported_at, n_total, n_vinyl, n_labels, n_artists}` - what `catalog_labelgraph.needs_rebuild()` and `scorestore`'s trigger compare against. |
| `discogs_dump_test.sqlite3` / `discogs_dump_import.state.json` | shared, throwaway/checkpoint | test imports (`params["limit"]`) / resumable real imports | never touched by a real import; the `.state.json` lets a real ~1h45 import survive a mid-import redeploy. |
| `catalog_labelgraph.sqlite3` (`SHARED_DIR`) | shared, all users | `job_build_catalog_labelgraph`, whenever the catalog above has a newer `dump_date` | one table, `label_pairs(a, b, kind, weight)` - `kind="artist"` (undirected, weight = number of distinct shared credited artists) and `kind="parent"` (directed, a=child label, b=parent label, weight always 1, from Discogs's own `<sublabels>` data). |
| `catalog_labelgraph_meta.json` | shared | `job_build_catalog_labelgraph` | `{dump_date, built_at, n_labels, n_edges, n_artist_edges, n_parent_edges, ...}`. |
| `scorestore.sqlite3` (under each `users/<uid>/`) | **per user** | `job_scorestore_releases` (releases) then `job_scorestore_tracks` (tracks), chained | `release_scores` (release_id PK, label_key, label, artist, title, year, styles, score, detail_json, computed_at - one row per release of a followed label; `score`/`detail_json` can be `NULL`, meaning "not scorable", never confused with a low real score) and `track_scores` ((release_id, track_no) PK, artist, title, score, detail_json, computed_at - one row per track of the best-scoring releases, `detail_json` holds the per-track `{label, artist, style}` breakdown used by RECOS RADAR's feedback). No atomic-swap dance here: this file grows by small individually-committed upserts, so a reader is never caught mid-rebuild. |
| `config.json` (per user, read via `store.py`) | per user | user edits via Reglages, or automatic migration on load | `label_categories` (`{"1": [...], "2": [...]}` - Coeur/Aime tiers, replacing the old flat `labels`/`watchlist` lists), `artist_categories`, `taste_categories`, and `scoring` (all tunable weights, `DEFAULT_SCORING` deep-merged in - `scoring.scorestore.min_score`/`fetches_per_run`, `scoring.recos.min_score`/`max_new_releases`). This is `Ctx`'s and `scorestore`'s only source of "what does this user like". |
| `corpus.json` / `collection.json` / `graph.json` / `profile.json` / `artists_res.json` (per user) | per user | other jobs outside this document's scope (`job_ingest_*`, `job_fetch_collection`, `job_build_graph`, `job_profile_labels`, `job_canonicalize`) | listening history, owned/wanted Discogs collection, the taste-derived producer graph, per-label style profile (fallback when a label is missing from the dump's own `label_styles`), and artist name resolution. `Ctx` reads all of these as inputs but does not write any of them - they are treated here strictly as data sources. |

## 6. Data flow summary

One sentence: a monthly Discogs XML export becomes a shared, queryable
SQLite catalog; a scoring engine combines that catalog with one user's own
config and listening history to score any release/track/artist/label on
demand; and a separate batch job runs that same scoring engine ahead of
time over every followed label's catalog and saves the results into a
small per-user SQLite file that other features can read cheaply.

| What | Where it lives | How/when it changes |
|---|---|---|
| Discogs catalog snapshot | `discogs_dump.sqlite3` (shared) | Replaced wholesale, monthly (or on manual `force`); atomic swap, never a partial delta. |
| Label<->label graph (shared/artist + parent) | `catalog_labelgraph.sqlite3` (shared) | Rebuilt wholesale whenever the catalog above gets a new `dump_date`; not consumed by scoring yet. |
| `wmap` (style -> taste weight) | in-memory, `Ctx._memo`, per request | Recomputed whenever `config.json`'s mtime/size changes (user edits taste categories/weights). |
| `label_tier_map` / `artist_tier_map` (Coeur/Aime) | in-memory, `Ctx._memo` | Recomputed on any `config.json` change (label/artist added, removed, or re-tiered in the UI). |
| `graph_rescore`, `ascore`, `reco_rows`/`reco_index` | in-memory, `Ctx._memo`, chained via `NODE_DEPS` | Recomputed together whenever any input file changes (`graph.json` from `job_build_graph`, `corpus.json` from ingestion, `collection.json` from `job_fetch_collection`, or `config.json`). |
| `album_score(release-or-track)` | computed on the fly, not memoised per-call (only its dependencies are) | Every call re-evaluates the current `ascore`/`reco_index`/`wmap` for the given label/artist/style combination. |
| `release_scores` / `track_scores` rows | `scorestore.sqlite3` (per user) | `release_scores`: fully recomputed for every followed label's releases on every `job_scorestore_releases` run (no staleness bookkeeping, since it's all local/free). `track_scores`: known tracks refreshed for free every run; new tracks added only when `job_scorestore_tracks` fetches a tracklist from the live Discogs API, budgeted at `scoring.scorestore.fetches_per_run` (default 20) per run. |

## 7. Key design decisions

- **Shared vs. per-user storage, by design.** `discogs_dump.sqlite3` and
  `catalog_labelgraph.sqlite3` live under `paths.SHARED_DIR` and are replaced
  wholesale - one copy serves every user, and it makes no sense to split them
  by user since the underlying Discogs catalog is the same for everyone.
  `scorestore.sqlite3` lives under each user's own directory and is *never*
  replaced wholesale (see next point) - because its content is a function of
  that user's own `label_categories`/`scoring` config.
- **Two different "atomicity" strategies, deliberately.** The two catalog
  files use a `.new` file + `os.replace()` swap because they are rebuilt
  entirely from scratch each time - a reader must never see a half-rebuilt
  file. `scorestore.sqlite3` grows by small, already-committed SQLite
  transactions (one upsert at a time) and is never rebuilt from scratch, so
  there is nothing to hide between writes and no swap is needed - instead it
  gets `_migrate_schema()` (an idempotent `ALTER TABLE ... ADD COLUMN` run on
  every open, including read-only opens) to handle schema changes on an
  already-populated file in place.
- **`catalog_labelgraph.py` is currently a dead end for scoring.** As of
  this writing, `scoring.py`'s `Ctx` does not read `catalog_labelgraph.sqlite3`
  at all - it is built (opt-in, `RADAR_CATALOG_LABELGRAPH=1`) purely as
  infrastructure for a scoring signal planned but not yet wired in. Don't
  assume label-graph edges influence `ascore`/`reco_rows` today; they don't.
- **`is_vinyl` is a flag, not a filter.** Since 2026-09-11 `discogs_dump.py`
  keeps every release format (vinyl, CD, digital, cassette, ...), not just
  12"/LP vinyl. `search_local`, `label_style_counts`, and
  `catalog_labelgraph.build()` therefore now see the *entire* catalog by
  default - no format filter is applied unless a caller explicitly adds
  `WHERE is_vinyl = 1`. This was a deliberate widening (a more complete
  catalog makes label/artist affinity more accurate) with an accepted side
  effect: non-vinyl reissues of the same release can now show up as
  separate rows.
- **`NODE_DEPS` makes `Ctx`'s computation order an explicit, checked DAG.**
  Every memoised method in `scoring.py` declares which other memoised
  methods it calls. `topological_order()` runs at *import time* and raises
  if the declared graph has a cycle; `Ctx._memo()` additionally checks *at
  runtime* (via a thread-local call stack) that a method only touches
  dependencies it actually declared. This was added not because a real
  cycle existed (none was found), but to make the computation order legible
  and to give `scorestore.py`'s asynchronous precompute a documented,
  verified order to follow.
- **A "not scored" value is never silently dropped or treated as a low
  score.** Both `Ctx.affinity_score()` (a style absent from `wmap`) and
  `scorestore.upsert_release_score()` (`score`/`detail` can be `None`)
  preserve the distinction between "no data" and "a real, low score" -
  conflating the two was an earlier diagnosed bug class in this codebase
  (referenced in comments as "diagnostic N4").
- **`label_artist_signal` vs. `label_db_signal` vs. `artist_label_signal`
  look similar but are intentionally three different signals.**
  `label_artist_signal` only counts labels actually present in the user's
  *listened* corpus; `label_db_signal` and `artist_label_signal` mine the
  *whole* Discogs catalog via `discogs_dump.py` for "does an artist I like
  have a release on this label" (and vice versa) even for labels/artists
  never listened to. `artist_label_signal` additionally cross-checks the
  *specific release's* styles against the user's taste, so a stylistically
  unrelated feature credit on an otherwise-liked label doesn't wrongly boost
  an artist (a real bug fixed 2026-09-10).
- **`scorestore` recomputes releases from scratch every run, but never
  re-fetches a tracklist it already has.** Release scoring is pure local
  computation (cheap, no network), so `job_scorestore_releases` simply
  redoes all of it every time to stay current with the user's latest taste
  settings. Track scoring splits into a free local refresh (existing tracks,
  every run) and a budgeted network fetch (`fetches_per_run`, default 20)
  for releases that have no tracklist yet - because fetching a tracklist is
  the one part of this pipeline that calls the live Discogs API.
- **Per-label query caps exist purely to prevent one prolific label from
  starving the others.** `discogs_dump.search_local()` (used by
  `job_scorestore_releases`) is called once per followed label with its own
  `SCORESTORE_PER_LABEL_LIMIT` (500), rather than one global query across
  all followed labels - a documented past bug (a single global,
  year-sorted query let one high-volume label crowd out every other
  followed label's releases).
- **`Ctx` is deliberately cheap to reconstruct.** It is built fresh on every
  scoring call (constructor re-reads all the small JSON inputs), relying
  entirely on the `_files_sig()`/`_memo()` mtime-based memoisation to avoid
  recomputation when nothing has actually changed - there is no long-lived
  singleton or explicit cache invalidation to manage.

## 8. Key files table

| File | Purpose |
|---|---|
| `radar_web/radar/discogs_dump.py` | Downloads and stream-parses the monthly Discogs XML dump into `discogs_dump.sqlite3`; the shared catalog and its read API. |
| `radar_web/radar/catalog_labelgraph.py` | Builds the shared label<->label graph (`catalog_labelgraph.sqlite3`) from that catalog; two edge kinds, artist and parent. |
| `radar_web/radar/scoring.py` | The scoring engine (`class Ctx`), its explicit dependency DAG (`NODE_DEPS`), and every derived score (`wmap`, `ascore`, `reco_rows`, `album_score`, ...). |
| `radar_web/radar/scorestore.py` | Persists `Ctx.album_score()` results per user into `scorestore.sqlite3` (`release_scores`, `track_scores`). |
| `radar_web/radar/store.py` | Loads/saves per-user `config.json`; owns `label_categories`, `artist_categories`, `taste_categories`, and `DEFAULT_SCORING` (every tunable weight `Ctx`/`scorestore` read). |
| `radar_web/radar/paths.py` | Defines `SHARED_DIR` (catalog + label graph, one copy for everyone) vs. `user_paths(uid)` (config/corpus/scorestore, one copy per user). |
| `crate_jobs.py` (`job_import_discogs_dump`) | Downloads, verifies, and imports the monthly dump; resumable; supports a bounded TEST mode via `params["limit"]`. |
| `crate_jobs.py` (`job_build_catalog_labelgraph`) | Runs `catalog_labelgraph.build()`; also has a TEST mode against the test catalog. |
| `crate_jobs.py` (`job_scorestore_releases`) | Scores every release of every followed label via `Ctx.album_score`, writes `release_scores`, chains `scorestore_tracks`. |
| `crate_jobs.py` (`job_scorestore_tracks`) | Refreshes known track scores for free; fetches real tracklists (Discogs API) for top-scored releases missing one, budgeted per run. |
| `crate_jobs.py` (`job_scan_recos`) | First real consumer of `scorestore`: selects RECOS RADAR candidates directly from `track_scores JOIN release_scores` (only this read is in scope here). |
| `radar_web/worker.py` | Background loop; decides *when* to enqueue the jobs above (`_maybe_monthly_dump_sync`, `_maybe_catalog_labelgraph_build`, `_maybe_scorestore_build`), each behind its own opt-in env var. |

**Where to look for any feature in this pipeline:**
- "Why is a release/label/artist missing from search or scoring" -> check
  `discogs_dump.py`'s `_parse_release_elem`/`_is_vinyl` (is it in the dump at
  all, and is `is_vinyl` set as expected) and `search_local`'s `LIMIT`.
- "Why did this album/track get the score it got" -> `scoring.py`,
  `Ctx.album_score()` and the terms feeding it (`reco_index` for the label
  term, `ascore` for the artist term, `style_affinity_of` for the style
  term).
- "Why is a label graph edge missing/wrong" -> `catalog_labelgraph.py`,
  `_build_artist_edges`/`_build_parent_edges` (remember: this graph is not
  yet read by `scoring.py`).
- "Why is a precomputed score stale or missing" -> `scorestore.py`'s
  `upsert_release_score`/`upsert_track_scores` and the two jobs that call
  them; check `scoring.scorestore.min_score`/`fetches_per_run` in config.
- "Why didn't a new dump/graph/scorestore build trigger automatically" ->
  the matching `_maybe_*` function in `radar_web/worker.py` and its opt-in
  env var (`RADAR_DISCOGS_DUMP_SYNC`, `RADAR_CATALOG_LABELGRAPH`,
  `RADAR_SCORESTORE`).
