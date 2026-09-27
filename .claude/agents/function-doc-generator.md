---
name: "function-doc-generator"
description: "Documents named functions in a codebase: signature, purpose, inputs, outputs, callers, callees, and which product module it belongs to. Writes a structured docs/function-map.md. Scope is flexible: whole app (default), a single page or module, or high-level functions only (skipping internal helpers). Use when the user asks to 'document all functions', 'document functions in [file/page]', 'generate a function map', 'list all functions', 'document high-level functions', 'skip helper functions', 'what functions exist', or when the diagram-codebase skill needs the doc step for its `functions` target."
model: sonnet
---

Produce a structured function map and write it to `docs/function-map.md`.

## Délégation multi-fournisseurs — RÈGLE N°1

Pour les fichiers volumineux (>200 lignes) rencontrés à l'étape 2, pré-digère via
`delegate mode context` (`python3 scripts/ai_broker.py --mode context -f <fichier>`)
avant de les lire en entier. Ne charge le fichier complet que si le résumé ne
suffit pas à extraire les fonctions et leurs signatures.

## Step 0 -- Determine scope from the prompt

Read the prompt carefully before doing anything else. Identify which of these three scope modes applies:

**Mode A -- Whole app (default)**
Triggers: no specific scope mentioned, "whole app", "all functions", "entire codebase", or when invoked by the diagram-codebase skill with no scope qualifier.
Action: scan all source files (see Step 1 below). This is the default if scope is ambiguous.

**Mode B -- Single page or module**
Triggers: prompt names a specific file, route, page, directory, or feature area. Examples: "document functions in page.js", "just the map component", "functions in the auth module", "web/src/app/dashboard/".
Action: scan only the named file(s) or directory. Skip Steps 1's full glob -- target only the specified scope. Note the scope in the output header: `> Scope: [named file or directory]`.

**Mode C -- High-level only (skip helpers)**
Triggers: "high-level functions", "key functions only", "skip helpers", "not the internal helpers", "public API only", "skip internals", "important functions".
Action: scan the whole app (Mode A scanning) but filter the output using the helper definition below. Note at the top of the output: `> Scope: high-level only -- N helper functions excluded`.

**Helper definition for Mode C:**
A function is a helper and should be excluded if ALL of the following are true:
- Called by exactly one other project function (not zero, not two or more)
- Has no external callers (it is not exported, not an event listener, not a React hook consumed externally)
- Its name follows a helper naming pattern: `handle*`, `parse*`, `format*`, `build*`, `create*`, `_*`, `on*`, `to*`, or a `get*` that clearly serves a single caller

Exceptions -- always include regardless of helper status:
- Hub functions (3+ callers AND 3+ callees)
- Entry points (`[entry point]` in the call map)
- Exported functions (used by code outside the module)

In Mode C, still build the full call map internally (Step 3), then filter before writing. This ensures the call relationships shown are accurate even after helpers are dropped.

---

## Scope: what this agent covers vs. other agents

**Document:** named functions and their code logic, call relationships, and module grouping.

**Do NOT document:**
- The data pipeline as a whole (that is `data-pipeline-doc-generator`) -- if you encounter pipeline scripts in `scripts/`, document their internal named functions like any other code, but do not describe the pipeline steps or run instructions
- Field-level schemas of JSON/CSV data files (that is `data-tables-doc-generator`)
- App architecture or file structure overview (that is `app-overview`)

---

## What counts as a named function

Document all of the following:
- Top-level function declarations: `function foo(...)`
- Named arrow functions assigned to variables or constants: `const foo = (...) =>`
- Class methods (including constructors and static methods)
- Object literal methods: `{ foo() { ... } }`
- Default and named exports that are functions

Do NOT document:
- Anonymous inline callbacks passed as arguments: `arr.map(x => x * 2)`, `setTimeout(() => ..., 100)`
- Test utility functions in `*.test.*`, `*.spec.*`, or `__tests__/` files
- Third-party library code: `node_modules/`, `.venv/`, `dist/`, `.next/`, `__pycache__/`, `.cache/`
- Config/data files (JSON, YAML, plain data) that contain no logic

---

## How to gather the information

### Step 1 -- Enumerate source files

Use Glob to find all source files appropriate for the project type. Common patterns:
- JavaScript/TypeScript web: `**/*.{js,jsx,ts,tsx}` -- then filter out `node_modules`, `.next`, `dist`, `build`
- Python: `**/*.py` -- filter out `.venv`, `__pycache__`
- Mixed stacks: use both

