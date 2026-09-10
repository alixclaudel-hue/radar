---
name: app-diagram
description: "Three-step workflow: generate a structured app overview doc (docs/app-overview.md) via the app-overview agent if one doesn't exist, produce a diagram brief (docs/app-diagram-brief.md), then render it straight to docs/app-overview.excalidraw. Use when the user asks for an architecture diagram, 'diagram the app', 'visualize the app structure', 'show the app architecture', 'draw the tech stack', 'how does the app work as a diagram', or any request that combines an app overview with a visual. Accepts an optional argument naming a module, a folder or a feature ('/app-diagram pipeline/', '/app-diagram the data refresh chain') to diagram just that part, written to its own files. The doc step is cached -- if the overview doc already exists and the code has not moved, only the diagram and render steps run."
---

# App Diagram Workflow

Orchestrates three steps in sequence: produce `docs/app-overview.md` via the `app-overview` agent, turn it into a diagram brief via the `diagram-brief` skill, then render that brief into an Excalidraw file with the bundled generator script.

Called on its own it documents the whole project. Given an argument -- a folder,
a file, or a feature described in words -- it documents only that, and writes to
its own set of files. Step 0 settles which.

The separation matters: the overview agent reads every significant file in the codebase, in its own context, and writes a file rather than reporting back. The diagram step only reads the cached markdown. The render step reads nothing at all -- it runs a script.

Only step 1 spawns an agent, and only when the doc is missing or stale. Steps 2 and 3 stay in this conversation on purpose: a brief is a few hundred tokens, and rendering is a single script call. Spawning an agent for either would cost more than doing it here.

---

## Step 0 -- Decide what to document

Read the arguments given to this skill. They arrive after `ARGUMENTS:` and are
empty when the user typed `/app-diagram` on its own.

**No arguments -- the whole project.** Use these names, unchanged from before so
existing projects keep their files:

```
doc      = docs/app-overview.md
brief    = docs/app-diagram-brief.md
drawing  = docs/app-overview.excalidraw
meta_key = app-overview
focus    = (none)
```

**Arguments given -- one module, folder or feature.** For example
`/app-diagram pipeline/`, `/app-diagram the data refresh chain`, or
`/app-diagram app/workload.tsx and what feeds it`. Take the argument text as
`focus`, and build a slug from it: lowercase, every run of non-alphanumeric
characters becomes a single `-`, trimmed, at most 40 characters. Then:

```
doc      = docs/app-overview-<slug>.md
brief    = docs/app-diagram-brief-<slug>.md
drawing  = docs/app-overview-<slug>.excalidraw
meta_key = app-overview:<slug>
```

Separate names are not a detail: a module drawing that overwrote the
whole-project one would quietly destroy work. Never mix the two.

If the argument is a path that exists in the repository (check with Bash), keep
it as `focus_paths` -- Step 1 uses it to decide whether the doc is stale. A
feature described in prose has no `focus_paths`, and that is fine.

Everywhere below, `<doc>`, `<brief>`, `<drawing>` and `<meta_key>` mean the
values decided here.

---

## Step 1 -- Ensure the app overview doc is up to date

### 1a. Read current state

Run these in sequence:

1. Use Bash: `git rev-parse HEAD` → capture as `current_commit`.
   - If the command fails (not a repository, or a repository with no commits),
     set `current_commit = null`. There is then nothing to compare and no cache
     to keep: go straight to Branch A and skip Step 1c.
2. Use Read tool on `docs/.doc-meta.json`:
   - If the file exists, parse the JSON and extract `<meta_key>.commit` as `last_commit`
   - Also extract `<meta_key>.scope` as `scope` if present -- a list of git
     pathspecs recorded by a previous run
   - If the file does not exist or the key is missing, set `last_commit = null`
     and `scope = null`
3. Use Read tool on `<doc>`:
   - If the file does not exist, set `doc_exists = false`; otherwise `doc_exists = true`

