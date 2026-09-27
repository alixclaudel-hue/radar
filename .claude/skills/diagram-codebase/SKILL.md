---
name: diagram-codebase
description: "Documents code and draws it, at ANY scope: the whole project, one module, one folder, one script, one file, or a single feature traced end to end. Five targets — `app` (architecture), `functions` (call graph), `pipeline` (data flow), `tables` (schema), `feature` (one capability across the stack). Invoked as `/diagram-codebase <target> [scope]`, where scope is a folder, a file, or a feature described in words: `/diagram-codebase app src/billing`, `/diagram-codebase feature le paiement par carte`, `/diagram-codebase pipeline scripts/refresh_prices.py`, `/diagram-codebase functions components/checkout.tsx`. Each target generates a structured doc via its own agent (cached, patched incrementally from git diff), turns it into a diagram brief, and renders it to .excalidraw. Use whenever the user asks to diagram, visualize, draw, sketch or map code — the whole app or any part of it. English: 'diagram the app', 'draw the architecture', 'visualize this module', 'diagram this script', 'show me how this feature works as a diagram', 'call graph', 'ETL diagram', 'data flow diagram', 'ER diagram', 'schema diagram', 'map the codebase'. French: 'fais un diagramme de', 'schématise', 'dessine l'architecture', 'visualise ce module', 'diagramme ce script', 'comment marche cette fonctionnalité, en schéma', 'graphe d'appels', 'schéma de données', 'diagramme du pipeline'. IMPORTANT: a module, folder, file, script or feature named anywhere in the request IS the scope — carry it through to the skill as the argument after the target, and never silently widen it to the whole project."
---

# Diagram Codebase

One workflow, five targets. Every target follows the same three steps:

```
[doc agent, in its own context, writes a .md]  →  diagram-brief  →  diagram-render
      cached; patched incrementally from git diff        ~300 tokens      a script
```

The separation is the point. The doc agent reads every significant file in the
codebase in its own context and **writes a file rather than reporting back**.
The diagram step only reads that cached markdown. The render step reads nothing
at all — it runs a script.

Only step 1 spawns an agent, and only when the doc is missing or stale. Steps 2
and 3 stay in this conversation on purpose: a brief is a few hundred tokens, and
rendering is a single script call. Spawning an agent for either would cost more
than doing it here.

---

## Step 0 — Settle the target and the scope

Read the arguments given to this skill. They arrive after `ARGUMENTS:`.

### 0a. The target

The **first word** of the arguments is the target. Accept these spellings:

| Target | Also accepts |
|---|---|
| `app` | `overview`, `architecture`, `archi`, `structure`, `stack` |
| `functions` | `function`, `calls`, `call-graph`, `callgraph` |
| `pipeline` | `data-pipeline`, `etl`, `dataflow`, `data-flow` |
| `tables` | `schema`, `er`, `data-tables`, `data-model`, `entities` |
| `feature` | `flow`, `parcours`, `fonctionnalite`, `end-to-end`, `e2e` |

**No arguments at all** — do not guess. Ask which of the five, in one line,
listing them with what each draws:

```
Quelle cible ?
  app        l'architecture : entrées, point d'entrée, logique, sorties
  functions  le graphe d'appels, une swim lane par module
  pipeline   le flux de données, étape par étape
  tables     le schéma des structures de données et leurs relations
  feature    une fonctionnalité de bout en bout, à travers les couches

Chacune accepte un scope : un dossier, un fichier, un script, ou une
fonctionnalité décrite en français. Sans scope, c'est tout le projet.
```

**A first word that matches nothing** — treat the whole argument as a scope for
the `app` target and say so before starting, so the user can correct it.

### 0b. The scope

Everything **after** the target word is the optional `focus`: a folder, a file,
or a feature described in prose. `/diagram-codebase app src/billing`,
`/diagram-codebase pipeline the nightly refresh chain`.

**No focus — the whole project.** Use the names in the target table below,
unchanged, so existing projects keep their files.

**Except for `feature`, where the focus is mandatory.** A feature with no name
is not a diagram anyone can draw. When the target is `feature` and nothing
follows it, ask which feature, in one line, and stop until answered. Do not fall
back to the whole project.

