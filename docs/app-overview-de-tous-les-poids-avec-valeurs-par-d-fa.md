# Radar - Scoring Weights Overview

**Scope of this document.** This is a focused slice of the Radar app, not a full app overview.
It covers only: every weight used to compute the **label score**, **album score**,
**artist score**, and **track score**, its exact default numeric value, which score it feeds,
and whether it can be changed from the `/settings` page. Everything else - Discogs dump
ingestion (`discogs_dump.py`), the RECOS playlist mechanics (queueing, YouTube search,
publish/scan jobs), UI templates beyond the settings page, the co-credit graph *builder*
job itself, multi-user auth - is treated as external context and mentioned only where it
explains where a weight is consumed. The focus turned out to match the request well: all
scoring weights live in one dict (`DEFAULT_SCORING` in `store.py`) and are all read by one
class (`Ctx` in `scoring.py`), so this did not need to be scaled up or down from what was asked.

## 1. What this piece of the app does

Radar is a personal Discogs crate-digging assistant: it watches labels and artists the user
likes, scores every release/track it sees against that taste profile, and surfaces the best
matches. The part documented here is the "grading rubric" behind every score shown in the
app - the numeric knobs that decide how much a label's reputation, an artist's track record,
a musical style match, or a listening-history signal counts toward the final 0-100 score a
user sees next to a record or a track. All of these knobs have a built-in default value, and
most of them can be adjusted by hand on the Settings page, or nudged automatically from
thumbs-up/thumbs-down feedback.

## 2. Framework / tech stack (relevant slice only)

- **Plain Python, no ML library.** Every score is arithmetic (weighted sums, `log1p` ratios,
  a hand-rolled logistic regression for auto-tuning). No numpy/sklearn/pandas involved.
- **FastAPI + Jinja2 + HTMX** (`radar_web/app.py`, `radar_web/templates/`): serves the
  `/settings` page where these weights live in a form, and every page that displays a score
  (search results, "Nouveautés", label pages) by calling into the scoring engine described
  here. Only relevant to this document as the delivery mechanism for reading/writing weights.
- **SQLite (WAL mode)** via `scorestore.py`: stores *precomputed* results of the album/track
  scoring formula (not the weights themselves - those stay in JSON config). Explained in
  section 5.
- **JSON files** (`store.py` on top of stdlib `json`): the weights themselves live in one
  JSON config file per user, read via atomic-write/mtime-cached helpers.

## 3. File structure (files relevant to weights/scoring)

```
radar_web/radar/
  scoring.py       ->  the "brain": class Ctx, reads self.scoring (= cfg["scoring"]) and
                        turns it into label score (reco_rows), artist score (ascore),
                        album/track score (album_score) - every weight is consumed here
                        or one level upstream in crate_jobs.py (graph construction)
  store.py         ->  DEFAULT_SCORING dict (every weight's default value), load_config()
                        (defaults + legacy-config migration), save_config()
  learn.py         ->  reads 👍/👎 feedback, proposes new values for the "reco" (label) and
                        "album" weight groups via a small logistic regression; applied
                        through /settings/learn/apply
  scorestore.py     ->  SQLite storage for *precomputed* album_score()/track results
                        (release_scores, track_scores tables) - not weights, just cached
                        outputs of the formula described here
  paths.py          ->  per-user config file location (crate_radar_config.json)

crate_jobs.py
  job_build_graph()            ->  reads scoring.graph.* to build the co-credit graph
                                    (max_credits/max_levels/level_decay/node_cap/role_*)
  job_scorestore_releases()    ->  calls Ctx.album_score() for every release of every
                                    followed label, using scoring.scorestore.min_score
  job_scorestore_tracks()      ->  same, per track; scoring.scorestore.fetches_per_run
  job_scan_recos()/job_publish_recos()  ->  read scoring.recos.* thresholds (RECOS
                                    playlist feature - external to this doc otherwise)

radar_web/app.py
  settings_page() / settings_save()   ->  GET/POST /settings - renders and writes every
                                           user-configurable weight below
  settings_learn() / settings_learn_apply()  ->  GET /settings/learn, POST
                                           /settings/learn/apply - feedback-driven auto-tune
  _scored_rows(), _inbox()             ->  callers of Ctx.album_score() for search results
                                           and the "Nouveautés" inbox (release- and
                                           track-level rows alike)

radar_web/templates/pages/settings.html   ->  the form: one block per weight group
  (reco, album, artist_score, taste_tiers/artist_tiers, recos), sliders/number inputs
  named "<group>__<key>", e.g. name="album__label"
```