Nothing here assumes a particular project layout, and nothing below should
either. The overview agent finds the code by itself; this skill only decides
whether it needs to run again.

### 1b. Decision tree

**Branch A — doc_exists is false OR last_commit is null**

Full generation: spawn the app-overview agent:

Use the Agent tool with:
- `subagent_type`: `"app-overview"`
- `description`: `"Generate app overview doc"`
- `prompt`: `"Generate a full app overview for this project following your documented format. Write the output to <doc> (create docs/ if needed). Do not print the overview to the conversation -- only write the file."`

When `focus` is set, append this to the prompt instead of asking for the whole
project:

```
Document only this part of the project: <focus>.

Read whatever else you need in order to understand it -- what calls into it,
what it calls, what data it reads and writes -- but describe only this part.
Name the boundary explicitly: say what is inside and what you treated as
external to it. If the focus turns out not to exist, or to be far larger than
described, say so in the document rather than guessing.
```

The agent starts with no knowledge of this conversation, so `focus` has to be
spelled out in the prompt; a folder name alone is often not enough.

Wait for the agent to complete. Then go to Step 1c.

**Branch B — doc_exists is true AND last_commit == current_commit**

The doc is already up to date. Tell the user:
`"<doc> is current (last documented at <last_commit[:7]>, matches HEAD). Skipping doc generation."`

Read `<doc>` into context, then proceed to Step 2.

**Branch C — doc_exists is true AND last_commit != current_commit**

First decide the pathspec to compare, in this order:

- If `focus_paths` was set in Step 0, use it: a drawing of one folder goes stale
  when that folder changes, not when the rest of the repository does.
- Otherwise, if `scope` was found in `.doc-meta.json` and is a non-empty list,
  use it: `-- <scope[0]> <scope[1]> ...`
- Otherwise compare the **whole repository except the docs folder**:
  `-- . ':(exclude)docs/'`

The default is deliberately broad. Guessing which folders hold the source means
guessing the project's shape, and that is exactly what breaks a skill when it
moves to a project built differently. Excluding `docs/` is enough to stop this
skill's own output from looking like a source change.

Then run via Bash:
```
git diff <last_commit>..<current_commit> -- <pathspec decided above>
```

- If diff output is **empty**: treat as Branch B (scope unchanged despite hash mismatch).
- If diff has **≤ 150 lines**: run the incremental update agent below.
- If diff has **> 150 lines**: ask the user:
  `"The diff since the last doc generation is ~N lines. Incremental update (patch affected sections only) or full regeneration?"`
  - Full regeneration → spawn agent as in Branch A.
  - Incremental → run the update agent below.

**Incremental update agent:**

Run via Bash: `git log --oneline <last_commit>..<current_commit>`

Read the current contents of `<doc>`.

Use the Agent tool with:
- `subagent_type`: `"general-purpose"`
- `description`: `"Patch app overview doc"`
- `prompt`:
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
- Add sections or entries for newly added files, components, routes, or tech stack items
- Remove sections or entries for deleted files or components
- Update descriptions for modified components, routes, state, or configuration
- Do NOT touch sections whose source files do not appear in the diff
- Preserve the exact document format, heading structure, and writing style verbatim
- Write the complete updated file to <doc>
```

Wait for the agent to complete. Then go to Step 1c.

### 1c. Update docs/.doc-meta.json

Skip this step entirely when `current_commit` is null -- there is no commit to
record.

Otherwise use the Read tool to get the current `docs/.doc-meta.json` content
(treat a missing file as `{}`), then use the Write tool to save it back with
`<meta_key>.commit` set to `current_commit`. Preserve every other key
unchanged, including any `scope` already recorded -- a project that has narrowed
its scope by hand should keep it.

Record `scope` yourself only when Step 0 produced `focus_paths`; that is a real
pathspec, not a guess. Otherwise leave `scope` absent: its absence is what
selects the broad default above.

Read `<doc>` into context so you have the architecture data for Step 2.

---

## Step 2 -- Create the diagram brief

Always run this step, and Step 3 after it, whatever the cache decided in Step 1.
Both are cheap, and rebuilding them from the current overview is the only way to
be sure the drawing matches the document. If no brief or `.excalidraw` exists
yet, this is simply the first time they are built.

Use the Skill tool to invoke `diagram-brief` with these args:

```
Read <doc> and produce a diagram brief at <brief>.

