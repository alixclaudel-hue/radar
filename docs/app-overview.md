# Radar - App Overview

## 1. What this app is

Radar is a personal crate-digging assistant built on top of Discogs (the vinyl
record database/marketplace). It watches the music you already listen to
(YouTube, Spotify, Bandcamp, DJ sets), figures out which labels and artists
you actually like, and then scores new and old vinyl releases against that
taste profile so you can find records worth buying instead of browsing
Discogs blind. It also keeps an internal "radar" playlist of fresh YouTube
finds picked to match your taste, and can point you to a record shop that
actually has a given release in stock.

It is a single-owner tool today (one real user), but it is mid-way through
being turned into a small multi-user app for a handful of invited people.

## 2. Framework / Tech Stack explainer

**FastAPI** - a Python web framework for building HTTP APIs and server-rendered
pages. In Radar it is the entire web server: `radar_web/app.py` defines every
page route, form submission, and JSON/HTML fragment endpoint (about 80 routes
in one file), plus an authentication middleware that runs before every
request.

**HTMX** - a small JavaScript library (loaded from a CDN) that lets plain HTML
trigger AJAX requests and swap in the HTML that comes back, without writing
custom JavaScript or a frontend framework. Radar's pages are classic
server-rendered HTML; HTMX is what makes buttons, forms, and autocomplete
boxes update part of the page (a table, a graph, a status line) without a
full reload.

**Jinja2** - a Python templating language that turns data into HTML. Every
page and reusable fragment in `radar_web/templates/` is a Jinja2 template;
FastAPI renders them with the current user's data on each request.

