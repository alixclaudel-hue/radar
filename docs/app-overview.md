Radar - Full App Overview

Generated from a full pass over the source code (not from prior docs). Plain ASCII only.

---

## 1. What this app is

Radar is a personal record-digging assistant built on top of Discogs (the big database/marketplace for vinyl records). Instead of endlessly scrolling Discogs hoping to spot something good, you point Radar at the things you actually listen to - YouTube playlists, Spotify playlists, Bandcamp purchases, DJ sets you like - and it quietly builds a picture of which labels and artists you gravitate toward. From that picture it scores new vinyl releases, recommends labels/artists you don't follow yet but probably would, and plays you a rotating "radio" of tracks picked to match your taste. It is a one-person tool today (built and run by its owner on a personal VPS), with a multi-user version underway so a few invited friends could eventually use their own private accounts on the same install.

## 2. Tech stack, explained in plain English

**FastAPI** - a Python web framework: it lets a Python program answer web requests (a browser hitting a URL) with either JSON or rendered HTML. In Radar, `radar_web/app.py` is one big FastAPI application with ~100 routes; every page and every button click on the site is one of its endpoints.

**HTMX** - a small JavaScript library that lets plain HTML tags trigger AJAX requests (e.g. clicking a button fetches a URL and swaps a piece of the page) without writing custom JavaScript for every interaction. Radar's UI is server-rendered HTML fragments (Jinja2 templates) that HTMX splices into the page - there is no separate React/Vue frontend.

**Jinja2 templates** - HTML files with embedded Python-like syntax, rendered server-side. `radar_web/templates/pages/*.html` are full pages; `radar_web/templates/partials/*.html` are the fragments HTMX swaps in after an action (e.g. a new results table after a search).

**SQLite** - a serverless, single-file database engine. Radar uses several separate `.sqlite3` files instead of one central database server: a huge local mirror of the entire Discogs catalog, a per-user "precalculated scores" cache, and a global label-relationship graph. This avoids running a database server and lets each dataset be rebuilt/replaced independently (atomic file swap).

**Docker Compose** - a way to describe and run several containers together as one stack. `docker-compose.yml` defines four services (`caddy`, `radar-web`, `radar-worker`, `radar-ops`) that all share the same Docker image and the same `/data` volume, so they see the same files but run as isolated processes.

**Caddy** - a reverse-proxy/web server that also handles HTTPS certificates automatically. It sits in front of `radar-web` and `radar-ops` and routes incoming traffic to the right container/port.

**Discogs API / Discogs monthly data dump** - Discogs is both queried live (search, wantlist, collection, marketplace) via its REST API, and mirrored wholesale once a month (a giant XML dump of every release/label/artist ever entered) into local SQLite so Radar doesn't have to make millions of rate-limited API calls just to browse its own copy of the catalog.

**YouTube Data API / Spotify Web API / Bandcamp (Subsonic-compatible API)** - the three "what have you been listening to" sources Radar can ingest from, each with its own quota/rate-limit quirks that the code works around (see Data files and Key design decisions).

## 3. File structure

