---
name: diagram-brief
description: Generate a diagram brief — a plain-text .md file with step-by-step drawing instructions — from existing documentation. Use when you want to produce Excalidraw diagrams without generating JSON. The output is a human-readable instruction file that tells someone exactly what to draw and in what order. Triggers on: "generate diagram brief", "diagram instructions from docs", "create drawing instructions", "diagram brief".
---

# Diagram Brief Skill

Read existing documentation and produce a `.md` file containing ordered, plain-English drawing instructions. No JSON. No rendering. One pass.

---

## Workflow

1. **Read the doc** — if not already in context, read it with `view`.
2. **Identify what matters** — extract the elements worth drawing (see categories below). Ignore implementation details that don't affect the visual structure.
3. **Decide the layout** — pick a flow direction and grouping before writing any instructions (see Layout section below).
4. **Write the brief** — produce ordered instructions following the format below.
5. **Write the file** — save as `<doc-name>-diagram-brief.md` next to the source doc, or at the path requested.

---

## What to Extract from Documentation

Draw things that have **relationships** or **boundaries**. Skip things that are purely internal detail.

| Doc element | Draw as |
|---|---|
| Module, package, layer | Group (enclosing frame) |
| Class, service, component | Box |
| Function, method, endpoint | Box (smaller, inside its parent) |
| Database, file, queue, external API | Box (with a note on its type) |
| Call, import, dependency, trigger | Arrow |
| Data flowing between elements | Arrow with a label |
| Shared state, config, constants | Box (referenced by multiple arrows) |

**Limit**: aim for 8–20 elements. If the doc is large, focus on the top-level structure. Nest details inside groups rather than flattening everything.

---

## Layout Decision (do this mentally before writing)

Pick one:

- **Left → right**: use for pipelines, request flows, data transformations.
- **Top → bottom**: use for call stacks, trigger chains, hierarchies.
- **Grouped / nested**: use for module-based architectures where grouping is more important than flow.

State the chosen layout at the top of the brief.

---

## Output Format

```
# Diagram Brief: [Doc or Feature Name]

**Layout**: [left-to-right | top-to-bottom | grouped]
**Flow summary**: [One sentence — what this diagram shows]

---

## Elements

[Ordered drawing instructions — one per line, with a blank line between each instruction]

---

## Notes
[Optional — ambiguities, things to confirm, alternative representations]
```

**Formatting rule**: Every instruction in the Elements section must be followed by a blank line. No two instructions may appear on consecutive lines without a blank line between them. This makes the brief scannable and easy to follow step by step.

---

## Instruction Types

Use exactly these sentence patterns. Be consistent.

**Box**
```
Create a box labeled "[Name]". [Optional: note its type — function, service, database, etc.]
```

**Group** (enclosing frame around multiple boxes)
```
Create a group labeled "[Name]" that contains: [Box A], [Box B], [Box C].
```

**Arrow**
```
Draw an arrow from [A] to [B]. [Optional: label it "[action or data name]".]
```

**Note** (annotation on an element)
```
Add a note to [Element]: "[short description]".
```

**Styling hint** (optional, only when meaningful)
```
Mark [Element] as [entry point | external | database | async].
```

---

## Ordering Rules

Write instructions in this order:
1. Groups first (outermost to innermost).
2. Boxes inside each group, in the order they appear in the flow.
3. Arrows after all boxes, in flow order (source → destination).
4. Notes and styling hints last.

This order means the diagram can be built top-to-bottom without needing to jump back.

---

## Example Output (short)

```
# Diagram Brief: Auth Service

**Layout**: left-to-right
**Flow summary**: Shows how a login request flows from the API route through validation, token generation, and the user database.

---

## Elements

Create a group labeled "Auth Module" that contains: Route Handler, Validator, Token Service.

Create a box labeled "Route Handler". Note: entry point, receives POST /login.

Create a box labeled "Validator". Note: checks email format and password length.

Create a box labeled "Token Service". Note: generates JWT, sets expiry.

Create a box labeled "User DB". Note: external PostgreSQL database.

Create a box labeled "Redis Cache". Note: stores active sessions.

Draw an arrow from Route Handler to Validator. Label it "raw credentials".

Draw an arrow from Validator to Token Service. Label it "validated payload".

Draw an arrow from Token Service to User DB. Label it "lookup user".

Draw an arrow from Token Service to Redis Cache. Label it "store session".

Draw an arrow from Token Service to Route Handler. Label it "JWT response".

Mark User DB as external.

Mark Redis Cache as external.

Mark Route Handler as entry point.

---

## Notes
- Validator also calls a rate-limiter (not shown — add if detail is needed).
- Token expiry logic lives inside Token Service but is not a separate element.
```

---

## Quality Check (before writing the file)

- Every arrow references two elements that exist as boxes or groups.
- No element is mentioned in an arrow without first appearing as a box or group.
- Groups are declared before their contents.
- The flow summary matches the actual arrow directions.
- Element count is between 8 and 20. If over 20, consolidate into groups.