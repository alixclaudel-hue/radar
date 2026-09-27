---
name: diagram-render
description: Convert a structured diagram brief (a Markdown file describing groups, boxes, notes, and arrows) into an Excalidraw (.excalidraw) file. Use this skill whenever the user wants to turn a diagram brief, architecture brief, "diagram spec", or a Markdown description of boxes-and-arrows into an Excalidraw / excalidraw.com file — including phrasings like "make this brief into an excalidraw", "generate the excalidraw from this spec", "turn this diagram description into a .excalidraw", or when a file containing "Create a box labeled", "Create a group labeled", or "Draw an arrow from" directives is provided. Each group becomes a distinct color, each box becomes a colored rectangle with its note in a separate text box, and arrows are bound to the boxes they connect.
---

# Brief → Excalidraw

Turn a structured diagram brief into an `.excalidraw` file **fast** (sub-second) by
running the bundled generator script. Do **not** hand-write the Excalidraw JSON —
it is large, repetitive, and slow. The script does the entire transform
deterministically.

## The one step that matters

Run the script. Pass the brief path and an output path:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/brief_to_excalidraw.py" <brief.md> <output.excalidraw>
```

`${CLAUDE_SKILL_DIR}` is this skill's own folder; Claude Code fills it in when it
loads the skill. If it shows up literally (another tool, an older Claude Code),
use the folder that contains this SKILL.md.

On Windows, run `python` instead of `python3`: there `python3` opens the
Microsoft Store and runs nothing. The script needs Python 3.8+ and nothing else
(standard library only).

Write the output next to the brief unless the user asks for another place.

That's it. The script prints a one-line summary (`groups / boxes / arrows`) and a
WARNING if any arrow references a box that was never defined — relay that warning
to the user if it appears, but still deliver the file.

## What the script produces

- **Groups → colors.** Each `Create a group labeled "X" that contains: ...`
  assigns one stroke color to every box it lists. Groups are conveyed by color
  only (no Excalidraw grouping), cycling through a 10-color palette in group
  order (red, blue, green, orange, …). Boxes in no group are dark gray.
- **Boxes.** Each `Create a box labeled "X"` becomes a rounded rectangle with the
  label bound inside it, in its group's color.
- **Notes in a separate text box.** The `Note:` text (plus any `Add a note to X`
  text, any `Mark X as ...` flags and any `Add columns to X` list) is rendered as
  one standalone text box — never inside the box.
- **Arrows.** Each `Draw an arrow from A to B` becomes an arrow **bound** to both
  boxes (via `startBinding`/`endBinding` plus back-references in each box's
  `boundElements`), so the arrows keep following the boxes when the user drags
  them around. `Label it "..."` becomes a label bound to the arrow.

Everything that isn't group color or arrow connectivity (background, stroke
width, roughness, fonts, etc.) is fixed and identical for every diagram.

## Layout expectations

The script reads the brief's `Layout:` line and picks one of two layouts.

**Layered** — `left-to-right`, `top-to-bottom`, `grouped`, or no `Layout:` line.
Every box gets a column (or row) from the flow itself: what feeds what sits next
to it. Groups are colours only, so one group can span several columns — a
script and the file it writes end up side by side even when they share a
colour. An arrow that skips columns gets invisible waypoints and runs between
the boxes, straightened when the space allows, instead of going round the whole
drawing. Box order in each column minimises crossings; a box with many arrows on
one side is drawn taller so they arrive at distinct points. A legend of the
group colours sits at the top. Curved arrows are kept.

In left-to-right, notes sit under their box. In top-to-bottom, beside it, and
arrows drop through the empty space under the box before turning, so they never
cut a note.

Measured on three real briefs (pipeline 22 boxes, schema 15, architecture 15):
arrows through a box 0 in all three (from 2, 8 and 4), arrows through a note
0 in all three (from 14, 12 and 2), arrow crossings 0, 1 and 1 (from 20, 13
and 11), total arrow length divided by 2 to 3.7.

Curves: Excalidraw rounds an arrow with a spline through its points, and a
spline overshoots wherever neighbouring points are unevenly spaced -- the arrow
swings past a bend, or hooks backwards at the end of a long straight run. The
script therefore computes each bend itself (a cubic that leaves and arrives
parallel to the flow, sampled densely) and spaces the points of long runs by
doubling steps from each end. Measured on the same briefs: no arrow overshoots
sideways and none steps backwards.

**Bands** — only when the `Layout:` line says `swim lane` (or `lane`, `band`).
One row per group, in brief order, for diagrams where a group really is a lane.

`--layout=lr`, `--layout=tb` or `--layout=bands` on the command line overrides
the brief. The summary line reports which layout ran.

Everything stays bound: boxes can be dragged in Excalidraw afterwards and the
arrows follow.

Do **not** post-process the coordinates or hand-write a layout of your own. If a
drawing still reads badly, fix it in the script so every brief benefits — the
passes are documented at the top of the layered section of
`scripts/brief_to_excalidraw.py`.

One limit is worth knowing: Excalidraw pins an arrow's label to the middle of the
arrow's path and recomputes it on load, so a label cannot be positioned
independently of its arrow. On a dense diagram a few labels still sit close to
each other. Unbinding the labels would fix it but would stop them following the
arrows, which is worse.

## Brief format the script understands

Directives live under a `## Elements` heading (one per blank-line-separated
paragraph). Recognized forms:

- `Create a group labeled "NAME" that contains: A, B, C.`
- `Create a box labeled "NAME". Note: free text (may contain periods, commas, em-dashes).`
- `Create a box labeled "NAME".`  (note optional)
- `Draw an arrow from A to B. Label it "LABEL".`  (label optional; A/B are bare box names, may contain dots like `page.js`)
- `Mark NAME as entry point.` / `Mark NAME as external.` / `Mark NAME as database.`
  — any word works and is shown as `▸ word` at the top of the note
- `Add a note to NAME: "extra text"`
- `Add columns to NAME: field type, field type, field type.` — the fields of a
  table or file, shown one per line under the note after a `▸ columns` line.
  Commas inside parentheses do not split (`counts object (a, b)`). A leading
  `+` marks a field created by the step that writes this file (lineage).
- `**Layout**: left-to-right | top-to-bottom | grouped | swim lane` — anywhere in
  the brief, usually right under the title

Long tokens (file paths, snake_case names) are cut at `/`, `_`, `-` or `.` when
they are longer than a note line, instead of running into the next column.

Box names are matched exactly by their label string. A `## Notes` section after
the elements is treated as prose and ignored.

## If the brief doesn't match this format

The script is the fast path for the directive format above. If a user's brief is
free-form prose that doesn't use these directives, first rewrite it into the
directive format (it's quick), save that as a `.md`, then run the script on it.
Don't abandon the script for hand-written JSON.

## Tweaking output

To change the palette, box size, fonts, or spacing, edit the constants block at
the top of `scripts/brief_to_excalidraw.py` (e.g. `GROUP_COLORS`, `BOX_W`,
`BOX_H`, `H_GAP`, `BAND_GAP`). Only colors and connectivity are meaningful to the
final diagram; the rest are cosmetic defaults.