```
radar-work/                          (repo root)
  crate_jobs.py                      -> CLI entry point + JOBS registry (job name -> radar_jobs module)
  docker-compose.yml                 -> defines caddy, radar-web, radar-worker, radar-ops
  Caddyfile                          -> reverse-proxy routing/HTTPS config
  .github/workflows/
    deploy.yml                       -> on push to main: SSH to VPS, git reset --hard, rebuild, health-check, rollback on failure
    ci.yml                           -> starts a real server (RADAR_NO_AUTH=1) and exercises web + radar_ops routes

  radar_web/                         -> the main product: FastAPI + HTMX web app
    app.py                           -> ~100 HTTP routes, auth middleware, multi-user (uid) plumbing (2559 lines)
    worker.py                        -> background job runner, serial queue, auto-maintenance scheduler (403 lines)
    Dockerfile, requirements.txt
    radar/                           -> the "radar" Python package: all business logic app.py calls into
      paths.py                      -> per-uid data directory layout, CRATE_DATA_DIR, path-traversal guards
      websession.py                 -> session cookies (HMAC signed uid.expiry.sig), CSRF origin check, dev_mode
      accounts.py                   -> user accounts, scrypt password hashing, invite tokens
      store.py                      -> atomic JSON load/save, per-request current-uid contextvar, default scoring config
      jobs.py                       -> job queue API (launch/status/stop/reap_orphans) used by app.py
      scorestore.py                 -> per-user SQLite of precalculated release/track scores
      scoring.py                    -> the "brain": Ctx class, scoring DAG, album_score/ascore/reco_rows (904 lines)
      discogs.py                    -> Discogs API client (search, wantlist, seller inventory, release lookup)
      discogs_dump.py               -> local mirror of the full Discogs monthly dump in SQLite (1428 lines)
      catalog_labelgraph.py         -> global label-to-label graph built from the dump (shared, taste-agnostic)
      labelgraph.py                 -> per-user visual label graph for the /univers page
      artistgraph.py                -> per-user visual artist graph (ego-network) for /univers
      textmatch.py                  -> shared token-overlap text matching (used by YouTube/Bandcamp/Volumo matching)
      ytcache.py                    -> YouTube API client + cache + quota/rate-limit handling (457 lines)
      bandcamp.py                   -> Bandcamp search (unofficial autocomplete API) for track/album links
      volumo.py                     -> Volumo shop search integration (button currently hidden in UI)
      sellers.py / sellers_seed.py  -> Discogs seller catalog, inventory snapshots, seed list of known shops
      recoindex.py                  -> optional disk cache of the recommendation index (opt-in, off by default)
      learn.py                      -> thumbs up/down feedback loop, logistic-regression weight tuning
      opslog.py                     -> append-only JSONL action log consumed by radar_ops
      vocab.py                      -> canonical Discogs genre/style vocabulary lists
      features.py                   -> feature flags (VEILLE_ENABLED, SELLERS_ENABLED - both False/paused)
      codeversion.py                -> reads the running code's git SHA (RADAR_CODE_SHA in prod)
    templates/
      pages/*.html                  -> disco, feedback, patte, reco_radar, search, settings, univers, veille, wantlist
      partials/*.html               -> ~25 HTMX fragments (results, tracklist, labels_table, cart, sets, review, ...)
    static/
      app.css, feedback.js, wltoggle.js, wradar.js

  radar_jobs/                        -> background job implementations, one module imported per job name
    common.py                       -> shared paths, atomic JSON I/O, cfg_load (secrets only for owner), Job class
    dump.py                         -> import_discogs_dump, build_catalog_labelgraph
    ingest.py                       -> ingest_youtube, ingest_spotify, ingest_bandcamp, fetch_collection, merge_corpus, profile_labels
    djsets.py                       -> ingest_djsets (yt-dlp search + Playwright scraping of YouTube's Music panel)
    curation.py                     -> enrich, canonicalize, prune_labels
    graph.py                        -> build_graph (BFS co-credit graph), resolve_artists
    scorestore.py                   -> scorestore_releases, scorestore_tracks, reco_index
    search.py                       -> seller_inventory (and other search/scan jobs)
    recos.py                        -> scan_recos, publish_recos (feeds the RECOS RADAR playlist)
    sources.py                      -> low-level API clients shared by ingest jobs (Discogs/YouTube/Spotify/Bandcamp)
    tracks.py                       -> shared track-cleaning helpers (placeholder/megamix filtering, identity keys)
    __init__.py

  radar_ops/                         -> read-only diagnostics service (separate container, port 8610)
    app.py                          -> routes: /, /bases, /scoring, /delegation, /delegation/workflows, /actions
    freshness.py                    -> compares stored vs current scoring fingerprint per user
    probe.py                       -> snapshot of the global job queue and all status files
    inventory.py                   -> read-only inventory of every JSON file and SQLite DB under /data
    sampler.py                     -> in-memory throughput/ETA estimation for running jobs
    workflows.py                   -> reads ingested Claude Code transcripts, renders Sankey of token flow
    delegation.py                  -> reads ai_broker receipts/telemetry, renders cost/quota/health dashboard
    templates/*.html                -> jobs, bases, scoring, delegation, workflows, actions, login, base

  scripts/                           -> dev tooling (not part of the running product)
    ai_broker.py                    -> multi-provider delegation gateway (free-first, then paid) used by Claude Code itself
    ai_query.py, ai_worker.py       -> underlying Gemini transport + worker
    workflow_ingest.py              -> ingests Claude Code transcripts for radar_ops' /delegation/workflows
    ai/                              -> router, health/circuit-breaker, quota, context cache, schema validation, redaction
    hooks/                           -> delegation_gate.py (blocks ungated Read/Grep/commit), telemetry.py, session_context.py

  archive/                           -> DEAD Streamlit prototype - not documented further, never touch/extend
  docs/                              -> architecture.md, prior overviews/diagrams, ROADMAP, ops runbooks
  tests/                             -> unittest suite (36+ files), fixtures/ytcache
  config/ai_models.json              -> model catalogue for the delegation broker
  data/                               -> runtime data volume (JSON + SQLite), never in git
```

