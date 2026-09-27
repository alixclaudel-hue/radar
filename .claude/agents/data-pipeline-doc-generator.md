---
name: "data-pipeline-doc-generator"
description: "Documents a project's data pipeline: every script or notebook that transforms raw data into processed outputs. For each pipeline stage: what it reads, what it writes, the algorithmic steps, external APIs called, and how to run it. Writes docs/data-pipeline-map.md. Use when the user asks to 'document the data pipeline', 'describe how data is processed', 'what do the pipeline scripts do', 'document the ETL', 'how is raw data transformed', or when the diagram-codebase skill needs the doc step for its `pipeline` target. Does NOT document individual function internals (use function-doc-generator for that) or data field schemas (use data-tables-doc-generator for that)."
model: sonnet
---

Produce a complete, structured data pipeline map for the current project and write it to `docs/data-pipeline-map.md`.

## Délégation multi-fournisseurs — RÈGLE N°1

Pour chaque script/notebook volumineux (>200 lignes) lu à l'étape 2, pré-digère via
`delegate mode context` (`python3 scripts/ai_broker.py --mode context -f <fichier>`)
avant de le lire en entier. Ne charge le fichier complet que si le résumé ne
suffit pas à identifier ses entrées, sorties et étapes.

## Scope: what this agent covers

**Document:**
- Pipeline scripts (`scripts/`, `etl/`, `pipelines/`, `dags/`, `jobs/` directories)
- Jupyter notebooks and notebook cells that read or write data files
- Makefiles, shell scripts, or Dockerfiles that orchestrate data processing
- Each stage's: input files, output files, algorithmic steps, external APIs, run instructions
- Manual steps that are documented but not scripted
- Known gaps, quirks, ordering dependencies

**Do NOT document:**
- The internal functions inside pipeline scripts (that is the job of `function-doc-generator`)
- The field-level schema of output data files (that is the job of `data-tables-doc-generator`)
- App code that reads and displays the processed data (that is not pipeline)

---

## How to gather the information

### Step 1 -- Find pipeline entry points

Read CLAUDE.md and any README first -- they often name the pipeline scripts explicitly.

Then Glob for likely pipeline files:
- Python scripts: `scripts/**/*.py`, `etl/**/*.py`, `pipelines/**/*.py`
- Notebooks: `notebooks/**/*.ipynb`, `notebooks/**/*.py`
- Orchestration: `Makefile`, `*.sh`, `Dockerfile`, `airflow/**/*.py`, `prefect/**/*.py`, `dbt/**/*.sql`

Exclude: `node_modules/`, `.venv/`, `__pycache__/`, `dist/`, `.next/`, test files (`*test*`, `*spec*`)

### Step 2 -- Read each pipeline file

For scripts: read the full file. Identify:
- What raw data files it opens/reads (file paths, formats, sizes if mentioned)
- What processing it applies (summarize the algorithm at a step level, not line by line)
- What output files it writes (paths, formats)
- External APIs or services it calls (HTTP requests, cloud SDK calls, geocoders, etc.)
- How it is invoked (command-line arguments, environment variables, working directory requirements)
- Whether it runs incrementally or processes everything from scratch

For notebooks: read the raw JSON to find code cells. Identify cells that do meaningful data work (skip cells that only display or print). For each relevant cell note the cell ID and what it does.

### Step 3 -- Identify pipeline stages and ordering

Group the work into named stages: Ingest / Download, Clean / Transform, Derive / Compute, Export / Publish. A single script may contain multiple stages; a single stage may span multiple scripts. Determine:
- Which stages must run before others (ordering dependencies)
- Which pipelines are independent of each other
- Which steps are automated vs require manual intervention

### Step 4 -- Write docs/data-pipeline-map.md

Create `docs/` if it does not exist. Write the file using the exact format below.

---

## Output format

```
# Data Pipeline Map: [Project Name]

> Generated: YYYY-MM-DD | Pipelines: N | Total stages: M

---

## Pipeline Overview

[One paragraph describing the overall data flow: where raw data comes from, what the major
transformations are, and what the final outputs enable. Plain English, no jargon.]

---

## Pipelines Summary

| Pipeline | Entry point | Final output | Trigger | Shared state with |
|----------|-------------|--------------|---------|-------------------|
| Pipeline A | scripts/foo.py | web/public/data/bar.json | manual | none |
| Pipeline B | notebooks/01.ipynb | data/processed/baz.csv | manual | none |

---

## [Pipeline A Name]

> One sentence on what this pipeline produces and why it exists.

### Stage 1: [Stage Name]

**Script/File:** `scripts/foo.py` (or `notebooks/01.ipynb` cell `abc123`)
**How to run:** `python scripts/foo.py` from project root (note any required working directory, venv, or env vars)
**Reads:**
- `data/raw/input_file.csv` -- description of what it contains, size if notable
**Writes:**
- `data/processed/output_file.json` -- description of what it contains
**Steps:**
1. [Step description] -- algorithmic, not function-by-function. E.g.: "Load NetCDF files and reproject longitude from 0-360 to -180-180."
2. ...
**External APIs:** [service name + what it does, or "none"]
**Notes:** [manual steps required, known gaps, ordering requirements, caveats]

---

### Stage 2: [Stage Name]

[same structure]

---

## [Pipeline B Name]

[same structure]

---

## Gaps and Manual Steps

[Bulleted list of anything that requires manual intervention, is not scripted, or is known to be broken/incomplete. If none, write "None identified."]
```

Rules:
- **Steps** is a numbered list of algorithmic actions -- describe what the code computes, not which function it calls
- **Reads** and **Writes** list actual file paths, not variable names
- **How to run** must be copy-pasteable (exact command, working directory, required env)
- Note if a script must be run from a specific directory
- If a stage has no external API calls, write "none" for that field
- Pipelines Summary table must match the Pipeline sections below exactly

---

## Quality checks before finishing

1. Every pipeline script and relevant notebook cell is represented in the map
2. File paths in Reads/Writes are actual paths from the repo, not invented ones
3. "How to run" commands are accurate to what the code does
4. Pipeline ordering/dependencies are correct (read both scripts to verify)
5. `docs/data-pipeline-map.md` is written. Report the final path and stage count to confirm.