Layout: top-to-bottom (layered architecture following the main flow).
Flow summary: Show how data or requests enter the project, what receives them, what does the work, and where the results end up.

Source sections to use, in priority order. Skip any that the document does not have:
1. "How the app works - the main flow" — spine of the diagram
2. "State / Data flow summary" — add state nodes and their transitions
3. "Framework / Tech Stack" — label each layer with what powers it
4. "Key files table" — annotate key nodes with the responsible filename

Draw layers top-to-bottom. Name each layer after what this project actually
does, not after a web app. The four below are the usual shape; adapt the labels
to what the overview describes:
- Layer 1 (Inputs): where data or requests come from — APIs, databases, files, schedules, user input
- Layer 2 (Entry point): what receives them — HTTP router, `main()`, CLI parser, scheduled job, message consumer
- Layer 3 (Logic): the units that do the work — one box per major unit, whatever it is called here: components, views, handlers, transforms, stages, services
- Layer 4 (Outputs): where results go — stores, databases, generated files, published artefacts, external calls

Use fewer layers when the project has fewer. A library or a pipeline may have
only inputs, stages and outputs. Never add a layer to fill the shape, and never
label a layer with a technology the overview does not mention.

Scale:
- Small project (<10 key files): one box per file from the key files table
- Medium project (10-20 key files): group by feature area or by stage
- Large project (>20 key files): layers + the 8-10 most architecturally significant units; note the rest as "+N more"

Save output to <brief>.
```

When Step 0 set a `focus`, add these lines to the args as well:

```
This diagram covers only <focus>, not the whole project. Draw that part in
detail, and represent everything outside it as a single box per external
dependency -- named, but not opened up. Say in the Notes what the boundary is.
```

Without that, the brief happily draws the whole document and the drawing stops
matching what was asked for.

---

## Step 3 -- Render the brief to Excalidraw

The `brief-to-excalidraw` skill ships a generator script. Call it directly -- do
**not** hand-write Excalidraw JSON, and do **not** spawn an agent for this. The
script does the whole transform in well under a second.

Run via Bash, from the project root:

```
python "$HOME/.claude/skills/brief-to-excalidraw/scripts/brief_to_excalidraw.py" <brief> <drawing>
```

Notes:

- On Windows use `python`; `python3` there opens the Microsoft Store and runs
  nothing. On macOS and Linux use `python3`.
- If `$HOME` does not expand, the skills live under `~/.claude/skills/` --
  substitute the absolute path.
- The script prints one summary line, `groups / boxes / arrows`. Read it: if
  **boxes is 0**, the brief did not match the directive format the script
  expects. In that case invoke the `brief-to-excalidraw` skill with the Skill
  tool and let it handle the fallback, rather than patching the JSON by hand.

The output overwrites `<drawing>` if it already exists, which
is what we want -- the file tracks the current architecture.

---

## Final output to report to user

Once both steps are complete, tell the user:
- `<doc>` -- full text overview, reusable and worth regenerating whenever the code moves
- `<brief>` -- step-by-step drawing instructions, in case the drawing needs adjusting by hand
- `<drawing>` -- the diagram itself, ready to open at excalidraw.com or in the VS Code Excalidraw extension

Say what the drawing covers: the whole project, or the `focus` that was asked
for. When a focus was used, mention that the whole-project files, if any, were
left untouched.

Report the script's `groups / boxes / arrows` counts so the user knows how big the drawing is before opening it.
