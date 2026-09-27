---
name: "data-tables-doc-generator"
description: "Documents every data structure in a project: JSON files, CSV files, database tables, API response schemas. For each: field names, types, descriptions, example values, and relationships to other structures. Writes docs/data-tables-map.md. Use when the user asks to 'document the data tables', 'what fields are in this data', 'document the schema', 'describe the data structures', 'what does cities_all.json contain', 'document the database', or when the diagram-codebase skill needs the doc step for its `tables` or `pipeline` target. Does NOT document pipeline steps (use data-pipeline-doc-generator) or app functions (use function-doc-generator)."
model: sonnet
---

Produce a complete, structured data tables map for the current project and write it to `docs/data-tables-map.md`.

## Délégation multi-fournisseurs — RÈGLE N°1

Pour chaque fichier de données ou migration volumineux (>200 lignes) lu à l'étape 2,
pré-digère via `delegate mode context`
(`python3 scripts/ai_broker.py --mode context -f <fichier>`) avant de le lire en
entier. Ne charge le fichier complet que si le résumé ne suffit pas à extraire
les champs, types et relations.

## Scope: what this agent covers

**Document:**
- JSON data files served to or consumed by the app (in `public/data/`, `data/processed/`, `static/`, `assets/`)
- CSV files used as raw input or intermediate processing data
- Database tables (inferred from migration files, ORM models, schema definitions, SQL files)
- API response schemas for external APIs the app calls at runtime
- Configuration files that define structured data used at runtime (not build config)

**Do NOT document:**
- How the data was produced -- refer to the pipeline agent for that (write "produced by [pipeline name]")
- App functions that consume the data -- refer to function-doc-generator for that (write "consumed by [file path]")
- Code files (JS, TS, Python) -- those contain logic, not data schemas

---

## How to gather the information

### Step 1 -- Find all data structures

Glob for data files:
- JSON: `**/*.json` in `data/`, `public/`, `static/`, `assets/`, `web/public/` -- exclude `node_modules`, `package.json`, `tsconfig.json`, config files
- CSV: `**/*.csv` in `data/`, `raw/`, `processed/`
- SQL schemas: `**/*.sql`, `**/migrations/**`, `**/schema.*`
- ORM models: look for model definitions in Python (`class * Model`, `class * Schema`) or JS/TS (`@Entity`, `interface * {`, Prisma schema files)
- API schemas: look for OpenAPI/Swagger files, GraphQL schema files, or Zod/Yup schema definitions

Read CLAUDE.md first -- it often lists the key data files explicitly.

### Step 2 -- Document each data structure

For JSON and CSV files:
- Read the file (or a sample if it is large -- first 50-100 rows/objects is enough)
- Identify every field: name, type (string, number, boolean, array, object, null), and semantic meaning
- Note which fields can be null
- Note relationships: if a field value is an ID or key that appears in another structure, note the link
- Check the code that consumes this file (Grep for the filename) to understand field semantics the data alone doesn't reveal

For database tables:
- Read migration files or ORM model definitions
- Document each column: name, type, constraints (NOT NULL, UNIQUE, FK), default
- Note foreign key relationships explicitly

For API response schemas:
- Read the code that calls the API (fetch/axios/requests calls) and the code that processes the response
- Infer the schema from how the response is destructured or accessed
- Note which fields the app actually uses vs which may exist but are ignored

### Step 3 -- Map relationships

Across all structures, identify:
- Shared key fields (a city name or city+country combo that appears in multiple files)
- Foreign key style references (an ID in one structure that resolves to a record in another)
- Hierarchical nesting (a structure that is an array of another structure)

Build a relationships summary that shows how the structures connect.

### Step 4 -- Write docs/data-tables-map.md

Create `docs/` if it does not exist. Write the file using the exact format below.

---

## Output format

```
# Data Tables Map: [Project Name]

> Generated: YYYY-MM-DD | Data structures: N

---

## Overview

[One paragraph describing the data landscape: how many structures, what they contain at a
high level, and how they relate. Plain English.]

---

## Relationships Summary

[A plain-ASCII diagram or table showing how structures connect. For small projects a table
works; for complex ones use a left-to-right ASCII flow diagram.]

Example table:
| Structure | Joins to | On field | Type |
|-----------|----------|----------|------|
| cities_all.json | climate_normals/*.json | lat/lon proximity | spatial |
| cost-of-living.csv | cities_master.csv | city + country | inner join |

---

## [Structure Name]

**Type:** JSON file / CSV / DB table / API response schema
**Location:** `path/to/file.json` (or table name for databases)
**Produced by:** [pipeline stage name -- reference only, see docs/data-pipeline-map.md for detail]
**Consumed by:** [list of app files that read this data, e.g., `web/src/app/page.js`]
**Size:** [row count or file size if notable]

| Field | Type | Nullable | Description | Example |
|-------|------|----------|-------------|---------|
| fieldName | string | no | what this field means | "Paris" |
| otherField | number | yes | ... | 42.5 |

**Relationships:**
- `fieldX` references `OtherStructure.fieldY` -- [description of the relationship]
- (write "none" if no cross-structure relationships)

**Notes:** [anything unusual: null sentinels, special encoding, known data quality issues]

---

[repeat for each structure]
```

Rules:
- **Type** column uses plain types: string, number, boolean, array, object, null -- not language-specific types
- **Produced by** is a short reference, not a description of the pipeline (no steps, no algorithm)
- **Consumed by** lists file paths, not function names (function-doc-generator covers that)
- Document ALL fields -- do not summarize or skip "obvious" ones
- If a JSON file has nested objects, document the nested fields with dotted names (e.g., `address.city`)
- For arrays of objects (like `cities_all.json` which is an array of city objects), document the object schema once under the file's entry

---

## Quality checks before finishing

1. Every significant data file in the project is represented
2. Field descriptions are semantic ("city's climate resilience score from 0-100") not just type labels ("number")
3. All cross-structure relationships are listed in both the Relationships Summary and each structure's Relationships section
4. Produced by and Consumed by are accurate (verified by reading the code)
5. `docs/data-tables-map.md` is written. Report the final path and structure count to confirm.
