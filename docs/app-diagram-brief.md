# Diagram Brief: Radar Architecture

**Layout**: top-to-bottom
**Flow summary**: Shows how a browser request or a scheduled job enters Radar through FastAPI, gets processed by the scoring brain or the background job pipeline, and how results land back in JSON/SQLite files and are rendered back to the browser.

---

## Elements

Create a group labeled "Inputs" that contains: Browser, Discogs API, YouTube Data API, Volumo API.

Create a group labeled "Entry Point (FastAPI)" that contains: FastAPI Routes.

Create a group labeled "Logic" that contains: Scoring Brain, Job Queue, Worker Process, Job Subprocess.

Create a group labeled "Outputs" that contains: Per-User JSON Files, Discogs SQLite Mirror, Job Status Files, Rendered HTML.

Create a box labeled "Browser". Note: HTMX-driven UI, no client-side app framework.

Create a box labeled "Discogs API". Note: live REST API, discogs.py, never raises - returns {} on failure.

Create a box labeled "YouTube Data API". Note: video search, ytcache.py, key cascade + cache.

Create a box labeled "Volumo API". Note: public JSON API, volumo.py, confirms a release is for sale.

Create a box labeled "FastAPI Routes". Note: app.py, ~80 routes, session cookie auth, renders full pages or HTMX partials.

Create a box labeled "Scoring Brain". Note: scoring.py, Ctx class, computes album_score/ascore/reco_rows per request.

Create a box labeled "Job Queue". Note: radar/jobs.py, /data/jobs/queue.json.

Create a box labeled "Worker Process". Note: worker.py, polls queue every 2s, round-robin across users, schedules maintenance jobs.

Create a box labeled "Job Subprocess". Note: crate_jobs.py, one job at a time (ingest, scan, publish_recos, import_discogs_dump, etc.).

Create a box labeled "Per-User JSON Files". Note: paths.py/store.py, config/corpus/profile/graph/recos playlist, atomic writes.

Create a box labeled "Discogs SQLite Mirror". Note: shared/discogs_dump.sqlite3, discogs_dump.py, monthly catalog mirror.

Create a box labeled "Job Status Files". Note: /data/jobs/<uid>/<job>.status.json, polled by the UI.

Create a box labeled "Rendered HTML". Note: Jinja2 templates, full page or HTMX partial fragment.

Draw an arrow from Browser to FastAPI Routes. Label it "HTTP request".

Draw an arrow from FastAPI Routes to Scoring Brain. Label it "build Ctx(uid)".

Draw an arrow from FastAPI Routes to Job Queue. Label it "enqueue job".

Draw an arrow from Job Queue to Worker Process. Label it "polled".

Draw an arrow from Worker Process to Job Subprocess. Label it "spawn subprocess".

Draw an arrow from Job Subprocess to Discogs API. Label it "lookup".

Draw an arrow from Job Subprocess to YouTube Data API. Label it "search video".

Draw an arrow from Job Subprocess to Volumo API. Label it "check for sale".

Draw an arrow from Scoring Brain to Per-User JSON Files. Label it "read config/corpus/profile/graph".

Draw an arrow from Job Subprocess to Per-User JSON Files. Label it "write results".

Draw an arrow from Job Subprocess to Discogs SQLite Mirror. Label it "query / rebuild".

Draw an arrow from Job Subprocess to Job Status Files. Label it "write progress".

Draw an arrow from FastAPI Routes to Rendered HTML. Label it "render template".

Draw an arrow from Scoring Brain to Rendered HTML. Label it "scored rows".

Draw an arrow from Rendered HTML to Browser. Label it "HTML / HTMX fragment".

Mark Browser as entry point.

Mark Discogs API as external.

Mark YouTube Data API as external.

Mark Volumo API as external.

Mark Discogs SQLite Mirror as database.

Mark Per-User JSON Files as database.

Mark Worker Process as async.

---

## Notes
- Radar has no separate database server: "Per-User JSON Files" and "Discogs SQLite Mirror" are the only two persistence boxes, both plain files under /data.
- Job Subprocess covers all job types from crate_jobs.py (ingest_youtube/spotify/bandcamp, scan_recos, publish_recos, profile_labels, build_graph, import_discogs_dump, etc.) as one box rather than one per job, per the medium-project grouping rule (13 key files listed in the source doc).
- RECOS RADAR playback (client-side YouTube IFrame Player) is a special case of "Rendered HTML" -> "Browser" and not drawn as a separate arrow to avoid a fifth near-duplicate output path.