## 4. How the app works - the main flow

```
Browser
   |
   v
Caddy (HTTPS, routing)              radar_ops (port 8610, read-only, Tailscale-only)
   |                                        ^
   v                                        | reads the SAME /data volume
radar-web  (FastAPI, port 8600)             |
   |  every request:                        |
   |   1. _guard middleware: session cookie / CSRF origin check
   |   2. store.set_current_uid(uid)  -> isolates all file/db access to that user
   |   3. route handler runs, reads/writes JSON+SQLite under /data/users/<uid>/ or /data/shared/
   |   4. opslog.record(...) appends one line describing the request
   v
Jinja2 template (page or HTMX partial) rendered back to the browser
   |
   v (for anything slow: import, ingest, scoring, graph build)
POST /jobs/<name>/launch
   |
   v
radar.jobs.launch()  -> appends to /data/jobs/queue.json (priority 1=click, 0=background)
   |
   v
radar-worker (separate container, its own process, SERIAL execution only)
   |  picks next job: priority first, then round-robin across users
   v
subprocess: python crate_jobs.py <name> '<params>'   (env: RADAR_UID, CRATE_DATA_DIR)
   |
   v
crate_jobs.py looks up JOBS[name] -> imports ONLY that radar_jobs/<module>.py -> calls job_<name>(job, params)
   |
   v
job writes progress to /data/jobs/<uid>/<name>.status.json, writes its results to JSON/SQLite under /data
   |
   v
GET /jobs/<name>/status  (polled by HTMX every ~1s while a job runs) -> progress bar / ETA
```

Concepts worth understanding, not just files:

- **Multi-user isolation by convention, not by database.** Every user gets a folder `data/users/<uid>/`; genuinely shared, taste-agnostic data (the Discogs mirror, the YouTube cache, the seller catalog) lives in `data/shared/`. A `contextvars`-based "current uid" is set once per request/job and threaded through everything, so the same code path serves the owner and any guest account without an explicit `uid` parameter everywhere.
- **The worker is a single serial queue on purpose.** Discogs rate-limits by source IP, not per account, so if two users' jobs ran in parallel they'd fight over the same budget. Priority 1 (a user clicked a button) always jumps ahead of priority 0 (nightly maintenance); round-robin by uid stops one user's big backlog from starving everyone else.
- **Jobs run as real OS subprocesses, not in-process functions.** `crate_jobs.py` is invoked as `python crate_jobs.py <job> <params>` so a crashing or infinite-looping job can never take down the web app or the worker's own process; it just leaves a `.status.json` that says it never finished, cleaned up by `reap_orphans()` on the next worker boot.
- **radar_ops is a second, independent, read-only window onto the same /data volume.** It never writes business data (only session bootstrapping); if radar-web or the worker is stuck, ops can still be opened to see why.
- **Scoring is precomputed, not calculated live per page view.** `scorestore_releases`/`scorestore_tracks` jobs walk every followed label's catalog and write a score per release/track into a per-user SQLite database ahead of time; the live scoring engine (`scoring.py`) is still what computes the number, but the expensive "which labels/artists do I like" indexing happens off the request path.

## 5. Data files

**`data/shared/discogs_dump.sqlite3` + `discogs_dump_tracks.sqlite3`** (`radar_web/radar/discogs_dump.py`) - a full local mirror of Discogs' monthly XML dump: `releases` (all formats, `is_vinyl` flag), `release_styles`, `labels` (with parent/sublabel hierarchy), `artists` + `artist_aliases`, `release_artists` (credits limited to roles that matter for scoring: Main/Producer/Remix/Written-By/Featuring), `label_styles` (materialized style profile per label), and a separate tracklists database. Rebuilt monthly by the `import_discogs_dump` job (~1h45, resumable, atomic `os.replace` swap so the site never reads a half-built copy); consumed constantly by local search, ranking, the co-credit graph, and score precomputation.

**`data/shared/catalog_labelgraph.sqlite3`** (`catalog_labelgraph.py`) - global, taste-agnostic label-to-label graph (`label_pairs`: shared-artist links and parent/sublabel links) rebuilt whenever the dump changes; feeds the "neighborhood" component of scoring so a label can be recommended even if the user has never heard it, purely by proximity to labels they do like.

**`data/users/<uid>/scorestore.sqlite3`** (`radar/scorestore.py`, `radar_jobs/scorestore.py`) - per-user precalculated scores: `release_scores`, `track_scores`, `scorestore_meta` (fingerprint of the scoring config/code used, so `radar_ops` can flag stale scores). Rebuilt incrementally in rotation by background jobs, WAL mode for concurrent reads.

