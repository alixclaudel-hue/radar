# Diagram Brief: Radar - App Architecture

**Layout**: top-to-bottom
**Flow summary**: Shows how a browser request or an external API enters Radar, how FastAPI and the scoring brain handle it, how slow work is queued off to background jobs, and where the results end up (per-user data, the shared Discogs mirror, and the rendered page).

---

## Elements

Create a group labeled "Inputs" that contains: Browser, External APIs, Scheduled Trigger.

Create a group labeled "Entry Point" that contains: FastAPI Routes.

Create a group labeled "Logic" that contains: Scoring Brain, Job Queue, Background Jobs.

Create a group labeled "Outputs" that contains: Per-User Data, Shared Data Store, Rendered Page.

Create a box labeled "Browser". Note: HTMX-enhanced HTML, no SPA framework - where every request originates and every page is displayed.

Create a box labeled "External APIs". Note: Discogs REST API, YouTube Data API, Spotify Web API, Bandcamp autocomplete, Volumo JSON API.

Create a box labeled "Scheduled Trigger". Note: worker.py idle timer - weekly seller scan, monthly Discogs dump sync, weekly/monthly scoring maintenance (RECOS auto-scan currently paused).

Create a box labeled "FastAPI Routes". Note: radar_web/app.py - about 80 routes plus session-cookie auth middleware, the single entry point for every request.

Create a box labeled "Scoring Brain". Note: radar_web/radar/scoring.py - Ctx class computes album_score, ascore, reco_rows from a user's taste data.

Create a box labeled "Job Queue". Note: radar_web/radar/jobs.py - queue file plus per-user status files, drained round-robin by the worker.

Create a box labeled "Background Jobs". Note: crate_jobs.py - ingestion, scans, graph/profile rebuilds, Discogs dump import; runs as a subprocess.

Create a box labeled "Per-User Data". Note: JSON files under /data/users/<uid>/ - taste config, listening corpus, label/producer graph, RECOS playlist.

Create a box labeled "Shared Data Store". Note: shared/discogs_dump.sqlite3 (about 3 GB catalog mirror) plus YouTube/Bandcamp/Volumo caches and the seller catalog.

Create a box labeled "Rendered Page". Note: Jinja2 templates in radar_web/templates/ - full pages or HTMX-swappable fragments.

Draw an arrow from Browser to FastAPI Routes. Label it "HTTP request (HTMX)".

Draw an arrow from FastAPI Routes to Scoring Brain. Label it "build Ctx per request".

Draw an arrow from FastAPI Routes to Job Queue. Label it "enqueue slow job".

Draw an arrow from Scheduled Trigger to Job Queue. Label it "enqueue maintenance job".

Draw an arrow from Job Queue to Background Jobs. Label it "worker.py drains, one at a time".

Draw an arrow from Background Jobs to External APIs. Label it "search / ingest calls".

Draw an arrow from Background Jobs to Per-User Data. Label it "writes taste, corpus, graph, RECOS files".

Draw an arrow from Background Jobs to Shared Data Store. Label it "writes SQLite dump, caches".

Draw an arrow from Scoring Brain to Per-User Data. Label it "reads taste config, corpus, profile".

Draw an arrow from Scoring Brain to Shared Data Store. Label it "queries catalog mirror".

Draw an arrow from FastAPI Routes to Rendered Page. Label it "Jinja2 render".

Draw an arrow from Rendered Page to Browser. Label it "HTML / fragment swap".

Mark FastAPI Routes as entry point.

Mark External APIs as external.

---

## Notes
- External APIs are called by Background Jobs (ingestion, search) rather than by FastAPI Routes directly - only one arrow is drawn (the call); the response data flows back through the same job into Per-User Data / Shared Data Store, not shown as a separate arrow to avoid a redundant reverse edge.
- The Discogs marketplace (prices, live listings) is deliberately never fetched (Cloudflare blocks it) - not shown, since it plays no role in the architecture.
- RECOS RADAR's automatic scan is currently paused by design (a `return` at the top of `worker.py`'s `_maybe_recos_scan`) - the Scheduled Trigger box still fires other maintenance jobs, just not that one right now.
- Per-user vs shared is a strict split (`docs/architecture.md`) reflected here as two separate Outputs boxes rather than one combined "storage" box.
