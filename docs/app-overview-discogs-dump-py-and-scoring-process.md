# Radar - the Discogs dump index and the scoring engine

This document covers ONE slice of the Radar app: the local SQLite copy of
the monthly Discogs catalog dump (`radar_web/radar/discogs_dump.py`) and the
scoring engine that turns it, plus the user's own taste data, into the
numbers ("album score", "artist score", "label reco score") the rest of the
app displays (`radar_web/radar/scoring.py`, class `Ctx`). It is not an
overview of the whole app.

## 0. Boundary - what is inside this document, what is treated as external

**Inside (described in detail below):**
- `radar_web/radar/discogs_dump.py` (1173 lines) - downloads the monthly
  Discogs dump, parses it, builds a local SQLite file, and serves read-only
  lookups from it.
- `radar_web/radar/scoring.py` (617 lines), specifically the class `Ctx` -
  reads that SQLite file plus the user's own JSON data and config, and
  computes every score the app uses (`wmap`, `label_affinities`, `ascore`,
  `label_tier_map`/`artist_tier_map`, `graph_rescore`, `reco_rows`/
  `reco_index`, `album_score`).

**Outside (mentioned only to situate the pipeline, not described in depth):**
- `radar_web/radar/store.py` - loads/saves the per-user config JSON
  (`label_categories`, `artist_categories`, `scoring` weights). `Ctx` reads
  this config through `store.read_config()`; this document treats `store.py`
  as a data source `Ctx` consumes, not as part of the scoring logic itself.
- `radar_web/radar/catalog_labelgraph.py` - a separate module that builds a
  catalog-wide label<->label graph from the same SQLite file. It is already
  documented in `docs/app-overview-scoring-lot1.md` / the project's
  `CLAUDE.md` (point 38) and is NOT consumed by `scoring.py` yet ("Lot 2" of
  an ongoing refactor, pure infrastructure so far) - out of scope here.
- `crate_jobs.py` - the long-running jobs (`job_import_discogs_dump`,
  `job_scan_recos`, `job_build_graph`, `job_canonicalize`, `job_profile_labels`,
  `job_fetch_collection`, `job_merge_corpus`, `job_build_catalog_labelgraph`)
  that call INTO `discogs_dump.py` and `scoring.Ctx`. They are described only
  as callers - what they ask for and what they do with the answer - not in
  their own internal detail.
- `radar_web/app.py` (FastAPI+HTMX routes) - the UI that calls `Ctx` on every
  page render (search, "Mes labels & artistes", Reco Radar, Reglages).
  Described only as a consumer.
- The live Discogs API (`radar_web/radar/discogs.py`), YouTube (`ytcache.py`),
  Bandcamp/Spotify ingestion - genuinely external data sources, unrelated to
  this document except where a job falls back to the live API when the local
  dump has no answer (e.g. fetching a release's actual tracklist, which the
  dump never contains).

No surprises on size: this focus is exactly two files, and both exist
essentially as described in the project's `CLAUDE.md` - `discogs_dump.py` as
"the local SQLite index of the monthly dump" and `scoring.py`'s `Ctx` as "the
brain". Nothing here needed guessing.

## 1. What this part of the app does

Discogs (the reference database for record collectors) publishes a full
snapshot of its catalog once a month as three giant compressed XML files:
one for every release ever listed, one for every label, one for every
artist. `discogs_dump.py` downloads that snapshot and turns it into a local
SQLite file Radar can query instantly, offline, without ever calling the
Discogs API - useful because the live API is slow, rate-limited, and blind
to the "which labels does this artist share with labels I already follow"
kind of question this app asks constantly.

`scoring.py`'s `Ctx` is the part that actually answers "how good is this for
my taste?" - for a record, an artist, or a label. It blends four kinds of
evidence: styles/genres you've told the app you like, artists you've marked
as favorites, what you've actually listened to or bought (your "corpus" and
Discogs collection), and structural hints from the catalog itself (does an
artist you like have a record on a label you follow?). It turns all of that
into three numbers used everywhere in the UI: an artist score, a label
"worth following" score, and a record ("album") score.

## 2. Tech stack relevant to this slice