**`data/users/<uid>/crate_radar_config.json`** - the user's declared taste: followed labels/artists (Coeur/Aime tiers), style categories, veille rules, per-service connection info.

**`data/users/<uid>/taste_corpus.json`** - the ingested listening history (YouTube/Spotify/Bandcamp/DJ-set tracks), tagged by source, feeding scoring's "corpus" signal.

**`data/users/<uid>/producer_graph.json`** - the personal co-credit graph (raw edges) built by `build_graph`, consumed by `artistgraph.py`/`labelgraph.py` for the `/univers` visualizations and by scoring's graph-proximity signal.

**`data/users/<uid>/collection_cache.json`** - synced snapshot of the user's Discogs collection + wantlist; used both to know what's already owned (excluded from recos) and as a scoring input.

**`data/users/<uid>/recos_playlist.json`, `recos_candidates.json`, `recos_history.json`, `recos_search_budget.json`** - the RECOS RADAR pipeline's state: current playlist, pending candidates awaiting a YouTube match, permanent dedup history (never purged), and the daily YouTube-search spend counter (resets at Pacific midnight).

**`data/shared/youtube_cache.json`** (`ytcache.py`) - cache of YouTube search results, keyed by a hash of query+artist+title+label; successes never expire by age (only by a 20,000-entry size cap), failures expire after 6 hours so a track can be retried later.

**`data/jobs/queue.json`, `data/jobs/<uid>/<job>.status.json`** - the global job queue and per-job-per-user status/progress files read by both `radar_web/worker.py` and `radar_ops/probe.py`.

**External APIs consumed live**: Discogs REST API (search, wantlist, seller inventory, release detail - `discogs.py`), YouTube Data API v3 (search + video lookup - `ytcache.py`), Spotify Web API (playlist tracks, OAuth client-credentials), Bandcamp's Subsonic-compatible API (collection) and public autocomplete API (link matching), Volumo's internal JSON search API.

## 6. State / data flow summary

One sentence: a user's declared taste plus everything they've actually listened to or bought flows into precomputed per-user SQLite scores, which are combined at request time with the global Discogs mirror and label graph to rank searches, drive two recommendation surfaces (label/artist suggestions on `/univers`, and an autoplaying track queue on `/reco-radar`), while a single serial background worker keeps all of that data fresh without ever touching the request path directly.

| What | Where it lives | How it changes |
|---|---|---|
| Current user (uid) | `contextvars` set by `_guard` middleware from the session cookie | New value every request, read from `radar_auth` cookie |
| Session validity | HMAC token `uid.expiry.sig` in `radar_auth` cookie | Signed with a secret + password-epoch, so changing your password revokes all sessions |
| Declared taste (labels/artists, tiers) | `crate_radar_config.json` per user | Edited on `/univers`; also touched by `merge_corpus`, `canonicalize`, `prune_labels` jobs |
| Listening history | `taste_corpus.json` per user | Appended by `ingest_youtube/spotify/bandcamp`, `ingest_djsets` jobs |
| Precalculated release/track scores | `scorestore.sqlite3` per user | Rebuilt in rotating batches by `scorestore_releases`/`scorestore_tracks` jobs |
| Reco index (label scores) | in-memory `Ctx.reco_index`, optional disk cache via `recoindex.py` | Recomputed per request unless `RADAR_RECO_INDEX=1` cache is fresh |
| Job queue | `data/jobs/queue.json` | Appended by any `POST /jobs/<name>/launch` or worker auto-maintenance tick |
| RECOS RADAR playlist | `recos_playlist.json` | `scan_recos` proposes candidates, `publish_recos` resolves a YouTube video and inserts; user "mark played" purges at Paris midnight |
| Feature flags | `radar_web/radar/features.py` (module constants) | Edited in code + app/worker restart; not a runtime toggle |

## 7. Key design decisions