## 4. How it works - the main flow

```
crate_radar_config.json (per user)
   |  cfg["scoring"]  (deep-merged over DEFAULT_SCORING at load time,
   |                   so a config missing a key silently gets the default)
   v
Ctx(uid)  (radar_web/radar/scoring.py, built fresh per request, memoised per file signature)
   |
   |-- wmap                     <- taste_tiers        (style weight by category)
   |-- artist_tier_map/label_tier_map                 (Coeur/Aime membership, no weight itself)
   |-- seed_category_weight     <- artist_tiers, graph.tier_w, sources
   |-- graph_rescore            <- graph.tier_w/artist_breadth/label_breadth/cat1_bonus
   |-- ascore (ARTIST SCORE)    <- artist_score.*, artist_tiers, (graph_rescore, artist_label_signal)
   |-- label_artist_signal, label_db_signal           (built from ascore / graph_rescore)
   |-- reco_rows / reco_index (LABEL SCORE)  <- reco.*, label_affinity_floor, (wmap for "affinity")
   `-- album_score(r) (ALBUM SCORE, and TRACK SCORE when r is one track)
                                 <- album.*, (reco_index for "label" term, ascore for
                                    "artist" term, wmap for "style" term)
   |
   v
Score (0-100) shown on: search results, "Nouveautés" inbox, label/artist pages, RECOS candidates
```

Four things to note about this flow:

- **There is no separate "track score" formula.** `Ctx.album_score(r)` is called with either
  a release dict (label/style/title from Discogs) or a single-track dict (`{"title": "Artist
  - Title", "label": [...], "style": [...]}` built from one track). Both go through the exact
  same weights (the `album` group). Callers building track-level input: `djset_rows()`
  (each corpus line), `crate_jobs.py`'s `job_scorestore_tracks`, and `app.py`'s `_inbox()`.
- **Label score, artist score and album/track score are layered**, not independent: album
  score's "label" term comes from the label score (`reco_index`), and its "artist" term comes
  from the artist score (`ascore`). Change an upstream weight (e.g. `artist_score.manual`) and
  every album/track score involving that artist shifts too.
- **Two config groups look alike but are not weights of these four scores**: `scoring.recos`
  (RECOS playlist queue: `min_score` threshold, `max_new_releases`, `searches_per_run`,
  `max_tracks` capacity) and `scoring.scorestore` (precompute job: `min_score`,
  `fetches_per_run`) only *consume* the already-computed album/track score as a filter
  threshold - they do not feed the formula. Treated as external/boundary here.
- **`Ctx._memo`** enforces a declared dependency graph (`NODE_DEPS`) between these nodes at
  runtime - accessing a node not declared as a dependency raises `RuntimeError` rather than
  silently working, which is why the arrows above are safe to trust as exhaustive.

## 5. Data files (relevant to weights)

| File | Contains | Loaded |
|---|---|---|
| `crate_radar_config.json` (per user, `paths.user_paths(uid).config`) | `cfg["scoring"]` = every weight group below, plus `label_categories`/`artist_categories`/`taste_categories` (which labels/artists/styles sit in which tier - not weights themselves, but what the weights get applied *to*) | Read on every request via `store.read_config()` (mtime-cached); written by `POST /settings` and `POST /settings/learn/apply` |
| `reco_feedback.json` (per user) | Every 👍/👎 a user gives on a label or album/track recommendation, with the feature values shown at the time | Read by `learn.summary()` when `/settings/learn` is opened, to propose new `reco`/`album` weight values |
| `scorestore.sqlite3` (per user) | `release_scores`/`track_scores` tables: **precomputed outputs** of `album_score()` for followed labels, refreshed by two background jobs | Not a weight source - listed because it is the main consumer of `scoring.album`/`scoring.scorestore` at scale |

## 6. State / data flow summary

One sentence: a weight starts as a hand-picked or feedback-tuned number in
`crate_radar_config.json`, is merged over the hardcoded defaults in `DEFAULT_SCORING`, and is
combined with taste/label/artist/style data inside `Ctx` into the 0-100 score rendered on
every page and cached by `scorestore.py`.

| What | Where it lives | How it changes |
|---|---|---|
| All weight defaults | `DEFAULT_SCORING` dict, `radar_web/radar/store.py` | Only by editing the code |
| Effective weights for one user | `cfg["scoring"]`, `crate_radar_config.json`, deep-merged over `DEFAULT_SCORING` | `POST /settings` (manual sliders/fields), or `POST /settings/learn/apply` (feedback-driven for `reco`/`album` groups only) |
| Ctx's per-request view of weights | `self.scoring` in `Ctx.__init__` | Re-read from disk every request; downstream derived values (`ascore`, `reco_rows`, ...) memoised per config-file signature (mtime+size) |
| Precomputed album/track scores | `scorestore.sqlite3` | Background jobs `scorestore_releases`/`scorestore_tracks`, re-run against current weights; not itself a weight |

## 7. Every weight, grouped by which score it feeds

Config path shorthand: all groups below live under `cfg["scoring"][...]`.

### 7.1 Label score (`Ctx.reco_rows()` / `reco_index`) - config group `reco`

The label score (0-100, shown per label, and consumed by album/track score as the "label"
term) blends five features, each `feature_value_0_to_1 * weight`, normalized by the sum of
weights actually present for that label (never a fixed denominator - a label missing one
signal just redistributes weight among the others).

| Weight | Default | What it modulates |
|---|---|---|
| `reco.collection` | `0.6` | How much owning/wanting records on this label (Discogs collection + wantlist, wantlist itself downweighted by `want_factor`) counts |
| `reco.corpus` | `0.5` | How much this label shows up in the user's listening corpus (YouTube/Spotify/Bandcamp/DJ-set history), each source pre-weighted by the `sources` group (7.5) |
| `reco.artist` | `0.4` | How strongly artists already scored highly (`ascore`) are linked to this label via the listened-to corpus (`label_artist_signal`) |
| `reco.affinity` | `0.4` | Style-affinity of everything released on this label vs. the user's taste categories (`wmap`, fed by `taste_tiers`, 7.6) |
| `reco.db_link` | `0.35` | Structural signal from the shared Discogs catalogue: do Coeur/Aime artists have releases on this label, directly or via the co-credit graph (`label_db_signal`, itself weighted by `graph.*`, 7.7) |
| `reco.want_factor` | `0.6` | Not a top-level term - discounts wantlist counts relative to owned-collection counts inside the `collection` feature above (owned counts at 1.0, wanted at `0.6`) |
| `label_affinity_floor` | `0` | Not a blended term - a hard cutoff: labels with a *known* affinity below this value (0-100 scale) are dropped from the list entirely; `0` = off. Never filters a label that has no affinity data yet (`aff is None` is kept) |

All configurable via `/settings` (`reco__*` and `label_affinity_floor` form fields), and the
`reco` group can also be auto-tuned by `learn.py` from 👍/👎 feedback on labels
(`/settings/learn/apply`, kind `"label"`).

### 7.2 Album score, and Track score (same formula) - `Ctx.album_score(r)`, config group `album`

Both the "album score" and the "track score" are the *same function*, `album_score()`. It is
called with a release-shaped dict for albums, and a single-track dict (title formatted as
`"Artist - Title"`, its own label/style) for tracks - there is no separate track weight set.

| Weight | Default | What it modulates |
|---|---|---|
| `album.label` | `0.4` | Weight of the release/track's label score (`reco_index`, 7.1) in the final score |
| `album.artist` | `0.4` | Weight of the artist term (`a_term`, blended from credited artists' `ascore`, see next row) |
| `album.style` | `0.2` | Weight of the style-affinity term (`style_affinity_of`, fed by `taste_tiers` via `wmap`) |
| `album.artist_max_vs_mean` | `0.6` | Not a top-level term - inside the artist term, blends `0.6 * max(credited artists' scores) + 0.4 * mean(...)`, so one strong artist credit can lift a multi-artist release/track without the average diluting it fully |

Two things **not** in config, hardcoded in `scoring.py` (`album_score`, line ~688):
`K = 0.35` and `PRIOR = 45`. They shrink the final score toward a neutral 45/100 prior when
few of the three terms above have data (e.g. an unlabeled white-label with no known artist) -
so a score based on one weak signal cannot display with the same apparent confidence as one
based on all three. These are not user-configurable.

All three weights and `artist_max_vs_mean` are configurable via `/settings`
(`album__*` fields), and can be auto-tuned by feedback (`/settings/learn/apply`, kind
`"album"` - which, per `learn.FEAT`, only proposes new `label`/`artist`/`style` values, never
`artist_max_vs_mean`).

### 7.3 Artist score (`Ctx.ascore`) - config group `artist_score`, plus `artist_tiers`

The artist score (0-100 per artist) blends six features (manual tier, corpus mentions,
Discogs-collection mentions, co-credit graph proximity, DJ-set mentions, and a "has a release
on a label I follow" signal), each turned into a 0-1 ratio and weighted, normalized by the sum
of the six weights below (fixed sum here, since all six terms are always evaluated, unlike the
per-label weights in 7.1 which vary by label).

| Weight | Default | What it modulates |
|---|---|---|
| `artist_score.manual` | `0.5` | Weight of the manual tier assignment (Coeur/Aime in `/univers`), via `artist_tiers` below |
| `artist_score.corpus` | `0.18` | Weight of how often the artist appears in the listening corpus (YouTube/Spotify/Bandcamp, DJ-sets excluded - counted separately) |
| `artist_score.collection` | `0.1` | Weight of how many records by this artist are in the Discogs collection |
| `artist_score.graph` | `0.14` | Weight of co-credit graph proximity to seed artists (`graph_rescore`, itself shaped by the `graph` group, 7.7) |
| `artist_score.djset` | `0.08` | Weight of how often the artist appears specifically in DJ-set corpus lines |
| `artist_score.label_link` | `0.15` | Weight of "does this artist have a release on a label I follow" (`artist_label_signal`, filtered by style match against `wmap`) |
| `artist_tiers.1` | `1.0` | The "manual" feature's value (`0.0`-`1.0` scale) for an artist manually placed in tier 1 = Coeur |
| `artist_tiers.2` | `0.5` | Same, for tier 2 = Aime |

Raw counts for `corpus`/`collection`/`graph`/`djset` are scaled with a p95 (not max) plus
`log1p` before being weighted, so one hyperactive artist/DJ-set can't compress everyone else's
scale (see the `_robust_scale`/`_log_ratio` helpers, commented as fixing "diagnostic N5").

All configurable via `/settings` (`artist_score__*` and `artist_tiers__1`/`__2` fields). Not
wired to the feedback auto-tuner (`learn.FEAT` only covers `label` and `album` kinds).

### 7.4 Shared upstream weights (feed more than one score above)

These are not weights of label/album/artist/track score directly, but of two lower-level
building blocks (`graph_rescore` and `seed_category_weight`) that several of the scores above
depend on.

| Weight | Default | Feeds |
|---|---|---|
| `taste_tiers.1` | `1.0` | Style weight for taste category 1 (Coeur styles) inside `wmap` -> feeds album/track score's `style` term (7.2) and label score's `affinity` term (7.1) |
| `taste_tiers.2` | `0.6` | Same, for taste category 2 (Aime styles) |
| `taste_tiers.3` | `0.3` | Declared in `DEFAULT_SCORING` but **vestigial**: taste categories were collapsed from 3 to 2 tiers earlier; no `taste_categories["3"]` can exist any more (`load_config` merges any legacy cat.3 into cat.2), so this value is never looked up by `wmap`. Kept only so an old config file with a stray key doesn't error. Not exposed in `/settings` |
| `graph.tier_w.1` | `3.5` | Weight of a Coeur artist as a "seed" of the co-credit graph -> feeds `graph_rescore` -> artist score's `graph` term (7.3) and label score's `db_link` term via `label_db_signal` (7.1) |
| `graph.tier_w.2` | `1.5` | Same, Aime artists |
| `graph.tier_w.3` | `0.6` | Legacy third tier, same vestigial status as `taste_tiers.3` above |
| `graph.tier_w.none` | `0.3` | Floor weight for any other artist that becomes a graph seed (global mode) |
| `graph.artist_breadth` | `0.4` | Bonus in `graph_rescore` for an artist being co-credited with *many distinct* seeds, not just one repeatedly |
| `graph.label_breadth` | `0.5` | Same idea, for labels linked to many distinct seeds |
| `graph.cat1_bonus` | `2.0` | Flat bonus added when at least one co-credit involves a Coeur (tier 1) artist |
| `graph.role_main` | `1.0` | Credit-role weight when building the graph (`job_build_graph`, `crate_jobs.py`): "Main" artist credit |
| `graph.role_remix` | `0.7` | Credit-role weight for "Remix"/"Producer" credits |
| `graph.role_other` | `0.4` | Credit-role weight for any other role not listed |
| `graph.role_written` | `0.05` | Near-zero weight for "Written-By" credits, deliberately - otherwise a prolific songwriter (e.g. Lennon-McCartney, per an in-code comment) becomes an artificial hub via unrelated cover versions |
| `graph.max_credits` | `6` | Above this many co-credited names on one release, the release is treated as a compilation and skipped entirely while building the graph |
| `graph.max_levels` | `4` | How many BFS hops from a seed artist the graph traversal goes |
| `graph.level_decay` | `0.5` | Multiplier applied to edge weight per additional hop level |
| `graph.node_cap` | `150` | Max nodes visited per seed traversal |
| `sources.discogs_collection` | `1.0` | Trust/weight of a "discogs_collection" corpus entry, used in `corpus_label_scores` (label score's `corpus` term, 7.1) and in `seed_category_weight` (artist score's `graph` term, 7.3, indirectly) |
| `sources.discogs_want` | `0.6` | Same, wantlist-sourced corpus entries |
| `sources.youtube` | `0.5` | Same, YouTube listening history |
| `sources.spotify` | `0.5` | Same, Spotify listening history |
| `sources.bandcamp` | `0.9` | Same, Bandcamp (weighted higher - an explicit purchase/follow is a stronger signal than passive listening) |
| `sources.djset` | `0.4` | Same, DJ-set corpus entries |

None of `graph.*` or `sources.*` are exposed as fields on `/settings` - they can only be
changed by editing the per-user `crate_radar_config.json` config file directly (the deep-merge
in `store.load_config()` will accept a partial override). Note also that `crate_jobs.py`
declares its own module-level `SOURCE_WEIGHTS` dict (line ~136) with values identical to
`sources` above, but it is dead code - grep shows it is never referenced after being defined,
so `sources.*` in config is the only copy that actually affects any score.

### 7.5 Declared but currently unused: `learn`

| Weight | Default | Status |
|---|---|---|
| `learn.l2` | `2.0` | Declared in `DEFAULT_SCORING`, intended as the L2 regularization strength for the feedback-driven weight auto-tuner |
| `learn.min_feedback` | `12` | Intended minimum number of 👍/👎 votes before a re-tune proposal is offered |
| `learn.min_per_class` | `3` | Intended minimum up-votes and down-votes each |
| **Actual behaviour** | - | `learn.summary()` and `learn.fit()` (`radar_web/radar/learn.py`) never read `cfg["scoring"]["learn"]` - they use their own hardcoded defaults (`min_fb=12`, `min_cls=3` as function parameters, `l2=2.0` inside `fit()`), which currently happen to match the config defaults. Changing `scoring.learn.*` in the config file has **no effect** on the app today |

## 8. Key design decisions relevant to this slice

- **Normalization is always "by weights actually in play," never a fixed sum.** Both
  `reco_rows` (label score) and `album_score` build a list of `(weight, value)` pairs only for
  terms that have data, then divide by the sum of those weights - so a release with no known
  style, or a label never profiled, doesn't get an artificially low score just because one
  weighted term was unavailable. Comments in the code call this out explicitly as a past bug
  fix ("diagnostic N2").
- **`album_score()` is one function serving two concepts** (album and track). There is no
  `track_score()` - "track score" simply means calling `album_score()` with a one-track input
  dict. Anyone looking to change "track scoring" specifically needs to change `album.*`, which
  also changes release/album scoring identically.
- **Two near-identical config keys, `reco` and `recos`, mean different things.** `scoring.reco`
  is the label-score weight group documented in 7.1. `scoring.recos` (with an `s`) is unrelated
  - it holds RECOS-playlist thresholds (`min_score`, `max_new_releases`, `searches_per_run`,
  `max_tracks`) that filter/consume an already-computed track score rather than feed the
  formula. Same naming trap for `scoring.scorestore.min_score`, a third independent threshold.
- **`scoring.learn.*` is dead configuration** (7.5) - present in defaults and migrated from
  legacy configs, read by nothing.
- **A 3rd taste/artist tier ("Cat.3") is vestigial.** `taste_tiers.3` and `graph.tier_w.3`
  still carry default values from before Coeur/Aime/Cat.3 was collapsed to a two-tier
  Coeur/Aime system; no category can hold a "3" any more, so these values are unreachable dead
  weight kept only for safe migration of old config files.
- **The shrinkage constants in `album_score` (`K=0.35`, `PRIOR=45`) are hardcoded**, not part
  of `cfg["scoring"]`, unlike almost every other constant in the formula. Changing how
  aggressively thin-evidence scores get pulled toward neutral requires an code edit, not a
  Settings change.
- **`graph.*` and `sources.*` have no Settings UI**, even though they measurably affect artist
  and label scores (via the co-credit graph and per-source trust). They are only reachable by
  hand-editing the per-user JSON config.
- **The feedback auto-tuner only ever touches two groups**: `reco` (label score) and `album`
  (album/track score) - `learn.FEAT` hardcodes exactly those two `kind`s. Artist-score weights
  (`artist_score.*`) cannot be auto-tuned from feedback today, only from the manual Settings
  sliders.

## 9. Key files table

| File | Purpose |
|---|---|
| `radar_web/radar/store.py` | `DEFAULT_SCORING` - the single source of truth for every weight's default value; `load_config()` merges/migrates a user's saved config over these defaults |
| `radar_web/radar/scoring.py` | `Ctx` class - reads `self.scoring`, computes label score (`reco_rows`/`reco_index`), artist score (`ascore`), album/track score (`album_score`), plus the upstream `graph_rescore`/`seed_category_weight`/`wmap` building blocks |
| `radar_web/radar/learn.py` | Feedback-driven weight re-tuning for the `reco` and `album` groups only |
| `radar_web/radar/scorestore.py` | SQLite cache of precomputed `album_score()` outputs (releases/tracks); consumes weights, stores no weights itself |
| `crate_jobs.py` (`job_build_graph`) | Reads `scoring.graph.*` to build the co-credit graph that `graph_rescore` later re-scores per request |
| `crate_jobs.py` (`job_scorestore_releases`/`job_scorestore_tracks`) | Read `scoring.scorestore.*` thresholds; call `Ctx.album_score()` in bulk for background precomputation |
| `crate_jobs.py` (`job_scan_recos`/`job_publish_recos`) | Read `scoring.recos.*` thresholds against precomputed track scores (RECOS feature, external to this doc) |
| `radar_web/app.py` (`settings_page`/`settings_save`) | GET/POST `/settings` - the only UI surface for changing most weights |
| `radar_web/app.py` (`settings_learn`/`settings_learn_apply`) | GET `/settings/learn`, POST `/settings/learn/apply` - feedback-driven weight proposals for `reco`/`album` |
| `radar_web/templates/pages/settings.html` | The settings form itself - one block per weight group, inputs named `<group>__<key>` |
| `radar_web/radar/paths.py` | Resolves the per-user `crate_radar_config.json` path that holds the effective (overridden) weights |

**Where to look for any weight-related task:**
- Change a default value everywhere (new installs / unset configs): `DEFAULT_SCORING` in
  `radar_web/radar/store.py`.
- Change how a weight is *used* in the formula (label/album/artist/track score math):
  `radar_web/radar/scoring.py`, inside `Ctx` (`_compute_reco_rows`, `album_score`,
  `_compute_ascore`, `_compute_graph_rescore`).
- Add a new weight to the Settings UI: add the field to
  `radar_web/templates/pages/settings.html` (`name="<group>__<key>"`) and add
  `(<group>, (<key>, ...))` to the loop in `settings_save()` in `radar_web/app.py`.
- Wire a weight group into feedback auto-tuning: add an entry to `learn.FEAT`/`learn.SUBKEY`
  in `radar_web/radar/learn.py`.
- Change how the co-credit graph itself is built (not just re-scored per request):
  `job_build_graph` in `crate_jobs.py`, which is where `graph.role_*`/`max_credits`/
  `max_levels`/`level_decay`/`node_cap` are actually consumed.