- **SQLite** (Python's built-in `sqlite3` module, no server) - the dump is
  stored as a single file (`discogs_dump.sqlite3` under the app's shared data
  directory) with plain tables and indexes. Chosen because it's a file like
  any other in `/data` - no database server to run, and it supports the
  atomic "replace the whole file at once" trick the import relies on (see
  section 7).
- **`xml.etree.ElementTree.iterparse`** (Python standard library) - streams
  through each multi-gigabyte dump file element by element instead of
  loading it into memory, calling `.clear()` on each parsed `<release>`/
  `<label>`/`<artist>` element as it goes so memory stays flat across ~18-20
  million rows.
- **`requests`** - plain HTTP download of the dump files from
  `data.discogs.com`, with byte-range resume, a browser-like User-Agent (the
  host sits behind Cloudflare), and a SHA-256 checksum check against the
  dump's published `CHECKSUM.txt`.
- No framework shapes `scoring.py` itself - it's pure Python (`math`,
  `hashlib`, `re`) operating on plain dicts/lists. It's a library module: it
  has no routes and no side effects, and is only ever imported by the jobs
  and the web app described as "outside" above.

## 3. File structure

```
radar_web/radar/
  discogs_dump.py    ->  builds + serves the local SQLite copy of the Discogs dump
                          (download, parse, atomic swap, search_local, resolve_name,
                          label_style_counts, artist_ids_for_labels, label_ids_for_artists)
  scoring.py          ->  class Ctx: reads discogs_dump.sqlite3 + the user's config/JSON
                          files, computes wmap, ascore, label_tier_map, graph_rescore,
                          reco_rows/reco_index, album_score

Data this slice reads/writes, under the shared data dir (paths.SHARED_DIR):
  discogs_dump.sqlite3            ->  the built index (releases, labels, artists, ...)
  discogs_dump_meta.json          ->  {dump_date, imported_at, n_total, n_vinyl, n_labels, n_artists}
  discogs_dump_import.state.json  ->  resume checkpoint for an interrupted import (deleted on success)
  discogs_dump_raw/*.xml.gz       ->  downloaded dump files, deleted once imported

Per-user data this slice only READS (owned/written by store.py and the jobs
listed as external callers - not built here):
  crate_radar_config.json  ->  label_categories, artist_categories, scoring weights (store.py)
  labels_profile.json      ->  fallback style profile per label (job_profile_labels, API-sampled)
  collection_cache.json    ->  owned/wanted Discogs collection counts (job_fetch_collection)
  taste_corpus.json        ->  everything ingested from YouTube/Spotify/Bandcamp/DJ sets
  artists_resolved.json    ->  artist name -> canonical Discogs id/name (job_canonicalize/resolve_artists)
  producer_graph.json      ->  raw co-credit graph edges (job_build_graph, mode "taste", per user)
```

Neither file defines HTTP routes; both are plain library modules called by
`radar_web/app.py` (the UI) and `crate_jobs.py` (background jobs) - see
section 8 for where each caller sits.

## 4. How it works - the main flow

```
data.discogs.com (monthly snapshot: releases.xml.gz, labels.xml.gz, artists.xml.gz)
       |
       v  download_dump() + verify_checksum()          [discogs_dump.py]
   discogs_dump_raw/*.xml.gz   (resumable byte-range download)
       |
       v  import_releases() / import_labels() / import_artists()
       |  (streaming XML parse, batched INSERTs into DB_PATH + ".new")
       v
  discogs_dump.sqlite3.new   (releases, release_styles, release_artists,
                              labels, artists, artist_aliases)
       |
       v  finalize_new_db(): build indexes, materialize label_styles,
       |                     ANALYZE, then os.replace() onto the live path
       v
  discogs_dump.sqlite3   <-- the ONE file every reader below queries
       |
       |  read-only lookups: search_local / label_style_counts /
       |  artist_ids_for_labels / label_ids_for_artists / resolve_name /
       |  lookup_release
       v
  scoring.py  ->  Ctx(uid)   (rebuilt fresh on every web request / job run,
       |          reads discogs_dump.sqlite3 + the user's config + JSON files)
       |
       |  wmap (taste weights per style)
       |    -> label_affinities() / style_affinity_of()   [style match]
       |  artist_tier_map() / label_tier_map()             [Coeur/Aime lists]
       |    -> artist_label_signal() / label_db_signal()   [catalog cross-links]
       |    -> graph_rescore()                             [co-credit graph]
       |    -> ascore                                      [per-artist 0-100]
       |    -> reco_rows() / reco_index                     [per-label 0-100]
       v
  album_score(release)  ->  (score 0-100, {label, artist, style} detail)
       |
       +---> radar_web/app.py routes (search results, /reco-radar, /univers, ...)
       +---> crate_jobs.py jobs (job_scan_recos scores every release on a
             followed label to build the RECOS RADAR candidate queue;
             job_build_graph reads discogs_dump's release_artists table
             directly by SQL to build co-credit edges without hitting the
             live API)
```