**Focus given.** Build a slug from the focus text: lowercase, every run of
non-alphanumeric characters becomes a single `-`, trimmed, at most 40
characters. Insert `-<slug>` before the extension of `doc`, `brief` and
`drawing`, and append `:<slug>` to `meta_key`.

Separate names are not a detail: a module drawing that overwrote the
whole-project one would quietly destroy work. Never mix the two.

If the focus is a path that exists in the repository (check with Bash), keep it
as `focus_paths` — Step 1 uses it to decide whether the doc is stale. A feature
described in prose has no `focus_paths`, and that is fine.

### 0c. The target table

| Target | `agent` | `doc` | `brief` | `drawing` | `meta_key` | default `pathspec` |
|---|---|---|---|---|---|---|
| `app` | `app-overview` | `docs/app-overview.md` | `docs/app-diagram-brief.md` | `docs/app-overview.excalidraw` | `app-overview` | `-- . ':(exclude)docs/'` |
| `functions` | `function-doc-generator` | `docs/function-map.md` | `docs/function-diagram-brief.md` | `docs/function-diagram.excalidraw` | `function-map` | `-- . ':(exclude)docs/'` |
| `pipeline` | `data-pipeline-doc-generator` | `docs/data-pipeline-map.md` | `docs/data-pipeline-diagram-brief.md` | `docs/data-pipeline-diagram.excalidraw` | `data-pipeline-map` | `-- . ':(exclude)docs/'` |
| `tables` | `data-tables-doc-generator` | `docs/data-tables-map.md` | `docs/data-tables-diagram-brief.md` | `docs/data-tables-diagram.excalidraw` | `data-tables-map` | `-- . ':(exclude)docs/'` |
| `feature` | `app-overview` | `docs/feature-<slug>.md` | `docs/feature-<slug>-brief.md` | `docs/feature-<slug>.excalidraw` | `feature:<slug>` | `-- . ':(exclude)docs/'` |

`feature` already carries its slug in the table, because it always has a focus.
Do not add a second one.

`feature` reuses the `app-overview` agent — it is the one that reads across the
whole codebase and maps files to routes and data flow. What changes is the
question put to it: `app` asks what the layers are, `feature` asks what happens,
in order, when someone uses this one capability.

The default pathspec is deliberately broad. Guessing which folders hold the
source means guessing the project's shape, and that is exactly what breaks a
skill when it moves to a project built differently. Excluding `docs/` is enough
to stop this skill's own output from looking like a source change. A project
that wants it narrower records `scope` in `.doc-meta.json` (Step 1c).

Everywhere below, `<agent>`, `<doc>`, `<brief>`, `<drawing>`, `<meta_key>` and
`<pathspec>` mean the values decided here.

---

## Step 1 — Ensure the doc is up to date

### 1a. Read current state

1. Bash: `git rev-parse HEAD` → `current_commit`.
   - If it fails (not a repository, or no commits), set `current_commit = null`.
     There is nothing to compare and no cache to keep: go straight to Branch A
     and skip Step 1c.
2. Read `docs/.doc-meta.json`:
   - If it exists, parse it and take `<meta_key>.commit` as `last_commit`, and
     `<meta_key>.scope` as `scope` if present (a list of git pathspecs recorded
     by a previous run).
   - If the file or the key is missing: `last_commit = null`, `scope = null`.
3. Read `<doc>` → `doc_exists`.

Nothing here assumes a particular project layout, and nothing below should
either. The doc agent finds the code by itself; this skill only decides whether
it needs to run again.

### 1b. Decision tree

**Branch A — `doc_exists` is false OR `last_commit` is null**

Full generation. Use the Agent tool with:

- `subagent_type`: `<agent>`
- `description`: `"Generate <target> doc"`
- `prompt`: `"Generate the full <target> documentation for this project following your documented format. Write the output to <doc> (create docs/ if needed). Do not print it to the conversation — only write the file."`

For `functions` and `pipeline`, append one sentence describing the project,
taken from `CLAUDE.md` or `README.md`, so the agent has context.

