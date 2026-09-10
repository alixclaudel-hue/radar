# Radar - App Overview

## 1. What this app is

Radar is a personal "crate-digging" assistant for buying vinyl records. It watches
what its owner actually listens to (YouTube playlists, Spotify, Bandcamp purchases,
scanned DJ sets), builds a taste profile from that listening history plus a Discogs
collection, and then scores new and existing vinyl releases against that taste
profile - so the owner can spot records worth buying instead of manually browsing
thousands of new releases from followed labels and artists. It also runs a small
internal "radio" (RECOS RADAR) that queues up YouTube tracks matching the owner's
taste and plays them back to back. The project is mid-way through becoming
multi-user (invite-only, a handful of people), but today it is effectively a
single-user tool ("owner").

## 2. Framework / tech stack explainer

- **FastAPI** - a Python web framework for building the app's server and HTTP
  routes. It is the backbone of `radar_web/app.py`: every page and every button
  click is one `@app.get`/`@app.post` route that reads/writes JSON files and
  renders HTML.
- **HTMX** - a small JavaScript library (loaded from a CDN in `base.html`) that lets
  plain HTML tags trigger server requests and swap in the returned HTML fragment,
  without writing a JavaScript frontend framework. Radar uses it everywhere:
  clicking a button, submitting a search, or paginating a table sends an HTMX
  request to a FastAPI route that returns an HTML partial (from `templates/partials/`)
  which replaces part of the page in place.
- **Jinja2** - the templating language FastAPI uses to render HTML from Python data.
  All of Radar's UI lives in `radar_web/templates/` as `.html` files with `{{ }}`
  expressions and `{% %}` control blocks.
