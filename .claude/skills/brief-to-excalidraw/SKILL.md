---
name: brief-to-excalidraw
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
python3 "$SKILL_DIR/scripts/brief_to_excalidraw.py" <brief.md> <output.excalidraw>
```

Replace `$SKILL_DIR` with this skill's directory. If the brief is at
`/mnt/user-data/uploads/foo.md`, write the output to
`/mnt/user-data/outputs/foo.excalidraw`, then present it with `present_files`.

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
  text and any `Mark X as entry point/external` flags) is rendered as one
  standalone text box — never inside the box. It is placed beside the box when a
  corridor next to it is free, and underneath when it is not.
- **Arrows.** Each `Draw an arrow from A to B` becomes an arrow **bound** to both
  boxes (via `startBinding`/`endBinding` plus back-references in each box's
  `boundElements`), so the arrows keep following the boxes when the user drags
  them around. `Label it "..."` becomes a label bound to the arrow.

Everything that isn't group color or arrow connectivity (background, stroke
width, roughness, fonts, etc.) is fixed and identical for every diagram.

## Layout expectations

Groups are stacked top-to-bottom as bands, but the script does real placement
work inside them: boxes are ordered and positioned so that connected boxes sit
above one another, empty corridors are squeezed out, notes move beside their box
when there is room, and an arrow only detours when a straight line would cross a
box or drop its label on a note. Measured over four briefs of different shapes
(13 to 49 boxes), notes no longer overlap at all and arrow crossings inside the
drawing fall by up to two thirds.

Do **not** post-process the coordinates or hand-write a layout of your own. If a
drawing still reads badly, fix it in the script so every brief benefits — the
placement passes are documented at the top of `scripts/brief_to_excalidraw.py`.

One limit is worth knowing: Excalidraw pins an arrow's label to the middle of the
arrow's path and recomputes it on load, so a label cannot be positioned
independently of its arrow. On a dense diagram a few labels still land on a note.
The user rearranges those by hand; unbinding the labels would fix it but would
stop them following the arrows, which is worse.

## Brief format the script understands

Directives live under a `## Elements` heading (one per blank-line-separated
paragraph). Recognized forms:

- `Create a group labeled "NAME" that contains: A, B, C.`
- `Create a box labeled "NAME". Note: free text (may contain periods, commas, em-dashes).`
- `Create a box labeled "NAME".`  (note optional)
- `Draw an arrow from A to B. Label it "LABEL".`  (label optional; A/B are bare box names, may contain dots like `page.js`)
- `Mark NAME as entry point.` / `Mark NAME as external.`
- `Add a note to NAME: "extra text"`

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
