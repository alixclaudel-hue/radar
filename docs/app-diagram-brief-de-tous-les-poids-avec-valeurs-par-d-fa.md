# Diagram Brief: Radar — Poids par défaut du système de scoring

**Layout**: top-to-bottom
**Flow summary**: Shows how a weight starts as a config value (default or user-edited), is loaded once per request into `Ctx`, flows through the weight groups that compute label/artist score and feed into album/track score, and ends up rendered on score surfaces or cached for RECOS/precompute.

---

## Elements

Create a group labeled "Layer 1 — Config Inputs" that contains: Config File, Settings Form, Feedback Auto-Tuner.

Create a group labeled "Layer 2 — Entry Point" that contains: Ctx Init.

Create a group labeled "Layer 3 — Weight Groups (Logic)" that contains: Shared Upstream Weights, Reco Weights, Artist Score Weights, Album Weights.

Create a group labeled "Layer 4 — Scores & Surfaces (Outputs)" that contains: Label Score, Artist Score, Album / Track Score, Score Surfaces, Precompute Cache.

Create a box labeled "Config File". Note: `crate_radar_config.json`, per user (`store.py`). Holds `cfg["scoring"]`, deep-merged over the hardcoded `DEFAULT_SCORING` dict — a config missing a key silently gets the default.

Create a box labeled "Settings Form". Note: GET/POST `/settings` (`app.py`, `settings.html`). Manual sliders/fields named `<group>__<key>`; the only UI for most weights.

Create a box labeled "Feedback Auto-Tuner". Note: `learn.py`, POST `/settings/learn/apply`. Proposes new values from 👍/👎 feedback, but ONLY for the `reco` and `album` groups — `artist_score` is never auto-tuned.

Create a box labeled "Ctx Init". Note: `scoring.py`, class `Ctx.__init__`, built fresh per request. Reads `self.scoring` from the Config File; downstream results memoised per config-file signature (mtime+size).

Create a box labeled "Shared Upstream Weights". Note: `graph.*` (tier_w 1=3.5/2=1.5/none=0.3, artist_breadth=0.4, label_breadth=0.5, cat1_bonus=2.0, role_main=1.0/role_remix=0.7/role_other=0.4/role_written=0.05, max_credits=6, max_levels=4, level_decay=0.5, node_cap=150), `sources.*` (discogs_collection=1.0, discogs_want=0.6, youtube=0.5, spotify=0.5, bandcamp=0.9, djset=0.4), `taste_tiers.1`=1.0/`.2`=0.6, `artist_tiers.1`=1.0/`.2`=0.5. No Settings UI — hand-edit config only.

Create a box labeled "Reco Weights". Note: label score group (`Ctx.reco_rows`/`reco_index`). collection=0.6, corpus=0.5, artist=0.4, affinity=0.4, db_link=0.35, want_factor=0.6, label_affinity_floor=0 (hard cutoff). Normalized by weights actually present, not a fixed sum.

Create a box labeled "Artist Score Weights". Note: `artist_score.*` group (`Ctx.ascore`). manual=0.5, corpus=0.18, collection=0.1, graph=0.14, djset=0.08, label_link=0.15 — fixed-sum normalization (all six always evaluated).

Create a box labeled "Album Weights". Note: `album.*` group (`Ctx.album_score`, same function serves album AND track). label=0.4, artist=0.4, style=0.2, artist_max_vs_mean=0.6. Hardcoded (not configurable) shrinkage: K=0.35, PRIOR=45, pulling thin-evidence scores toward a neutral 45/100.

Create a box labeled "Label Score". Note: `reco_rows`/`reco_index` output, 0-100 per label.

Create a box labeled "Artist Score". Note: `ascore` output, 0-100 per artist.

Create a box labeled "Album / Track Score". Note: `album_score(r)` output, 0-100 — same formula for a release or a single track, no separate track formula exists.

Create a box labeled "Score Surfaces". Note: search results, "Nouveautés" inbox, label/artist pages — where scores render (`app.py`: `_scored_rows`, `_inbox`).

Create a box labeled "Precompute Cache". Note: `scorestore.sqlite3` (`scorestore.py`), `release_scores`/`track_scores` tables — background jobs `scorestore_releases`/`scorestore_tracks` refresh this against current weights.

Create a box labeled "External: Dump, RECOS, Multi-user". Note: Discogs dump ingestion, RECOS playlist queueing/YouTube search (`scoring.recos.*` thresholds only consume the score, never feed the formula), multi-user auth — out of scope for this diagram.

Draw an arrow from Settings Form to Config File. Label it "writes weight overrides".

Draw an arrow from Feedback Auto-Tuner to Config File. Label it "writes reco/album only".

Draw an arrow from Config File to Ctx Init. Label it "cfg['scoring'] merged over DEFAULT_SCORING".

Draw an arrow from Ctx Init to Shared Upstream Weights. Label it "self.scoring".

Draw an arrow from Ctx Init to Reco Weights. Label it "self.scoring".

Draw an arrow from Ctx Init to Artist Score Weights. Label it "self.scoring".

Draw an arrow from Ctx Init to Album Weights. Label it "self.scoring".

Draw an arrow from Shared Upstream Weights to Artist Score Weights. Label it "graph_rescore feeds graph term".

Draw an arrow from Shared Upstream Weights to Reco Weights. Label it "label_db_signal + wmap feed db_link/affinity".

Draw an arrow from Shared Upstream Weights to Album Weights. Label it "wmap feeds style term".

Draw an arrow from Artist Score Weights to Artist Score. Label it "blend of 6 features".

Draw an arrow from Artist Score Weights to Reco Weights. Label it "ascore feeds label_artist_signal (artist term)".

Draw an arrow from Reco Weights to Label Score. Label it "blend of 5 features".

Draw an arrow from Label Score to Album / Track Score. Label it "reco_index feeds label term".

Draw an arrow from Artist Score to Album / Track Score. Label it "ascore feeds artist term".

Draw an arrow from Album Weights to Album / Track Score. Label it "blend of 3 terms + shrinkage".

Draw an arrow from Label Score to Score Surfaces.

Draw an arrow from Artist Score to Score Surfaces.

Draw an arrow from Album / Track Score to Score Surfaces.

Draw an arrow from Album / Track Score to Precompute Cache. Label it "scorestore_releases/tracks jobs".

Draw an arrow from Precompute Cache to External: Dump, RECOS, Multi-user. Label it "scoring.recos.* reads as threshold only".

Mark Config File as entry point.

Mark Precompute Cache as database.

Mark External: Dump, RECOS, Multi-user as external.

---

## Notes
- Scope boundary: this diagram covers only the weight groups feeding label/album/track/artist score and their default values. Discogs dump ingestion, RECOS playlist mechanics beyond reading a threshold, multi-user auth, and UI templates beyond `/settings` are collapsed into the single "External" box.
- Grouped by weight-group, not by individual numeric constant, to keep the box count readable (per section 7 of the source doc) — every default value is listed in the relevant box's note instead of becoming its own box.
- Two vestigial values (`taste_tiers.3`, `graph.tier_w.3`) and the dead `scoring.learn.*` config group (declared, never read by `learn.py`) are intentionally omitted from the drawing — they carry defaults but do not currently affect any live score. Also omitted: `crate_jobs.py`'s unreferenced duplicate `SOURCE_WEIGHTS` dict (dead code).
- "Album / Track Score" is one box because it is one function (`album_score()`) — there is no separate track-score formula or weight set.
- `scoring.reco` (label weights, drawn here) vs `scoring.recos` (RECOS playlist thresholds, inside the External box) are different config keys that are easy to conflate — kept visually separate on purpose.