**For the `feature` target, replace the prompt entirely with this one:**

```
Trace one capability of this project end to end: <focus>.

Write the result to <doc> (create docs/ if needed). Do not print it to the
conversation — only write the file.

Follow the execution path, not the folder structure. Start where the user or
the caller enters, and follow what actually happens, in order, until the work
is done and something is returned, stored or displayed. For each hop, record:

- what it is (a route, a component, a hook, a handler, a service, a query,
  a table, an external API)
- the file and, when it helps, the symbol
- which layer it belongs to: UI, API, domain, data, or external
- what is handed to the next hop — the payload, the call, the record

Then record, separately: the branches (what happens on failure, on an empty
result, on a permission denial), and anything you expected to find and did not.

If the capability does not exist under that name, say so in the document rather
than assembling a plausible path. If it turns out to be several capabilities,
name them and trace the main one, listing the others.
```

When `focus` is set, append this instead of asking for the whole project
(targets `app`, `functions`, `pipeline` and `tables` only — the `feature`
prompt above already scopes itself):

```
Document only this part of the project: <focus>.

Read whatever else you need in order to understand it — what calls into it,
what it calls, what data it reads and writes — but describe only this part.
Name the boundary explicitly: say what is inside and what you treated as
external to it. If the focus turns out not to exist, or to be far larger than
described, say so in the document rather than guessing.
```

The agent starts with no knowledge of this conversation, so `focus` has to be
spelled out in the prompt; a folder name alone is often not enough.

Wait for the agent to complete. Then go to Step 1c.

**Branch B — `doc_exists` is true AND `last_commit == current_commit`**

The doc is current. Tell the user:
`"<doc> is current (last documented at <last_commit[:7]>, matches HEAD). Skipping doc generation."`

Read `<doc>` into context, then go to Step 2.

**Branch C — `doc_exists` is true AND `last_commit != current_commit`**

Decide the pathspec to compare, in this order:

1. `focus_paths` from Step 0, if set — a drawing of one folder goes stale when
   that folder changes, not when the rest of the repository does.
2. Otherwise `scope` from `.doc-meta.json`, if it is a non-empty list:
   `-- <scope[0]> <scope[1]> ...`
3. Otherwise the target's default `<pathspec>`.

Then: `git diff <last_commit>..<current_commit> <pathspec>`

- Diff **empty** → treat as Branch B (scope unchanged despite hash mismatch).
- Diff **≤ 150 lines** → incremental update agent below.
- Diff **> 150 lines** → ask:
  `"The diff since the last doc generation is ~N lines. Incremental update (patch affected sections only) or full regeneration?"`
  Full → Branch A. Incremental → below.

**Incremental update agent**

Bash: `git log --oneline <last_commit>..<current_commit>`. Read `<doc>`.

Use the Agent tool with `subagent_type: "general-purpose"`,
`description: "Patch <target> doc"`, and:

```
<doc> was last generated at commit <last_commit>.
Since then, the following commits landed:

<git log output>

Here is the diff of all changed files (scope: <pathspec used above>):

<git diff output>

Here is the current contents of <doc>:

<doc contents>

Your task: update <doc> to reflect only what changed in the diff.
Rules:
- Add entries for newly added files, functions, stages, structures or fields
- Remove entries for anything the diff deletes
- Update descriptions for whatever the diff modifies
- Do NOT touch sections whose source files do not appear in the diff
- Preserve the exact document format, heading structure, and writing style verbatim
- Write the complete updated file to <doc>
```

Wait for the agent to complete. Then go to Step 1c.

### 1c. Update `docs/.doc-meta.json`

Skip entirely when `current_commit` is null.

Otherwise read the current `docs/.doc-meta.json` (treat a missing file as `{}`),
then write it back with `<meta_key>.commit` set to `current_commit`. Preserve
every other key unchanged, including any `scope` already recorded — a project
that has narrowed its scope by hand should keep it.

Record `scope` yourself only when Step 0 produced `focus_paths`; that is a real
pathspec, not a guess. Otherwise leave `scope` absent: its absence is what
selects the broad default.

Read `<doc>` into context for Step 2.