- **`discogs_get()` never raises** - it always returns `{}` on failure, so callers must treat "empty dict" as the failure signal rather than expecting an exception.
- **Two separate SQLite databases for the Discogs mirror** (`discogs_dump.sqlite3` and `discogs_dump_tracks.sqlite3`) so a tracklist-parsing bug can never corrupt or block the main catalog import.
- **The mirror stores every format, not just vinyl** ("exhaustive at discovery time, vinyl at purchase time" - a deliberate product decision): `is_vinyl` is a flag, and `search_local()` filters to vinyl by default except for the `scorestore_releases` job, which needs the full picture of a label's catalog to score it properly.
- **`features.py` is a separate module from `app.py` on purpose**: the worker runs in its own process and never imports `app.py`, so a flag has to live somewhere both processes import, or disabling a feature on the web side wouldn't stop the worker from still queuing it.
- **YouTube quota resets at Pacific midnight, not local midnight** - the daily search budget counter uses `America/Los_Angeles` explicitly to match Google's actual reset time, avoiding a spurious ~9-hour lockout if it used Paris time instead.
- **RECOS RADAR playlist has no FIFO auto-eviction** - a full playlist simply stops accepting new tracks until the user marks one as played; this is intentional so a queued recommendation is never silently bumped.
- **`label_style_counts` batches its SQL by ~900 keys** to stay under SQLite's `SQLITE_MAX_VARIABLE_NUMBER`; going over it used to fail silently and quietly break the affinity signal.
- **Scoring never treats "no data" as "zero"** - an unknown style affinity (`aff=None`) is excluded from the weighted average rather than counted as a bad score, so new/undiscovered labels aren't unfairly buried.
- **Scoring uses percentile-95 robust scaling + log1p everywhere**, never a raw max(), so one wildly over-represented artist/label can't compress everyone else's score toward zero.
- **`cfg_load()` only injects `.env` secrets (Discogs/Spotify/YouTube tokens) if the current uid is the owner** - a guest account never gets implicit access to the admin's API credentials.
- **`radar_ops` is read-only by contract**: the only writes it can ever make are bootstrapping `/data` directory structure and `.session_secret` on first boot; it exists specifically so it stays usable when `radar-web` is broken.
- **Volumo/Veille/Sellers features are code-complete but paused** (`VEILLE_ENABLED = SELLERS_ENABLED = False`, Volumo's UI button removed) - the backend logic, jobs, and data are all intact; re-enabling is a flag flip + restart, not a rewrite.

## 8. Key files table

| File | Purpose |
|---|---|
| `radar_web/app.py` | Every HTTP route, auth/session middleware, multi-user request context |
| `radar_web/worker.py` | Serial background job runner + auto-maintenance scheduler |
| `crate_jobs.py` | CLI entry point + `JOBS` registry mapping job name -> `radar_jobs` module |
| `radar_web/radar/scoring.py` | The scoring engine: `Ctx`, dependency DAG, `album_score`/`ascore`/`reco_rows` |
| `radar_web/radar/discogs_dump.py` | Local full mirror of the Discogs catalog (SQLite), search/resolve/lookup API |
| `radar_web/radar/paths.py` | Per-user data directory layout and path-traversal guards |
| `radar_web/radar/websession.py` | Session cookies, CSRF origin check, dev_mode fallback |
| `radar_web/radar/store.py` | Atomic JSON I/O, current-uid contextvar, default scoring config |
| `radar_web/radar/jobs.py` | Job queue API used by `app.py` (launch/status/stop) |
| `radar_web/radar/ytcache.py` | YouTube API client, cache, quota/rate-limit handling |
| `radar_jobs/recos.py` | `scan_recos`/`publish_recos` - the RECOS RADAR pipeline |
| `radar_jobs/dump.py` | `import_discogs_dump`/`build_catalog_labelgraph` |
| `radar_ops/app.py` | Read-only diagnostics service routes |
| `docker-compose.yml` | Defines caddy / radar-web / radar-worker / radar-ops services |
| `docs/architecture.md` | Multi-user chantier plan (data model, OAuth, encryption, roadmap) |

**Where to look for any feature:**
- Search/browse vinyl -> `app.py` routes under "Recherche de disques", `discogs_dump.search_local`, `scoring.album_score`
- Label/artist affinity & recommendations -> `scoring.py`, `catalog_labelgraph.py`, `radar_jobs/scorestore.py`
- "Mon profil" / connecting listening sources -> `app.py` `/patte*` routes, `radar_jobs/ingest.py`, `radar_jobs/sources.py`
- RECOS RADAR playlist -> `radar_jobs/recos.py`, `app.py` `/reco-radar*` routes, `templates/pages/reco_radar.html`
- Wantlist / cart -> `app.py` `/cart*` and `/wantlist` routes, `radar/discogs.py` wantlist functions
- Background jobs / job status UI -> `radar_web/worker.py`, `radar/jobs.py`, `app.py` `/jobs/*` routes, `radar_ops/probe.py`+`sampler.py`
- Multi-user accounts/auth -> `radar/accounts.py`, `radar/websession.py`, `radar/paths.py`
- Diagnostics / delegation cost tracking -> `radar_ops/` (whole package), especially `delegation.py` and `workflows.py`