- **SQLite** (via Python's built-in `sqlite3`) - a lightweight, file-based database.
  Radar uses it for exactly one thing: a local mirror of the Discogs monthly data
  dump (`shared/discogs_dump.sqlite3`), so it can search/rank millions of releases
  without hitting the Discogs API for every query.
- **YouTube IFrame Player API** - a JavaScript widget Google provides to embed a
  controllable YouTube player in a web page. `/reco-radar` uses it client-side to
  play the internal "RECOS RADAR" playlist (a list of video IDs handed to
  `cuePlaylist`) - there is no real YouTube playlist or Google account involved.
- **Caddy** - a reverse-proxy/web-server used only in deployment
  (`docker-compose.yml` + `Caddyfile`) to terminate HTTPS (Let's Encrypt) in front
  of the FastAPI app. This piece of the stack is not yet live (blocked on owning a
  domain name, see Key Design Decisions).
- **yt-dlp / Playwright / requests** - used by background jobs (`crate_jobs.py`),
  not by the web server itself, to scrape YouTube playlist contents, drive a
  headless browser for DJ-set ingestion, and call the Discogs/YouTube/Bandcamp
  HTTP APIs.

No frontend build system (no React/Vue/bundler) - the UI is server-rendered HTML
plus HTMX and a few hand-written vanilla JS files under `radar_web/static/`.

## 3. File structure and routes

```
radar_web/
  app.py            ->  the FastAPI app itself: ~80 routes, auth/session/cookie
                         logic, and all page handlers (2200+ lines, the biggest
                         file in the project)
  worker.py          ->  standalone process (docker service "radar-worker"):
                          drains the job queue one job at a time, round-robin
                          across users, and enqueues scheduled maintenance jobs
                          (weekly seller scan, monthly Discogs dump refresh,
                          taste-graph rebuild, RECOS RADAR feed - currently
                          paused, see Key Design Decisions)
  Dockerfile         ->  builds the image used by both radar-web and radar-worker
  requirements.txt   ->  fastapi, uvicorn, jinja2, requests, yt-dlp, playwright
  radar/             ->  the actual application logic, imported by app.py/worker.py
    paths.py          ->  where every data file lives (per-user vs shared),
                           multi-user path namespace, legacy-layout migration
    store.py           ->  atomic JSON load/save, per-request "current user"
                            context var, mtime-based read cache, config read/write
    accounts.py         ->  user accounts: scrypt password hashing, invite tokens,
                             owner bootstrap from APP_PASSWORD
    jobs.py              ->  the job queue itself (/data/jobs/queue.json) and
                              per-job status files consumed by the UI
    discogs.py            ->  thin wrapper around the live Discogs REST API
                               (search, release/label/artist lookups) - never
                               raises, returns {} on failure (see pitfall #10)
    discogs_dump.py        ->  builds/queries the local SQLite mirror of the
                                Discogs monthly catalog dump (vinyl only)
    scoring.py               ->  the "brain": Ctx class loads all of a user's
                                  data once per request and computes
                                  album_score/ascore/reco_rows from it
    artistgraph.py / labelgraph.py -> co-credit graph builders used to find
                                       artists/labels related by taste
    learn.py               ->  turns thumbs up/down feedback into adjusted
                                scoring weights
    ytcache.py              ->  cached, scored YouTube Data API search (see
                                 Section 5) with a personal/app API-key cascade
    bandcamp.py             ->  Bandcamp search helpers (no real API, see
                                 pitfall #9)
    volumo.py                ->  checks whether a release is actually for sale
                                  via Volumo's public JSON API (only verified
                                  working shop integration, see pitfall #24)
    sellers.py / sellers_seed.py -> seller/shop catalog scanning and seed list
    vocab.py / textmatch.py    ->  small text-normalization/matching helpers
  templates/
    base.html           ->  HTML shell: nav bar, HTMX script tag, combobox/chip
                             widgets shared by every page
    pages/               ->  one file per top-level route (patte.html = "Mes
                              sources", search.html, veille.html = "Nouveautés",
                              univers.html = "Mes labels & artistes",
                              reco_radar.html, settings.html, disco.html,
                              feedback.html)
    partials/            ->  ~25 HTML fragments returned by HTMX endpoints
                              (tables, suggestion dropdowns, the cart, the
                              feedback widget, the artist/label graphs, etc.)
  static/
    app.css              ->  all styling, including the shared .tbl table style
    feedback.js / wltoggle.js / wradar.js -> small vanilla-JS behaviors
crate_jobs.py        ->  every long-running background task, run as a
                          subprocess by radar_web/worker.py: ingest_youtube,
                          ingest_spotify, ingest_bandcamp, ingest_djsets,
                          fetch_collection, merge_corpus, profile_labels,
                          resolve_artists, build_graph, search_base,
                          scan_sellers, scan_catalog, enrich, canonicalize,
                          scan_veille, scan_recos, publish_recos,
                          import_discogs_dump (2600 lines; ~120KB, second
                          biggest file)
docker-compose.yml   ->  3 services: caddy (HTTPS proxy), radar-web (FastAPI,
                          port 8600), radar-worker (job queue drainer) - all
                          sharing the ./data volume
Caddyfile             ->  reverse-proxy config, domain from $RADAR_DOMAIN
scripts/
  backup.sh            ->  nightly encrypted backup of /data (excludes the
                            multi-GB Discogs dump SQLite file, see pitfall #12)
  cloud-setup.sh        ->  provisioning helper
data.sample/           ->  minimal config to run the app locally with nothing
                            ingested yet (no real user data - gitignored elsewhere)
.github/workflows/deploy.yml -> CI: on push to main, SSHes to the VPS, git pulls,
                                 rebuilds Docker, health-checks, rolls back on failure
archive/                ->  the old Streamlit app, removed 2026-09-01, dead code
                             kept for reference only - never edit
```

Data is never stored in git: everything real lives under `/data` on the VPS
(`CRATE_DATA_DIR` env var), organized as `users/<uid>/*.json` (per-user taste,
config, caches) and `shared/*.json` + `shared/discogs_dump.sqlite3` (neutral,
public Discogs/YouTube metadata reused across users).

## 4. How the app works - the main flow

```
Browser
  |
  v
FastAPI route in app.py  (session cookie -> uid, via _req_uid)
  |
  |-- reads/writes JSON files under /data/users/<uid>/ and /data/shared/
  |        (radar_web/radar/store.py + paths.py)
  |
  |-- for scoring/recommendation pages: builds a scoring.Ctx(uid), which loads
  |        the user's config + profile + collection + corpus + graph files
  |        once and computes scores (album_score / ascore / reco_rows)
  |
  |-- for anything slow (scraping YouTube, calling Discogs, rebuilding the
  |        artist graph): enqueues a job via radar.jobs.launch(name, params)
  |        into /data/jobs/queue.json, and returns immediately
  |
  v
Jinja2 template renders HTML (full page or an HTMX partial)
  |
  v
Browser shows the page / swaps the fragment via HTMX

                    (separate process, always running)
radar_web/worker.py  --polls--> /data/jobs/queue.json
  |
  |-- pops one job (round-robin across users, serial execution to protect
  |        the shared Discogs rate limit)
  |
  v
subprocess: python crate_jobs.py <job_name> <params_json>
  |
  |-- writes progress to /data/jobs/<uid>/<job>.status.json
  |-- writes its results back into the same per-user/shared JSON files
  |        that app.py reads on the next request
```

There is no separate database server and no API layer beyond FastAPI's own
routes - every "table" is a JSON file (or, for the Discogs catalog mirror, a
local SQLite file), and every slow operation is a Python subprocess coordinated
through a queue file rather than a task broker like Celery/Redis.

## 5. Data files

| Source | What it contains | When loaded |
|---|---|---|
| `crate_radar_config.json` (per-user) | Followed labels, taste categories, artist categories, "veille" rules, connection settings, scoring weights | Read on every request via `store.read_config` |
| `taste_corpus.json` (per-user) | Every track ingested from YouTube/Spotify/Bandcamp/DJ sets, resolved to Discogs artist/label where possible | Read into `scoring.Ctx` per request; written by the `ingest_*`/`merge_corpus` jobs |
| `labels_profile.json` (per-user) | A style-distribution profile per followed label, built from its Discogs releases | Read per request; rebuilt weekly by `profile_labels` job |
| `collection_cache.json` (per-user) | The owner's Discogs collection + wantlist, cached from the live API | Read per request; refreshed by `fetch_collection` job |
| `producer_graph.json` (per-user) | Multi-level co-credit graph (which artists/labels collaborate) seeded from taste | Read for recommendations; rebuilt monthly by `build_graph` (mode "taste") |
| `recos_playlist.json`, `recos_candidates.json`, `recos_seen.json`, `recos_playlist_history.json` (per-user) | The internal RECOS RADAR queue/playlist state (max 5 tracks, FIFO) | Read/written by `scan_recos`/`publish_recos` jobs and the `/reco-radar` page |
| `shared/discogs_dump.sqlite3` | Local mirror of the Discogs monthly catalog dump, vinyl-only subset (id, title, artist, label, catno, year, country, formats, genres, styles, master_id) for ~millions of releases | Built once by `import_discogs_dump`, refreshed monthly; queried directly by `discogs_dump.py` for local search/ranking without hitting the live API |
| `shared/lookup_cache.json`, `shared/release_meta_cache.json` | Cached Discogs release/artist/label lookups and release metadata (rating, price hints, num-for-sale) | Read/written on demand by `discogs.py`; shared across users because the content is public |
| `shared/youtube_cache.json` | Cached YouTube search results and video metadata, keyed by query | Read/written by `ytcache.py`, TTL 7 days for hits / 6 hours for misses |
| Discogs REST API (`api.discogs.com`) | Live search, release/label/artist details, collection/wantlist | Called on demand by `discogs.py`, `discogs_get()` never raises - returns `{}` on any failure |
| YouTube Data API v3 | Video search + metadata | Called on demand by `ytcache.py` with a personal-key-then-app-key fallback cascade |
| Volumo public JSON API | Whether a release is currently for sale in a real shop | Called on demand from search results (`volumo.py`) - the only shop integration that survived vetting (Beatport/Traxsource/Bleep/7digital/Juno were all ruled out, see pitfall #24) |
| `accounts.json`, `invites.json` (root of `/data`) | Login accounts (scrypt-hashed passwords) and pending invite tokens | Read/written by `accounts.py`; owner account bootstrapped from `APP_PASSWORD` on first boot |

## 6. State / data flow summary

Listening history and a Discogs collection flow in through background jobs into
per-user JSON files, `scoring.Ctx` recomputes taste scores from those files on
every request, and FastAPI renders the result as HTML (or an HTMX fragment) that
the browser shows - there is no client-side app state beyond a session cookie and
a couple of small localStorage-free JS widgets.

| What | Where it lives | How it changes |
|---|---|---|
| Logged-in user (uid) | Signed cookie (`radar_auth`), verified in `app.py` on every request | Login/logout, or `RADAR_NO_AUTH=1` dev mode which forces uid "owner" |
| Taste config (labels, categories, scoring weights) | `crate_radar_config.json` per user | Edited on `/settings`, `/univers` (add/remove label or artist) |
| Scored releases / recommendations | Computed in-memory each request by `scoring.Ctx`, memoized per process by file mtime signature | Recomputed whenever any input JSON file's mtime changes (a job finished, or config was saved) |
| Job queue / status | `/data/jobs/queue.json` + `/data/jobs/<uid>/<job>.status.json` | Enqueued by an HTMX POST (e.g. "Scanner maintenant"), drained by `worker.py`, progress polled by the UI via `/jobs/{name}/status` |
| RECOS RADAR playlist | `recos_playlist.json` (max 5 tracks, FIFO) | `scan_recos` finds candidates -> `publish_recos` searches YouTube and appends; manual delete button removes a track; "Forcer" wipes the candidate cache to rescore from scratch |
| Discogs catalog mirror | `shared/discogs_dump.sqlite3` | Rebuilt monthly by `import_discogs_dump`, resumable via a checkpoint state file |

## 7. Key design decisions

- **Everything is JSON files, on purpose.** There is no Postgres/MySQL - config,
  taste data, caches and job status are all plain JSON files under `/data`,
  written atomically (`store.save` via a temp file + rename). This keeps the app
  deployable as a single Docker Compose stack with a bind-mounted volume. A
  migration to a real SQL database ("Option B") is explicitly deferred until the
  user base grows past ~20 people.
- **Jobs run as subprocesses, not a task queue library.** `crate_jobs.py` is
  invoked as `python crate_jobs.py <job> <params_json>` by `worker.py`, which
  polls a JSON queue file every 2 seconds. This is intentional for simplicity but
  means only one job runs at a time system-wide (also protects the single shared
  Discogs API rate limit).
- **`discogs_get()` never raises** - it returns `{}` on any failure. Code calling
  into `discogs.py` must handle an empty dict as "lookup failed," not treat
  exceptions as the error signal.
- **RECOS RADAR is not a real YouTube playlist.** It is a Radar-internal JSON
  list of video IDs played client-side via the YouTube IFrame Player API
  (`cuePlaylist`). No Google OAuth app was ever needed for this feature.
- **The automatic RECOS RADAR feed job is currently disabled in code**
  (`worker._maybe_recos_scan` returns immediately, before checking its own env
  var) - a deliberate pause during testing with the playlist capped at 5 tracks.
  Feeding the playlist currently requires the manual buttons on `/reco-radar`.
  Removing that early `return` re-enables the hourly automatic scan.
- **The old VPS "diag loop" (`/diag`, `/dev-loop` skills) is paused, not
  deleted.** Its skill contracts were moved out of `.claude/skills/` (so they
  can't be invoked) into `docs/archive/`. Do not re-enable, open `Diag <sha>`
  issues, or fire that trigger without an explicit user request.
- **Multi-user support is partially built.** Per-user data namespacing, accounts,
  a job queue, and shared caches are done and deployed; HTTPS+domain and
  Discogs/Spotify OAuth are blocked (no domain name yet, and OAuth needs an
  HTTPS callback URL). Until then this is effectively a single-user ("owner")
  app running without a real domain.
- **Discogs Marketplace data (live prices/listings) is unreachable** - Cloudflare
  blocks it - so Radar only ever shows a link out to Discogs plus an "API
  available" badge, never scraped price data.
- **This cloud session has no real credentials or network access to Discogs/
  YouTube/Spotify/the VPS.** Changes here must be validated with `py_compile` and
  offline smoke tests, then verified for real on the VPS after deploy - several
  "TODO next session" items exist specifically to re-verify fixes made blind.

## 8. Key files table

| File | Purpose |
|---|---|
| `radar_web/app.py` | FastAPI app: all ~80 HTTP routes, auth/session cookies, page rendering |
| `radar_web/worker.py` | Background process that drains the job queue and schedules maintenance jobs |
| `crate_jobs.py` | All long-running jobs (ingestion, scoring maintenance, RECOS RADAR, Discogs dump import), run as subprocesses |
| `radar_web/radar/scoring.py` | The recommendation "brain" - `Ctx`, `album_score`, `ascore`, `reco_rows` |
| `radar_web/radar/paths.py` | Defines per-user vs shared data file locations and the multi-user layout |
| `radar_web/radar/store.py` | Atomic JSON read/write, per-request current-user context, config helpers |
| `radar_web/radar/discogs.py` | Live Discogs API wrapper (never raises, returns `{}` on failure) |
| `radar_web/radar/discogs_dump.py` | Builds/queries the local SQLite mirror of the Discogs catalog dump |
| `radar_web/radar/ytcache.py` | Cached, scored YouTube search used by RECOS RADAR and search results |
| `radar_web/radar/accounts.py` | Login accounts, password hashing, invites |
| `radar_web/radar/jobs.py` | The job queue (enqueue/status) that connects app.py and worker.py |
| `radar_web/templates/base.html` | Shared HTML shell, nav, and HTMX/JS glue used by every page |
| `docker-compose.yml` | The 3-service deployment: Caddy, radar-web, radar-worker |
| `.github/workflows/deploy.yml` | CI/CD: deploys to the VPS on push to `main`, with rollback |
| `CLAUDE.md` | Living project state, TODOs, and a long list of documented pitfalls - the primary source of truth for this repo |

**Where to look for any feature:**
- Adding/removing a followed label or artist, or the taste graph -> `/univers`
  routes in `app.py` + `radar/labelgraph.py` / `radar/artistgraph.py`
- Scoring / why a release ranks the way it does -> `radar/scoring.py`
- RECOS RADAR playlist behavior -> `crate_jobs.job_scan_recos` /
  `job_publish_recos`, `radar_web/worker.py._maybe_recos_scan`, `reco_radar.html`
- YouTube search/matching quality -> `radar/ytcache.py`
- Discogs catalog search without hitting the live API -> `radar/discogs_dump.py`
- Background job wiring (new job type) -> add to `crate_jobs.JOBS` dict and a
  route in `app.py` under `/patte/run/{job}` or `/jobs/{name}/launch`
- Login/accounts/invites -> `radar/accounts.py` + `/login`, `/register`,
  `/account/invite` routes in `app.py`
- Deployment/VPS -> `docker-compose.yml`, `Caddyfile`, `.github/workflows/deploy.yml`,
  `scripts/backup.sh`