---

## Step 2 — Create the diagram brief

Always run this step, and Step 3 after it, whatever the cache decided in Step 1.
Both are cheap, and rebuilding them from the current doc is the only way to be
sure the drawing matches it.

Use the Skill tool to invoke `diagram-brief`. The args depend on the target.

### Target `app`

```
Read <doc> and produce a diagram brief at <brief>.

Layout: top-to-bottom (layered architecture following the main flow).
Flow summary: Show how data or requests enter the project, what receives them, what does the work, and where the results end up.

Source sections to use, in priority order. Skip any the document does not have:
1. "How the app works - the main flow" — spine of the diagram
2. "State / Data flow summary" — add state nodes and their transitions
3. "Framework / Tech Stack" — label each layer with what powers it
4. "Key files table" — annotate key nodes with the responsible filename

Draw layers top-to-bottom. Name each layer after what this project actually
does, not after a web app. The four below are the usual shape; adapt the labels
to what the doc describes:
- Layer 1 (Inputs): where data or requests come from — APIs, databases, files, schedules, user input
- Layer 2 (Entry point): what receives them — HTTP router, main(), CLI parser, scheduled job, message consumer
- Layer 3 (Logic): the units that do the work — one box per major unit, whatever it is called here
- Layer 4 (Outputs): where results go — stores, databases, generated files, published artefacts, external calls

Use fewer layers when the project has fewer. Never add a layer to fill the
shape, and never label a layer with a technology the doc does not mention.

Scale:
- Small (<10 key files): one box per file from the key files table
- Medium (10-20): group by feature area or by stage
- Large (>20): layers + the 8-10 most architecturally significant units; note the rest as "+N more"

Save output to <brief>.
```

### Target `functions`

```
Read <doc> and produce a diagram brief at <brief>.

Layout: grouped (one swim lane per module, function boxes inside their module's lane).
Flow summary: Show how functions within and across modules call each other, with hub functions as the primary anchors.

Draw:
- One group (swim lane) per product module from the "Product Modules" section
- Function boxes inside their module's lane, arranged so callers appear before their callees
- Hub functions (marked [hub] in the doc) as prominent boxes within their lane
- Intra-module call arrows between functions in the same lane
- Cross-module call arrows between functions in different lanes

Scale:
- Fewer than 30 functions: show every function individually
- 30-80: show all functions
- More than 80: module lanes + hub functions only; add a note per module listing the non-hub count ("+12 more in Module A")

Save output to <brief>.
```

### Target `pipeline`

```
Read <doc> and produce a diagram brief at <brief>.

Layout: left-to-right (data flow, each pipeline as a separate horizontal row).
Flow summary: Show how raw inputs are transformed stage-by-stage into final output files, with multiple pipelines stacked as parallel rows.

Draw:
- Raw inputs as entry boxes on the left (one per raw file or external data source)
- Processing stages in the middle (one box per stage, labeled with stage name + script filename)
- Intermediate files between the stages that produce and consume them
- Final output files on the right (anything written but not read by a later stage)
- Multiple independent pipelines: stack them vertically with a section label for each
- A pipeline with more than 6 stages: abbreviate middle stages with a "..." note

Arrows:
- From each raw input to the first stage that reads it
- Between stages where one stage's output feeds the next
- From each final stage to its output file(s), labeled with the filename being passed

Save output to <brief>.
```

### Target `tables`

The renderer draws boxes, notes, groups-as-colors and labeled arrows. It does
**not** draw two-zone ER rectangles, dashed strokes, or per-row field cells. The
brief below expresses the schema within that vocabulary; do not ask for ER
chrome the renderer will silently drop.

