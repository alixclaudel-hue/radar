# Diagram Brief: Discogs Dump Index + Scoring Process

**Layout**: top-to-bottom
**Flow summary**: The monthly Discogs dump is downloaded and built into a local SQLite index; the scoring engine (`Ctx`) reads that index plus per-user taste data to compute artist/label/album scores consumed by the web app and background jobs.

---

## Elements

Create a group labeled "Discogs Dump Build (discogs_dump.py)" that contains: Download & Verify, Streaming XML Import, Atomic Finalize.

Create a group labeled "Scoring Engine — Ctx (scoring.py)" that contains: Taste Weights & Tiers, Graph Rescore, Artist Score, Label Reco Score, Album Score.

Create a box labeled "Discogs.com Monthly Dump". Note: external input — releases.xml.gz, labels.xml.gz, artists.xml.gz, published once a month.

Create a box labeled "Download & Verify". Note: download_dump()/verify_checksum() — resumable byte-range download, SHA-256 check.

Create a box labeled "Streaming XML Import". Note: import_releases/import_labels/import_artists() — iterparse, batched inserts into DB_PATH + ".new".

Create a box labeled "Atomic Finalize". Note: finalize_new_db() — builds indexes, materializes label_styles, os.replace() onto the live file.

Create a box labeled "discogs_dump.sqlite3". Note: the one live index file — releases, release_styles, release_artists, labels, artists, artist_aliases, label_styles.

Create a box labeled "Read-Only Lookups". Note: search_local, resolve_name, label_style_counts, artist_ids_for_labels, label_ids_for_artists — degrade to empty result, never raise.

Create a box labeled "Taste Weights & Tiers". Note: wmap (style weights), label_tier_map/artist_tier_map (Coeur/Aime lists).

Create a box labeled "Graph Rescore". Note: graph_rescore() — co-credit proximity from producer_graph.json.

Create a box labeled "Artist Score". Note: ascore — blends tier, corpus/collection counts, graph proximity, artist_label_signal (catalog cross-link, style-checked).

Create a box labeled "Label Reco Score". Note: reco_rows()/reco_index — blends collection/want counts, corpus label scores, label_db_signal, label_affinities.

Create a box labeled "Album Score". Note: album_score(release) — not memoized itself, combines Artist Score + Label Reco Score + Taste Weights per release; shrinks toward a neutral prior (45) when evidence is thin.

Create a box labeled "Per-User Config & Taste Data". Note: external — store.py's config (label_categories/artist_categories/scoring weights) plus taste_corpus.json, collection_cache.json, producer_graph.json, artists_resolved.json.

Create a box labeled "crate_jobs.py (background jobs)". Note: external caller — job_import_discogs_dump (monthly build), job_scan_recos (scores candidates via Album Score), job_build_graph (reads release_artists via SQL to build producer_graph.json).

Create a box labeled "app.py (web routes)". Note: external consumer — builds a fresh Ctx per request for search, Reco Radar, Mes labels & artistes.

Create a box labeled "catalog_labelgraph.py". Note: external, separate module — builds a catalog-wide label graph from the same SQLite file; not yet consumed by Ctx.

Draw an arrow from Discogs.com Monthly Dump to Download & Verify. Label it "raw dump files".

Draw an arrow from Download & Verify to Streaming XML Import. Label it "verified .xml.gz".

Draw an arrow from Streaming XML Import to Atomic Finalize. Label it "DB_PATH+.new populated".

Draw an arrow from Atomic Finalize to discogs_dump.sqlite3. Label it "os.replace()".

Draw an arrow from discogs_dump.sqlite3 to Read-Only Lookups. Label it "SQL queries".

Draw an arrow from discogs_dump.sqlite3 to catalog_labelgraph.py. Label it "same file, separate reader".

Draw an arrow from Read-Only Lookups to Artist Score. Label it "catalog cross-links".

Draw an arrow from Per-User Config & Taste Data to Taste Weights & Tiers. Label it "config JSON".

Draw an arrow from Per-User Config & Taste Data to Graph Rescore. Label it "producer_graph.json".

Draw an arrow from Per-User Config & Taste Data to Artist Score. Label it "corpus/collection counts".

Draw an arrow from Per-User Config & Taste Data to Label Reco Score. Label it "corpus/collection counts".

Draw an arrow from Taste Weights & Tiers to Artist Score. Label it "style weights, tier".

Draw an arrow from Taste Weights & Tiers to Label Reco Score. Label it "style weights, tier".

Draw an arrow from Graph Rescore to Artist Score. Label it "co-credit proximity".

Draw an arrow from Artist Score to Album Score. Label it "ascore".

Draw an arrow from Label Reco Score to Album Score. Label it "reco_index".

Draw an arrow from Taste Weights & Tiers to Album Score. Label it "wmap".

Draw an arrow from Album Score to app.py (web routes). Label it "score shown per page".

Draw an arrow from Album Score to crate_jobs.py (background jobs). Label it "job_scan_recos candidate scoring".

Draw an arrow from crate_jobs.py (background jobs) to Atomic Finalize. Label it "job_import_discogs_dump triggers monthly build".

Draw an arrow from discogs_dump.sqlite3 to crate_jobs.py (background jobs). Label it "job_build_graph reads release_artists directly".

Mark Discogs.com Monthly Dump as external.

Mark discogs_dump.sqlite3 as database.

Mark Per-User Config & Taste Data as external.

Mark crate_jobs.py (background jobs) as external.

Mark app.py (web routes) as external.

Mark catalog_labelgraph.py as external.

---

## Notes
- Boundary: only `discogs_dump.py` (top group) and `scoring.py`'s `Ctx` (bottom group) are drawn in detail. `store.py`, `catalog_labelgraph.py`, `crate_jobs.py`'s jobs, and `app.py`'s routes are each a single named box — real dependencies, not opened up further.
- "Per-User Config & Taste Data" merges several files owned by `store.py` and various jobs (config, corpus, collection, producer graph, resolved artists) into one box for scale — the source doc's section 5 lists them individually if finer detail is ever needed.
- The two arrows into "Atomic Finalize" and "release_artists" from `crate_jobs.py` are the two directions background jobs touch this slice: triggering the monthly rebuild, and reading the index directly by SQL (bypassing `Ctx`) to build the co-credit graph.
- `Ctx` itself has no arrow drawn "into" it as a whole — its five internal boxes (Taste Weights & Tiers, Graph Rescore, Artist Score, Label Reco Score, Album Score) are the actual computation chain; grouping them under "Scoring Engine — Ctx" is what represents the class boundary.