**SQLite** (via Python's built-in `sqlite3`) - a serverless, single-file SQL
database. Radar uses it as a local read-only mirror of Discogs' public
monthly data dump (releases, labels, artists, styles) so that searching and
scoring never depend on hitting the live Discogs API.

**YouTube Data API / Spotify Web API / Discogs API / Bandcamp & Volumo search
endpoints** - external HTTP services the app calls for listening-history
ingestion, catalog lookups, and shop-availability checks; see section 5.

**yt-dlp / Playwright** (in `requirements.txt`, used only by `crate_jobs.py`
subprocesses, not by the web server itself) - `yt-dlp` extracts metadata from
YouTube playlists during ingestion; Playwright is a browser-automation library
that was used for a now-removed feature (see Key Design Decisions).

**Docker Compose + Caddy** - the app ships as three containers: the FastAPI
app, a background worker, and Caddy (a reverse proxy that can auto-provision
HTTPS certificates). Caddy is wired up but HTTPS is not live yet (needs a
domain name, see section 7).

## 3. File structure and routes

```
radar_web/
  app.py            -> the whole HTTP surface: ~80 routes (pages + HTMX
                        fragments + form posts), session auth middleware,
                        cookie-based login, CSRF-by-origin check
  worker.py          -> long-running process (separate container): drains the
                        job queue one job at a time, round-robin across users;
                        also fires scheduled maintenance (weekly seller scan,
                        monthly Discogs dump refresh, weekly graph/label
                        maintenance, hourly RECOS scan - currently disabled,
                        see section 7)
  Dockerfile          -> builds the radar-web / radar-worker image
  README.md           -> short dev-run notes
  radar/                          (the app's Python package - business logic)
    paths.py           -> resolves every data file path, per-user vs shared,
                          under /data/users/<uid>/ and /data/shared/
    store.py           -> atomic JSON load/save, current-user contextvar,
                          label/style name normalization, config access
    accounts.py         -> user accounts (scrypt password hashing), invites,
                          owner bootstrap from APP_PASSWORD
    discogs.py           -> thin Discogs REST API client (get/search wrappers)
    discogs_dump.py       -> builds/queries the local SQLite mirror of
                            Discogs' monthly catalog dump (releases, labels,
                            artists, styles, aliases)
    scoring.py            -> the "brain": Ctx class loads a user's whole
                            profile and computes label/artist/album scores
                            and recommendation rows
    labelgraph.py          -> label<->label graph (shared artists) computed
                            from the cached producer graph, no API calls
    artistgraph.py          -> artist<->artist ego-network graph, same source
    learn.py                -> logs thumbs up/down feedback and nudges scoring
                            weights (a small hand-rolled logistic regression)
    jobs.py                  -> job queue file format + status files under
                            /data/jobs/<uid>/
    sellers.py                -> shared Discogs seller catalog + inventory
                            snapshot cache, "seller affinity" scoring
    sellers_seed.py            -> hand-compiled seed list of electronic-music
                            record shops' Discogs usernames
    ytcache.py                  -> shared YouTube Data API cache + API-key
                            cascade (personal key -> app key) + video search
                            with metadata-based relevance scoring
    bandcamp.py                  -> Bandcamp search via its public
                            autocomplete endpoint, with a search-page fallback
    volumo.py                     -> Volumo (DJ record shop) search via its
                            internal JSON API; no fallback link if unsure
    textmatch.py                  -> shared word-overlap text similarity used
                            by ytcache/bandcamp/volumo/tracklist matching
    vocab.py                       -> canonical Discogs genre/style lists for
                            the search form
  templates/
    base.html                     -> HTML shell: nav bar, HTMX script tag,
                            login/session bar, combobox/chip-input JS
    pages/                        -> one file per top-level nav page: patte
                            (Mes sources), search, veille (Nouveautés),
                            univers (Mes labels & artistes), reco_radar,
                            settings, disco, feedback
    partials/                      -> ~25 HTMX-swappable fragments: tables,
                            graphs (SVG), suggest/autocomplete panels, cart,
                            tracklists, job status widgets, etc.
  static/
    app.css                        -> all styling (no CSS framework)
    feedback.js, wltoggle.js, wradar.js -> small page-specific behaviors
      (feedback widget, wantlist toggle, the radar/star weight-editing widget)
crate_jobs.py           -> all long-running background jobs, run as a
                          subprocess (`python crate_jobs.py <job> <params>`)
                          by worker.py or on demand from a page button
docker-compose.yml       -> 3 services: caddy, radar-web, radar-worker
Caddyfile                -> reverse proxy config, HTTPS if RADAR_DOMAIN is set
.env.example              -> required secrets/config template
scripts/
  backup.sh                -> nightly encrypted backup of /data (excludes the
                          Discogs SQLite dump, see Key Design Decisions)
  cloud-setup.sh             -> environment bootstrap for this cloud session
.github/workflows/
  ci.yml                     -> py_compile + import + route smoke test on
                          every PR/non-main push
  deploy.yml                  -> on push to main: SSH to the VPS, git pull,
                          docker compose rebuild, health-check, auto-rollback
archive/                       -> retired Streamlit app - do not modify or
                          build on top of it
docs/
  architecture.md               -> multi-user migration plan (per-user vs
                          shared data, OAuth plan, remaining blocked steps)
  backup.md                     -> backup strategy detail
  archive/                      -> superseded docs, kept for reference
CLAUDE.md                      -> living project memory: state, TODOs, and a
                          long list of learned pitfalls/gotchas
claude_archive.md               -> older detailed history (Discogs dump,
                          scoring internals, RECOS RADAR feature log)
```

## 4. How the app works - the main flow

```
Browser (HTMX-enhanced HTML, no SPA framework)
        |
        v
  auth middleware (app.py: _guard)
   - reads session cookie, verifies HMAC signature
   - sets the request's user id into a contextvar (store.set_current_uid)
        |
        v
  route handler (app.py)
   - loads that user's JSON files via radar.store / radar.paths
   - for scoring pages: builds a scoring.Ctx (loads config + corpus + graph +
     labels/artist profiles once per request)
   - for slow work (ingest, scans, graph rebuilds): pushes a job onto the
     queue (radar.jobs) instead of doing it inline
        |
        v
  Jinja2 template renders full page or an HTML fragment
        |
        v
  Browser (HTMX swaps the fragment in place, or full page navigation)

Background (separate container, radar_web/worker.py):
  loop: pop next queued job (round-robin across users)
    -> subprocess: python crate_jobs.py <job> <params>
    -> writes progress to /data/jobs/<uid>/<job>.status.json
  also, when idle: fires scheduled maintenance (weekly seller scan, monthly
  Discogs dump sync, weekly/monthly scoring maintenance)
```

Requests are synchronous and stateless per-page: nothing is held in server
memory between requests except the job queue on disk. All "taste" data lives
in per-user JSON files re-read (with mtime-based caching) on each request, so
the scoring logic always reflects the latest saved state without a restart.
Anything that could take more than a couple seconds (API ingestion, rebuilding
the artist/label graph, scanning seller inventories, Discogs dump import) is
handed off to `crate_jobs.py`, run out-of-process by the worker so a slow job
never blocks the web server or another user's request.

## 5. Data files

| Source | What it holds | How/when it's loaded |
|---|---|---|
| `users/<uid>/crate_radar_config.json` | user's taste config: watched labels, taste categories, artist categories, scoring weights, Discogs token | read on every request via `store`/`scoring.Ctx` |
| `users/<uid>/taste_corpus.json` | tracks ingested from YouTube/Spotify/Bandcamp/DJ sets, deduplicated | read by scoring; appended by ingest jobs |
| `users/<uid>/labels_profile.json` | per-label style profile, built from Discogs credits | read by scoring; rebuilt weekly by `profile_labels` job |
| `users/<uid>/producer_graph.json` | multi-level co-credit graph (artist/label edges) seeded from taste | read by labelgraph/artistgraph; rebuilt monthly by `build_graph` |
| `users/<uid>/collection_cache.json` | cached Discogs collection + wantlist | refreshed by `fetch_collection` job |
| `users/<uid>/recos_playlist.json`, `recos_candidates.json`, `recos_seen.json`, `recos_playlist_history.json` | RECOS RADAR internal playlist state (max 5 tracks, FIFO) | written by `scan_recos`/`publish_recos` jobs; read by `/reco-radar` |
| `users/<uid>/reco_feedback.json` | thumbs up/down log used to nudge scoring weights | written by `learn.log`, read on scoring |
| `shared/discogs_dump.sqlite3` (~3 GB) | local mirror of Discogs' monthly catalog dump: releases, labels, artists, styles, aliases (vinyl only, catalog fields only - no marketplace data) | built/refreshed monthly by `import_discogs_dump`; queried on every targeted search |
| `shared/lookup_cache.json`, `release_meta_cache.json` | public Discogs release/artist/label metadata cache, shared across users | read-through cache, filled on demand |
| `shared/youtube_cache.json`, `bandcamp_cache.json`, `volumo_cache.json` | cached search results for public YouTube/Bandcamp/Volumo lookups (saves API quota) | read-through cache with TTLs (7d/24h for YouTube depending on hit/miss, 30d for Bandcamp/Volumo) |
| `shared/sellers_catalog.json`, `shared/seller_inventory/<user>.json` | curated Discogs seller directory + latest "for sale" snapshot per seller | seeded from `sellers_seed.py`, refreshed by weekly `scan_catalog` job |
| `accounts.json`, `invites.json` (repo root of `/data`) | app login accounts (scrypt-hashed passwords) and pending invites | read by `accounts.py` on every login/auth check |
| Discogs REST API, YouTube Data API, Spotify Web API, Bandcamp autocomplete endpoint, Volumo JSON API | live external services for search, ingestion, and metadata resolution | called on demand, mostly from `crate_jobs.py`; wrapped by cache layers above |

## 6. State / Data flow summary

Taste data starts as raw listening history (YouTube/Spotify/Bandcamp/DJ sets)
or a Discogs collection, gets normalized and merged into per-user JSON files
by background jobs, is turned into label/artist/style profiles and a
co-credit graph, and is finally combined by `scoring.Ctx` into scores that
drive every page's recommendations and search results.

| What | Where it lives | How it changes |
|---|---|---|
| Logged-in user id | HMAC-signed cookie, resolved into a contextvar per request | Login/logout; expires after 5h |
| Taste config (watched labels, weights) | `crate_radar_config.json` | Edited on `/settings` and `/univers` (add/remove label, tune weight sliders) |
| Listening corpus | `taste_corpus.json` | Grows via `ingest_youtube`/`ingest_spotify`/`ingest_bandcamp`/`ingest_djsets` jobs |
| Label/artist scores | Computed in-memory each request by `scoring.Ctx`, cached by file mtime | Recomputed whenever any input JSON file changes |
| Producer graph | `producer_graph.json` | Rebuilt monthly (or on demand) by `build_graph` |
| RECOS RADAR playlist | `recos_playlist.json` (max 5 tracks, FIFO eviction) | `scan_recos` finds candidates, `publish_recos` searches YouTube and appends; manual delete via `/reco-radar/delete` |
| Job progress | `/data/jobs/<uid>/<job>.status.json` | Written by `crate_jobs.Job`, polled by the page via HTMX |
| Discogs catalog mirror | `shared/discogs_dump.sqlite3` | Rebuilt wholesale monthly (never incremental) |

## 7. Key design decisions

- **The Discogs marketplace (prices, live listings) is never fetched** -
  Cloudflare blocks it. Radar only mirrors the public catalog dump and shows
  a plain search link plus an availability badge instead.
- **RECOS RADAR is not a real YouTube playlist.** It is an internal JSON file
  played back client-side through the YouTube IFrame Player API
  (`cuePlaylist`) - no Google OAuth app, no account linking.
- **The automatic RECOS scan is intentionally disabled right now**
  (`worker.py`, `_maybe_recos_scan` returns immediately) - a deliberate pause
  from a 2026-09-10 user request during a test phase; feeding the playlist is
  manual-only from `/reco-radar` until that `return` is removed.
- **The VPS diagnostic loop (`diag-vps` trigger) is also intentionally
  paused** - judged unreliable, not to be re-enabled without an explicit
  request.
- **YouTube search scores results instead of trusting the first hit** - after
  repeated bad matches, it now queries with the label included, checks 15
  results, and scores by title/channel/description overlap; a quota-exhausted
  search must never be cached as "no match found" (a past bug that froze
  tracks for 7 days).
- **Per-user vs shared data is a strict, audited split** (`docs/architecture.md`):
  anything specific to one person's taste is per-user; only neutral public
  Discogs/YouTube metadata is shared, to keep the multi-user migration safe.
- **Job launches must pass `force` explicitly in Python code**, not rely on
  FastAPI's `Form(...)` default - calling a route handler directly (not
  through HTTP) leaves the `Form` object truthy, which silently forced a full
  rescan on every click of a button that was supposed to respect the cache.