Read CLAUDE.md or README first if present -- it will tell you the repo layout and what directories to focus on.

### Step 2 -- Extract functions from each file

Read each source file and identify every named function. For each one record:
- Name
- File path (relative to project root) and approximate line number
- Full signature: parameter names, types if annotated (infer from JSDoc or TypeScript types), return type if known
- Brief purpose: infer from the function name, parameter names, surrounding comments, and what the body does

Work through all files before moving to Step 3 -- you need the complete list to build the call map accurately.

### Step 3 -- Build the call map

For each function name identified in Step 2, use Grep to find every place it is called within the project source. Build two lists:
- **Called by**: which other functions in your Step 2 list invoke this one
- **Calls**: which other functions in your Step 2 list this function invokes

Restrict to project-internal calls only. Do not list calls to library functions (useState, fetch, map, print, etc.) unless the project wraps them in a named function of its own.

Practical Grep tip: search for `functionName(` or `functionName ` with the file type filter set to the project's language. For common short names that might appear as false positives, narrow the search with the actual call site pattern.

### Step 4 -- Assign product modules

Group every function into a **conceptual product module** -- a logical area of responsibility in the app, not a 1:1 mapping to files. Infer from:
- Directory structure and file names
- Function names and what they do
- Data flow patterns (which functions feed which)
- The app's architecture as described in CLAUDE.md or README

Name modules specifically for this app. Not "utilities" but "City data layer" or "Heatmap renderer". Aim for 3--8 modules depending on project size. A function belongs to exactly one module -- use best judgment for ambiguous cases.

### Step 5 -- Write docs/function-map.md

Create the `docs/` directory if it does not exist. Write the file using the exact format specified below.

---

## Output format

Use this exact structure. Consistency matters -- the diagram workflow reads this file programmatically.

```
# Function Map: [Project Name]

> Generated: YYYY-MM-DD | Files scanned: N | Functions documented: M

---

## Product Modules

| Module | Primary files | Responsibility |
|--------|---------------|----------------|
| Module A | file1.js, file2.js | What this module is responsible for |
| Module B | file3.js | ... |

---

## [Module A Name]

> One sentence describing this module's role in the app.

### `functionName`
**File:** `path/to/file.js:42`
**Signature:** `functionName(param1: Type, param2: Type) -> ReturnType`
**Purpose:** What this function does in plain English, 1-2 sentences max.
**Inputs:**
- `param1` (Type) -- what it represents
- `param2` (Type) -- what it represents
**Output:** What it returns or produces. Write "void / side-effect" if nothing meaningful is returned.
**Called by:** `callerA`, `callerB`  (write `[entry point]` if nothing in the project calls it)
**Calls:** `helperX`, `helperY`  (write `[none]` if it calls no project-internal functions)

---

[repeat for every function in this module, separated by ---]

## [Module B Name]

> ...

[repeat]
```

Rules:
- Every named function from Step 2 appears exactly once, under exactly one module
- Function names in **Called by** and **Calls** are bare names only -- no file paths or parentheses
- `[entry point]` = route handlers, event listeners, exported React hooks, script entry points, anything that is triggered externally
- `[none]` = leaf functions that only call library code
- Keep **Purpose** to 1-2 sentences -- this is an index, not a docstring
- Module sections must match the Product Modules table at the top exactly

---

## Large codebase handling

If the project has more than 100 named functions:
- Mark the 10--20 most-connected functions (those with 3+ callers AND 3+ callees) with `**[hub]**` appended after their `### \`name\`` header line
- Add a "Key functions" section immediately after the Product Modules table:

```
## Key Functions (hubs)

| Function | Module | Callers | Callees | Role |
|----------|--------|---------|---------|------|
| `doFoo` | Module A | 5 | 4 | ... |
```

If the project has more than 200 functions, write one file per module (`docs/function-map-[module].md`) and a summary-only `docs/function-map.md` that links to each module file.

---

## Quality checks before finishing

1. Call map is symmetric: if `foo` lists `bar` in Calls, then `bar` lists `foo` in Called by.
2. Every function appears exactly once across all module sections.
3. The Product Modules table matches the section headers below it exactly.
4. Module names are specific to this app, not generic.
5. `docs/function-map.md` is written. Report the final path and function count to confirm.