**Build side (discogs_dump.py).** A monthly import (`job_import_discogs_dump`,
outside this document but the only caller of the build functions) downloads
the three dump files, then calls `open_new_db()` /
`import_releases`/`import_labels`/`import_artists` / `finalize_new_db()` in
that order, all on one connection to a temporary `.new` SQLite file. Nothing
ever writes to the live `discogs_dump.sqlite3` directly - the whole book is
built to the side and swapped in with one `os.replace()` at the very end, so
a request arriving mid-import always sees either the complete old file or
the complete new one, never a half-built one.

**Serve side (discogs_dump.py).** Once built, the file is only ever opened
read-only (`connect_readonly()` / a fresh `sqlite3.connect(DB_PATH)`). Every
lookup function (`search_local`, `resolve_name`, `label_style_counts`,
`artist_ids_for_labels`, `label_ids_for_artists`, `lookup_release`,
`suggest_labels`) degrades to an empty result (`[]`/`{}`/`None`) rather than
raising, both when the dump hasn't been imported yet (`available()` is
`False`) and when it exists but predates a schema change
(`sqlite3.OperationalError` caught explicitly) - callers never need a
try/except of their own.

**Scoring side (scoring.py).** `Ctx` is cheap to construct - it just reads
small JSON files and the user's config, all of which the app already caches
by (mtime, size) via `store.load_cached`/`store.read_config`. The expensive
derived computations (`graph_rescore`, `ascore`, `reco_rows`, the two
catalog-cross-link signals) are memoized per-request behind a signature that
combines the user id with the mtimes/sizes of every input file *including*
`discogs_dump.DB_PATH` - so a fresh monthly dump import, or any job writing
to the user's own JSON files, transparently invalidates every derived score
on the next request, with no explicit cache-busting code anywhere else in
the app.

## 5. Data files

**`discogs_dump.sqlite3`** (built and served entirely by this module) -
schema:

| Table | Rows represent | Key columns |
|---|---|---|
| `releases` | one Discogs release, any format | `id`, `title`, `artist`/`artist_key`, `label`/`label_key`, `catno`, `year`, `country`, `format`, `genres`, `styles` (comma-joined), `master_id`, `is_vinyl` |
| `release_styles` | one (release, style) pair | `release_id`, `style` - lets a style be searched by index; `releases.styles` alone can't be |
| `release_artists` | one (release, artist, role) credit | `release_id`, `artist_id`, `role` in `{Main, Producer, Remix, Written-By, Featuring}` only - the roles that matter to scoring |
| `labels` | one Discogs label | `id`, `name`/`name_key`, `parent` (name), `parent_id` (Discogs id of the parent label, if declared) |
| `artists` | one Discogs artist | `id`, `name`/`name_key`, `real_name` |
| `artist_aliases` | a spelling variant -> artist id | `name_key`, `artist_id` (from the dump's `<namevariations>`) |
| `label_styles` (materialized at build time) | one (label, style) count, over the WHOLE imported catalog | `label_key`, `style`, `n` |

As of 2026-09-11, `releases` holds every format (vinyl, CD, digital file,
cassette, etc.), not vinyl-only as before that date - `is_vinyl` is computed
once per release at parse time so a consumer that wants vinyl-only can filter
on it, but nothing in either file applies that filter by default.

**`discogs_dump_meta.json`** - `{dump_date, imported_at, n_total, n_vinyl,
n_labels, n_artists}`, written once an import finishes; `Ctx` doesn't read
this file directly but the app/jobs use it to decide whether a rebuild is
needed.

**`discogs_dump_import.state.json`** - resume checkpoint (`stage`,
`releases_done`, `releases_vinyl`, ...) written after every committed batch
during an import (~1h45 end to end); read only by `job_import_discogs_dump`,
deleted on success.

**Loading pattern**: the dump is rebuilt only by the monthly job, never on
demand. `Ctx` and every lookup function open a **fresh, read-only** SQLite
connection per call (or accept a shared one passed in for a batch of
lookups) - there is no in-process cache of the dump's contents; SQLite's own
file cache and the pre-built indexes make repeated queries fast enough
without one.

**Per-user JSON files `Ctx` reads but does not write** (all owned by
`store.py` or a job outside this document's scope): the config
(`label_categories`/`artist_categories`/`scoring` weights), `labels_profile.json`
(an older, API-sampled fallback style profile - used only for labels absent
from the dump), `collection_cache.json` (owned/wanted counts from the user's
real Discogs collection), `taste_corpus.json` (everything ingested from
YouTube/Spotify/Bandcamp/DJ sets), `artists_resolved.json` (name -> canonical
Discogs id), and `producer_graph.json` (raw co-credit edges built by
`job_build_graph`, itself often populated via a direct SQL join over
`discogs_dump.sqlite3`'s `release_artists` table).

## 6. State / data flow summary

One sentence: a monthly Discogs snapshot becomes a local SQLite file, and on
every single web request or job run `Ctx` re-reads that file plus the user's
taste config and history to recompute, from scratch, the score shown for
whatever artist/label/record is on screen.

| What | Where it lives | How it changes |
|---|---|---|
| `discogs_dump.sqlite3` | one file, `SHARED_DIR/discogs_dump.sqlite3` | replaced whole, atomically, once a month by `job_import_discogs_dump` |
| `wmap` (style -> taste weight) | `Ctx._memo`, derived from `cfg["taste_categories"]` + `scoring.taste_tiers` | recomputed whenever the config file's mtime changes (Reglages save) |
| `label_tier_map`/`artist_tier_map` | `Ctx._memo`, derived from `cfg["label_categories"]`/`cfg["artist_categories"]` | changes when the user adds/moves a label or artist in "Mes labels & artistes" |
| `graph_rescore()` | `Ctx._memo`, derived from `producer_graph.json` (edges) + current tiers/weights | recomputed whenever `producer_graph.json` changes (`job_build_graph`) or tiers/weights change |
| `ascore` (per-artist 0-100) | `Ctx._memo`, combines manual tier, corpus counts, collection counts, graph proximity, DJ-set counts, and `artist_label_signal` | recomputed on every request from whichever of those five inputs last changed |
| `reco_rows()`/`reco_index` (per-label 0-100) | `Ctx._memo`, combines collection/want counts, corpus label scores, `label_artist_signal`, `label_affinities` (dump-backed), `label_db_signal` | recomputed on every request; a fresh dump import changes `label_affinities` even with nothing else touched |
| `album_score(release)` | not memoized (called per release, e.g. once per search result) - reads `reco_index`/`ascore`/`wmap` which ARE memoized | changes whenever any of its three memoized inputs changes |

## 7. Key design decisions

- **Build-to-a-side-file, then one atomic swap.** All three dumps
  (releases/labels/artists) are imported into `DB_PATH + ".new"` on the same
  connection; only `finalize_new_db()` builds indexes and calls
  `os.replace()`. A reader never sees a half-imported catalog, and an import
  killed mid-way (e.g. by a container redeploy - the worker gets redeployed
  on every merge to `main`, not just ones touching the dump) leaves the live
  file untouched.
- **WAL during the build, `DELETE` journal mode before the swap.** The
  temporary file uses `PRAGMA journal_mode=WAL` + `synchronous=OFF` while
  being written (much faster for bulk inserts), but `finalize_new_db()`
  switches it back to `DELETE` mode before the swap - otherwise every reader
  would create its own `-wal`/`-shm` files, and the next monthly
  `os.replace()` would orphan them next to the new file (a documented SQLite
  stale-read risk).
- **Resumable import with a checkpoint file.** `discogs_dump_import.state.json`
  records the stage and, for releases, the exact count already committed -
  because a full import takes ~1h45, comfortably longer than the deploy
  cycle. A checkpoint is only ever written right after a commit, so it can
  never point at uncommitted (and therefore lost) rows.
- **`is_vinyl` is a stored flag, not a live filter.** Since 2026-09-11 the
  table keeps every format, computed once at parse time; nothing in
  `discogs_dump.py` or `scoring.py` applies an `is_vinyl` filter by
  default - a consumer that wants vinyl-only has to ask for it explicitly.
  This was a deliberate widening (more complete label/artist/style
  cross-referencing) accepted along with its costs (bigger file, possible
  format duplicates in search results).
- **`normalize_label`/name-key collisions are surfaced, never guessed.**
  Discogs disambiguates same-named entities with a suffix ("Rhythm (2)"),
  and `normalize_label` strips it - so `resolve_name` can find several
  distinct Discogs entities sharing one key. Rather than pick one, it
  returns `status="ambiguous"` with all candidates; nothing downstream ever
  silently picks a random id.
- **A missing/outdated schema degrades to empty, not to a crash.** Every
  read function catches `sqlite3.OperationalError` and returns `[]`/`{}` -
  covering both "dump never imported" and "dump imported before a schema
  change, not yet re-imported this month".
- **`_robust_scale` uses the 95th percentile, never the max.** Scaling any
  count-based signal (corpus plays, collection copies, graph co-credit
  scores) by the single most prolific artist would compress everyone else's
  score toward zero; the p95 keeps one outlier from distorting the whole
  scale.
- **Scores shrink toward a neutral prior when little evidence is
  available.** `album_score` blends whichever of {label, artist, style} data
  it actually has, then pulls the result toward a neutral 45 by an amount
  inversely proportional to how much weight was actually available (`K,
  PRIOR = 0.35, 45`) - a score based on one signal never displays with the
  same apparent confidence as one based on three.
- **Every weighted sum normalizes by the sum of weights actually in play**,
  not a fixed constant - so adding a new scoring term (e.g. `label_link` to
  `ascore`) rebalances the existing terms' relative influence instead of
  silently raising the achievable ceiling above 100.
- **`artist_label_signal` cross-checks style, not just label membership.**
  Sharing a followed label used to be enough to boost any artist, even one
  completely outside the user's taste (e.g. a rap feature on an otherwise
  house label) - fixed by only counting a credit if at least one of *that
  specific release's* styles is in the user's `wmap`; a release with no
  style tag at all still counts (unclassified isn't the same as
  "known and disliked").
- **Two structurally different "does an artist I like connect to this
  label" signals coexist on purpose.** `label_artist_signal` looks only at
  the corpus actually listened to (narrow, behavioral); `label_db_signal`
  looks at the whole catalog via `discogs_dump.py` plus the co-credit graph
  (broad, structural). They deliberately overlap when a listened title is
  also in the catalog - this is treated as intentional, not redundant.

## 8. Key files table

| Path | Purpose |
|---|---|
| `radar_web/radar/discogs_dump.py` | Downloads, parses, and atomically rebuilds the local Discogs catalog index; serves every read-only lookup against it. |
| `radar_web/radar/scoring.py` | The `Ctx` class: reads the index above plus per-user config/JSON, computes every taste score in the app. |
| `radar_web/radar/store.py` (external caller) | Owns the per-user config file `Ctx` reads (`label_categories`, `artist_categories`, `scoring` weights); migrates older config shapes forward. |
| `radar_web/radar/paths.py` (external) | Defines where `discogs_dump.sqlite3` and every per-user JSON file physically live. |
| `radar_web/radar/catalog_labelgraph.py` (external, separate module) | A different, catalog-wide label<->label graph built from the same SQLite file - not consumed by `Ctx` yet. |
| `crate_jobs.py: job_import_discogs_dump` (external caller) | The only caller of `discogs_dump.py`'s download/parse/build functions; runs monthly. |
| `crate_jobs.py: job_scan_recos` (external caller) | Scores every release on a followed label via `Ctx.album_score` to build the RECOS RADAR candidate queue. |
| `crate_jobs.py: job_build_graph` (external caller) | Builds the per-user co-credit graph (`producer_graph.json`) that `Ctx.graph_rescore` consumes, via SQL over `discogs_dump.sqlite3`'s `release_artists`. |
| `crate_jobs.py: job_canonicalize` / `job_profile_labels` (external callers) | Resolve names to canonical Discogs ids and refresh the API-sampled label style fallback, both re-run automatically after a dump import. |
| `radar_web/app.py` (external consumer) | Builds a fresh `Ctx()` per request; every score-bearing page (search, Reco Radar, "Mes labels & artistes") reads from it. |

**Where to look for any feature:**
- "Why does this record/artist/label have this score?" -> `scoring.py`,
  `Ctx.album_score` / `Ctx.ascore` / `Ctx.reco_rows`.
- "Where do the local search results / label style stats come from?" ->
  `discogs_dump.py`, `search_local` / `label_style_counts`.
- "The dump is stale / import failed / resume behavior" ->
  `discogs_dump.py`'s `open_new_db`/`import_releases`/`finalize_new_db`, and
  `crate_jobs.py: job_import_discogs_dump` for how it's driven.
- "Coeur/Aime lists aren't affecting scores as expected" ->
  `scoring.py`'s `label_tier_map`/`artist_tier_map`, and `store.py`'s
  `label_categories`/`artist_categories` migration.
- "An artist/label score changed after a config edit" -> the memoization key
  in `Ctx.__init__` (`_files_sig`) - check whether the file that changed is
  actually part of that signature.