- **The nightly backup excludes the Discogs SQLite dump** (~3 GB,
  reconstructible from the monthly source) after it caused a disk-full
  incident that blocked a deployment.
- **The Streamlit app under `archive/` is dead** and must never receive new
  features - FastAPI + HTMX is the only active frontend.
- **HTTPS/OAuth (Discogs, Spotify) is blocked on getting a domain name** -
  Caddy is already wired into `docker-compose.yml` but currently serves over
  plain HTTP because no domain is configured yet.

## 8. Key files table

| File | Purpose |
|---|---|
| `radar_web/app.py` | All ~80 HTTP routes, auth middleware, page/fragment rendering |
| `radar_web/worker.py` | Background job-queue runner + scheduled maintenance |
| `crate_jobs.py` | Every long-running job (ingest, scans, graph/profile rebuilds, dump import) |
| `radar_web/radar/scoring.py` | The scoring "brain" (`Ctx`, `album_score`, `ascore`, `reco_rows`) |
| `radar_web/radar/paths.py` | Per-user vs shared data path resolution |
| `radar_web/radar/store.py` | JSON load/save, current-user context, config helpers |
| `radar_web/radar/discogs_dump.py` | Local SQLite mirror of the Discogs catalog dump |
| `radar_web/radar/accounts.py` | Login accounts, password hashing, invites |
| `radar_web/radar/ytcache.py` | YouTube search + cache + API-key cascade + relevance scoring |
| `radar_web/radar/sellers.py` | Seller catalog + inventory snapshots + affinity scoring |
| `radar_web/templates/base.html` | HTML shell + nav + shared client-side JS (combobox/chips) |
| `docker-compose.yml` | Service topology: caddy, radar-web, radar-worker |
| `.github/workflows/deploy.yml` | Push-to-main deploy pipeline with health-check rollback |
| `CLAUDE.md` | Living project memory: state, TODOs, and known pitfalls |
| `docs/architecture.md` | Multi-user migration plan and remaining blocked steps |

**Where to look for any feature:**
- Login/accounts/invites -> `radar_web/radar/accounts.py`, routes in `app.py` around `/login`, `/register`, `/account/invite`
- Scoring/recommendations math -> `radar_web/radar/scoring.py`
- "Mes sources" ingestion (YouTube/Spotify/Bandcamp/DJ sets) -> `crate_jobs.py` (`job_ingest_*`), page `templates/pages/patte.html`
- Targeted record search -> `app.py` `/search*` routes, `radar_web/radar/discogs_dump.py`
- "Mes labels & artistes" graphs and tables -> `app.py` `/univers*` routes, `labelgraph.py`/`artistgraph.py`
- RECOS RADAR playlist -> `crate_jobs.py` (`job_scan_recos`, `job_publish_recos`), `app.py` `/reco-radar*`, `templates/pages/reco_radar.html`
- Shop availability badge -> `radar_web/radar/volumo.py`, `radar_web/radar/sellers.py`
- Background jobs/queue -> `radar_web/radar/jobs.py`, `radar_web/worker.py`
- Deployment/CI -> `.github/workflows/`, `docker-compose.yml`, `Caddyfile`