```
Read <doc> and produce a diagram brief at <brief>.

Layout: grouped by origin, related structures placed near each other.
Flow summary: Show every data structure in the project, what it holds, and how the structures join to one another.

Draw:
- One box per data structure, labeled with its name
- Its Note carries: type and location on the first line, then the fields as
  "fieldName  type", one per line
- Structures with more than 12 fields: list the primary key, the foreign keys
  and the 5 most significant fields, then "+N more fields (see <doc>)"

Groups — use the "Produced by" field of the Relationships Summary to sort every
structure into exactly one:
- Raw inputs (source files as delivered)
- Intermediate processed files
- App-facing outputs (served to a frontend)
- Database tables
- External API schemas
Skip any group the project does not have. Groups become colors, so a structure
in no group reads as uncategorized — place every one.

Arrows — one per relationship in the Relationships Summary:
- Label it with the join, "on fieldA = fieldB", or "spatial join" for geographic ones
- An optional or soft reference is labeled "optional: fieldA = fieldB"
- Draw the arrow from the producing structure to the consuming one

Scale:
- Fewer than 6 structures: all fields on every box
- 6-12: all fields, grouped as above
- More than 12: names plus key fields only, and a note "full schema in <doc>"

Save output to <brief>.
```

### Target `feature`

```
Read <doc> and produce a diagram brief at <brief>.

Layout: left-to-right (the execution path, in order).
Flow summary: Show what happens, hop by hop, when someone uses this capability — from the entry point to the last thing written, returned or displayed.

Draw:
- One box per hop, in execution order, left to right
- Each box labeled with what the hop is ("POST /api/pay", "useCheckout", "stripe.ts")
- Its Note carries the file path, and the symbol when the file holds several
- The entry point marked as entry point
- Anything outside this codebase marked as external

Groups — one per layer, in this order, skipping any the capability does not touch:
UI, API, domain, data, external.
Groups become colors, so the drawing reads as a path crossing bands of color.

Arrows:
- One per hop, following execution order
- Label it with what is handed over: the payload, the call, the record
  ("order id", "POST body", "row inserted")
- Branches (failure, empty result, permission denied) as arrows leaving the hop
  that decides, labeled with the condition

Scale:
- Fewer than 12 hops: every hop individually
- 12-20: collapse consecutive hops inside one file into a single box
- More than 20: the capability is probably several — draw the main path and
  note the rest

Never invent a hop to make the path look complete. A gap the document reports
is drawn as a gap, with a note saying what was not found.

Save output to <brief>.
```

### When a focus was set

Append to the args for `app`, `functions`, `pipeline` and `tables`. The
`feature` args above are already scoped — do not append it there:

```
This diagram covers only <focus>, not the whole project. Draw that part in
detail, and represent everything outside it as a single box per external
dependency — named, but not opened up. Say in the Notes what the boundary is.
```

Without that, the brief happily draws the whole document and the drawing stops
matching what was asked for.

---

## Step 3 — Render the brief

The `diagram-render` skill ships the generator script. Call it directly — do
**not** hand-write Excalidraw JSON, and do **not** spawn an agent for this. The
script does the whole transform in well under a second.

Run via Bash, from the project root:

```
python "$HOME/.agents/skills/diagram-render/scripts/brief_to_excalidraw.py" <brief> <drawing>
```

Notes:

- On Windows use `python`; `python3` there opens the Microsoft Store and runs
  nothing. On macOS and Linux use `python3`.
- If `$HOME` does not expand, substitute the absolute path to
  `~/.agents/skills/diagram-render/scripts/`.
- The script prints one summary line, `groups / boxes / arrows`. Read it: if
  **boxes is 0**, the brief did not match the directive format the script
  expects. In that case invoke the `diagram-render` skill with the Skill tool
  and let it handle the fallback, rather than patching the JSON by hand.
- Relay any WARNING line the script prints (an arrow referencing a box that was
  never defined), and still deliver the file.

The output overwrites `<drawing>` if it exists, which is what we want — the file
tracks the current state of the code.

---

## Final output to report to user

- `<doc>` — full text documentation, reusable and worth regenerating whenever the code moves
- `<brief>` — step-by-step drawing instructions, in case the drawing needs adjusting by hand
- `<drawing>` — the diagram itself, ready to open at excalidraw.com or in the VS Code Excalidraw extension

Say what the drawing covers: the whole project, or the `focus` that was asked
for. When a focus was used, mention that the whole-project files, if any, were
left untouched.

Report the script's `groups / boxes / arrows` counts so the user knows how big
the drawing is before opening it.
