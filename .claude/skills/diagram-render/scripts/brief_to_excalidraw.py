#!/usr/bin/env python3
"""
brief_to_excalidraw.py

Transform a structured diagram brief (Markdown) into an Excalidraw file.

Design goals
------------
* Deterministic and instant (pure parsing + JSON emit, no model calls).
* Each GROUP gets its own stroke color (groups are conveyed by color, not by
  Excalidraw's groupId mechanism).
* Each BOX is a rectangle with a bound label, plus a SEPARATE text box for its
  note(s), placed beside the box when there is room and underneath when there
  is not.
* Each ARROW is bound to its source and target boxes (startBinding / endBinding
  + back-references in each box's boundElements) so the arrows keep following
  the boxes when the user drags them around in Excalidraw.

Layout
------
Two layouts, chosen by the brief's `Layout:` line (or --layout=lr|tb|bands).

Layered (left-to-right, top-to-bottom, grouped, or no Layout line) -- every box
gets a column from the flow itself, groups are colours only, arrows that skip
columns run between the boxes through invisible waypoints. See the "Layered
layout" section below for the five passes.

Bands (swim lane) -- groups stacked top-to-bottom as rows, in brief order.
Within a band the boxes are ordered and positioned so that connected boxes end
up above one another:

1. `order_within_bands` -- barycentre ordering, to decide left-to-right order.
2. `assign_x`           -- median coordinate assignment, to align what connects.
3. `compact_x`          -- squeeze the empty corridors that pass leaves behind.
4. `choose_note_sides`  -- put notes beside their box when a corridor is free.
5. routing              -- straight lines, and detours only where a line would
                           actually cross a box or drop its label on a note.

In both, every arrow stays bound to its boxes: the drawing can be rearranged
by hand in Excalidraw afterwards and the arrows follow.

Known limit: Excalidraw pins an arrow's label to the middle of the arrow's path
and recomputes that position on load, so the label cannot be placed
independently. On a dense diagram some labels still land on a note. The
alternative -- unbinding the labels -- would place them freely but stop them
following the arrow when the user drags a box, which is worse for a drawing
meant to be rearranged by hand.

Usage
-----
    python3 brief_to_excalidraw.py <brief.md> [output.excalidraw] [--layout=lr|tb|bands]

If output is omitted, writes "<brief-stem>.excalidraw" next to the brief.
"""

import collections
import math
import json
import random
import re
import string
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Constants you can tweak globally (everything that is NOT group-color or
# arrow-connection is fixed here so it is identical for every diagram).
# --------------------------------------------------------------------------

# Excalidraw stroke palette, extended to 16 well-separated hues so that briefs
# with many groups still get a distinct color per group. The first 13 are spread
# across the wheel (no near-duplicate hues among them); the rest are spares for
# very group-heavy briefs. Groups are assigned in order, cycling if exhausted.
GROUP_COLORS = [
    "#e03131",  # red
    "#1971c2",  # blue
    "#2f9e44",  # green
    "#f08c00",  # orange
    "#9c36b5",  # grape
    "#0c8599",  # teal
    "#c2255c",  # pink
    "#5f3dc4",  # indigo
    "#66a80f",  # lime
    "#e8590c",  # pumpkin
    "#ae3ec9",  # magenta
    "#3b5bdb",  # royal blue
    "#495057",  # gray
    "#1098ad",  # cyan
    "#d6336c",  # raspberry
    "#087f5b",  # deep green
]
DEFAULT_COLOR = "#1e1e1e"   # boxes that belong to no group
ARROW_COLOR = "#1e1e1e"

# Fonts: 5 = Excalifont (hand-drawn), used everywhere for a consistent look.
FONT_FAMILY = 5

BOX_W = 300
BOX_H = 70
BOX_LABEL_FONT = 20
NOTE_FONT = 16
GROUP_HEADER_FONT = 28
ARROW_LABEL_FONT = 16

CHAR_W = NOTE_FONT * 0.55     # rough glyph width for note width estimate
LINE_H = 1.25                 # Excalidraw default lineHeight
# A note is drawn left-aligned under its own box. Wrapping it wider than the box
# makes it run under the neighbouring column, which is the single most common
# way these diagrams become unreadable. Derive the wrap from the box width so
# the two can never drift apart.
NOTE_WRAP_CHARS = int((BOX_W - 8) / CHAR_W)

H_GAP = 90                    # horizontal gap between boxes in a band
# An arrow's label is centred on the path, so a gutter too close to the drawing
# lets a long label reach back over the first column. Keep it clear.
GUTTER_GAP = 170              # distance from the drawing to the first gutter
GUTTER_LANE = 60              # distance between two parallel gutters
NOTE_GAP = 12                 # gap between a box and its note
BAND_GAP = 140                # vertical gap between group bands
HEADER_GAP = 16               # gap below a group header before its boxes
MARGIN = 80                   # top-left margin

FIXED_TS = 1700000000000      # any stable timestamp


def new_id():
    return "".join(random.choices(string.ascii_letters + string.digits + "_-", k=21))


def rnd():
    return random.randint(1, 2_000_000_000)


# --------------------------------------------------------------------------
# Brief parsing
# --------------------------------------------------------------------------

def _strip_period(s):
    return s.strip().rstrip(".").strip()


def parse_brief(text):
    """Return (groups, boxes, arrows).

    groups: list of (name, [member_label, ...])  -- order preserved
    boxes:  dict label -> {"notes": [str, ...], "marks": set()}  -- order via box_order
    arrows: list of (src_label, dst_label, label_or_None)
    """
    # Isolate the "## Elements" section (directives) if present, else use all.
    m = re.search(r"##\s*Elements\b(.*?)(?:\n##\s|\Z)", text, re.S | re.I)
    body = m.group(1) if m else text

    # Split into paragraphs (blank-line separated blocks).
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]

    groups = []
    box_order = []
    boxes = {}
    arrows = []

    def ensure_box(label):
        if label not in boxes:
            boxes[label] = {"notes": [], "marks": set(), "columns": []}
            box_order.append(label)

    re_group = re.compile(
        r'^Create a group labeled\s+"(.+?)"\s+that contains:\s*(.+)$', re.I | re.S)
    re_box = re.compile(
        r'^Create a box labeled\s+"(.+?)"\s*\.\s*(?:Note:\s*(.+))?$', re.I | re.S)
    re_arrow = re.compile(r'^Draw an arrow from\s+(.+)$', re.I | re.S)
    re_mark = re.compile(
        r'^Mark\s+(.+?)\s+as\s+(?:an?\s+)?(.+?)\s*\.?$', re.I | re.S)
    re_columns = re.compile(
        r'^Add columns to\s+(.+?):\s*(.+?)\s*\.?$', re.I | re.S)
    re_addnote = re.compile(
        r'^Add a note to\s+(.+?):\s*"(.*)"\s*\.?$', re.I | re.S)
    re_label = re.compile(r'Label it\s+"(.*?)"\s*\.?\s*$', re.I | re.S)

    for p in paragraphs:
        first_line = p.lstrip()

        mg = re_group.match(first_line)
        if mg:
            name = mg.group(1).strip()
            members = [_strip_period(x) for x in mg.group(2).split(",")]
            members = [m for m in members if m]
            groups.append((name, members))
            continue

        mb = re_box.match(first_line)
        if mb:
            label = mb.group(1).strip()
            ensure_box(label)
            note = mb.group(2)
            if note:
                boxes[label]["notes"].append(note.strip().rstrip("."))
            continue

        mm = re_mark.match(first_line)
        if mm:
            label = mm.group(1).strip()
            ensure_box(label)
            boxes[label]["marks"].add(mm.group(2).lower())
            continue

        mc = re_columns.match(first_line)
        if mc:
            label = mc.group(1).strip()
            ensure_box(label)
            boxes[label]["columns"] += split_columns(mc.group(2))
            continue

        man = re_addnote.match(first_line)
        if man:
            label = man.group(1).strip()
            ensure_box(label)
            boxes[label]["notes"].append(man.group(2).strip())
            continue

        ma = re_arrow.match(first_line)
        if ma:
            rest = ma.group(1).strip()
            label = None
            ml = re_label.search(rest)
            if ml:
                label = ml.group(1).strip()
                rest = rest[:ml.start()].strip()
            rest = _strip_period(rest)
            src, dst = _split_arrow_endpoints(rest, set(boxes.keys()))
            if src and dst:
                ensure_box(src)
                ensure_box(dst)
                arrows.append((src, dst, label))
            continue
        # Unrecognized paragraph -> ignore silently (e.g. prose).

    return groups, box_order, boxes, arrows


def _split_arrow_endpoints(rest, known):
    """Split 'A to B' into (A, B). Box names may contain '.' (page.js) so we
    split on ' to ' and prefer the split where both sides are known boxes."""
    positions = [m.start() for m in re.finditer(r"\s+to\s+", rest)]
    best = None
    for pos in positions:
        left = rest[:pos].strip()
        right = re.sub(r"^\s+to\s+", "", rest[pos:]).strip()
        if left in known and right in known:
            return left, right
        if best is None:
            best = (left, right)
    if best:
        return best
    return None, None


# --------------------------------------------------------------------------
# Text wrapping / sizing helpers
# --------------------------------------------------------------------------

def _split_long(word, width):
    """Cut a token longer than a line: after '/', '_', '-' or '.', else hard."""
    out, cur = [], ""
    for piece in re.split(r"(?<=[/_\-.])", word):
        while len(piece) > width:
            if cur:
                out.append(cur)
                cur = ""
            out.append(piece[:width])
            piece = piece[width:]
        if cur and len(cur) + len(piece) > width:
            out.append(cur)
            cur = piece
        else:
            cur += piece
    if cur:
        out.append(cur)
    return out


def wrap_text(s, width=NOTE_WRAP_CHARS):
    """Greedy word-wrap. Preserves explicit newlines in the source, and cuts
    tokens longer than a line (paths, file names) instead of letting them run
    past the note into the next column."""
    out_lines = []
    for raw in s.split("\n"):
        words = []
        for w in raw.split(" "):
            words += _split_long(w, width) if len(w) > width else [w]
        line = ""
        for w in words:
            if not line:
                line = w
            elif len(line) + 1 + len(w) <= width:
                line += " " + w
            else:
                out_lines.append(line)
                line = w
        out_lines.append(line)
    return "\n".join(out_lines)


def text_size(s, font):
    lines = s.split("\n")
    w = max((len(l) for l in lines), default=1) * font * 0.55
    h = len(lines) * font * LINE_H
    return max(w, 10), max(h, font)


# --------------------------------------------------------------------------
# Element factories
# --------------------------------------------------------------------------

def base_props(eid, x, y, w, h, color, opacity=100):
    return {
        "id": eid,
        "x": x, "y": y, "width": w, "height": h,
        "angle": 0,
        "strokeColor": color,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": opacity,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": rnd(),
        "version": 1,
        "versionNonce": rnd(),
        "isDeleted": False,
        "boundElements": None,
        "updated": FIXED_TS,
        "link": None,
        "locked": False,
    }


def make_rectangle(eid, x, y, w, h, color, text_id):
    el = base_props(eid, x, y, w, h, color)
    el["type"] = "rectangle"
    el["roundness"] = {"type": 3}
    el["boundElements"] = [{"type": "text", "id": text_id}]
    return el


def make_container_text(eid, container_id, x, y, w, h, color, text):
    el = base_props(eid, x, y, w, h, color)
    el["type"] = "text"
    el.update({
        "text": text,
        "fontSize": BOX_LABEL_FONT,
        "fontFamily": FONT_FAMILY,
        "textAlign": "center",
        "verticalAlign": "middle",
        "containerId": container_id,
        "originalText": text,
        "autoResize": True,
        "lineHeight": LINE_H,
    })
    return el


def make_free_text(eid, x, y, color, text, font, opacity=100, align="left"):
    w, h = text_size(text, font)
    el = base_props(eid, x, y, w, h, color, opacity=opacity)
    el["type"] = "text"
    el.update({
        "text": text,
        "fontSize": font,
        "fontFamily": FONT_FAMILY,
        "textAlign": align,
        "verticalAlign": "top",
        "containerId": None,
        "originalText": text,
        "autoResize": True,
        "lineHeight": LINE_H,
    })
    return el


def make_arrow(eid, ax, ay, points, src_id, dst_id, label_text_id=None):
    # Bounds over every point, not just the last one: an elbowed arrow reaches
    # further than its endpoint.
    w = max(px for px, _ in points) - min(px for px, _ in points)
    h = max(py for _, py in points) - min(py for _, py in points)
    el = base_props(eid, ax, ay, w, h, ARROW_COLOR, opacity=80)
    el["type"] = "arrow"
    el["roundness"] = {"type": 2}
    el["points"] = points
    el["startBinding"] = {"elementId": src_id, "focus": 0, "gap": 4}
    el["endBinding"] = {"elementId": dst_id, "focus": 0, "gap": 4}
    el["startArrowhead"] = None
    el["endArrowhead"] = "arrow"
    el["elbowed"] = False
    if label_text_id:
        el["boundElements"] = [{"type": "text", "id": label_text_id}]
    return el


def make_arrow_label(eid, arrow_id, cx, cy, text):
    w, h = text_size(text, ARROW_LABEL_FONT)
    el = base_props(eid, cx - w / 2, cy - h / 2, w, h, ARROW_COLOR, opacity=90)
    el["type"] = "text"
    el.update({
        "text": text,
        "fontSize": ARROW_LABEL_FONT,
        "fontFamily": FONT_FAMILY,
        "textAlign": "center",
        "verticalAlign": "middle",
        "containerId": arrow_id,
        "originalText": text,
        "autoResize": True,
        "lineHeight": LINE_H,
    })
    return el


# --------------------------------------------------------------------------
# Geometry: clip a center->center segment to the box borders
# --------------------------------------------------------------------------

def box_center(b):
    return (b["x"] + b["w"] / 2, b["y"] + b["h"] / 2)


def side_point(box, side):
    """Middle of the left or right border of `box`."""
    y = box["y"] + box["h"] / 2
    return (box["x"], y) if side == "left" else (box["x"] + box["w"], y)


def edge_point(box, toward):
    """Point on `box` border along the line from box center toward `toward`."""
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    tx, ty = toward
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
    hw, hh = box["w"] / 2, box["h"] / 2
    # scale to reach the nearest border
    sx = hw / abs(dx) if dx != 0 else float("inf")
    sy = hh / abs(dy) if dy != 0 else float("inf")
    s = min(sx, sy)
    return cx + dx * s, cy + dy * s


# --------------------------------------------------------------------------
# Main build
# --------------------------------------------------------------------------

def order_within_bands(bands, arrows, sweeps=6):
    """Reorder each band so connected boxes sit above one another.

    Barycentre heuristic: a box moves toward the average column of the boxes it
    is connected to in the other bands, repeated a few times until it settles.
    Declaration order breaks ties, so a brief that already reads in flow order
    keeps its shape; only boxes that would force a long diagonal move.
    """
    column = {}
    for bi, members in enumerate(bands):
        for ci, m in enumerate(members):
            column[m] = (bi, ci)

    neighbours = collections.defaultdict(list)
    for src, dst, _ in arrows:
        if src in column and dst in column:
            neighbours[src].append(dst)
            neighbours[dst].append(src)

    for _ in range(sweeps):
        moved = False
        for bi, members in enumerate(bands):
            def barycentre(m, bi=bi):
                cols = [column[n][1] for n in neighbours[m]
                        if n in column and column[n][0] != bi]
                return sum(cols) / len(cols) if cols else column[m][1]
            reordered = sorted(members, key=barycentre)   # stable: ties keep order
            if reordered != members:
                bands[bi] = reordered
                moved = True
            for ci, m in enumerate(bands[bi]):
                column[m] = (bi, ci)
        if not moved:
            break
    return bands


def split_columns(s):
    """'a string, b number, c object (k, v)' -> ['a string', 'b number', ...].
    Commas inside brackets or parentheses do not split."""
    out, depth, cur = [], 0, ""
    for ch in s.replace("\n", " "):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            if cur.strip():
                out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def note_block(info):
    """The note text that belongs beside a box, or None."""
    parts = []
    for mark in sorted(info["marks"]):
        parts.append("▸ " + ("ENTRY POINT" if mark == "entry point" else mark))
    for n in info["notes"]:
        parts.append(wrap_text(n))
    cols = info.get("columns") or []
    if cols:
        # One field per line, so the schema reads as a list, not a paragraph.
        # A leading "+" marks a column this step creates (lineage), kept as is.
        lines = ["", "▸ columns"]
        for c in cols:
            lines.append(wrap_text(c, NOTE_WRAP_CHARS - 2).replace("\n", "\n  "))
        parts.append("\n".join(lines))
    return "\n".join(parts) if parts else None


def assign_x(bands, arrows, rounds=8):
    """Give every box an x so connected boxes end up above one another.

    Evenly spaced columns look tidy but make every arrow a diagonal. Each pass
    here pulls a box toward the *median* x of what it connects to in other
    bands, then two sweeps restore the minimum gap. Median rather than average:
    a single distant neighbour should not drag a box across the drawing.

    Nothing about the project's shape is assumed -- a band of one box and a band
    of ten both work, and a box with no connections simply keeps its column.
    """
    step = BOX_W + H_GAP
    x = {}
    for band in bands:
        for ci, m in enumerate(band):
            x[m] = MARGIN + ci * step
    if not x:
        return x

    band_index = {m: bi for bi, band in enumerate(bands) for m in band}
    neighbours = collections.defaultdict(list)
    for src, dst, _ in arrows:
        if (src in band_index and dst in band_index
                and band_index[src] != band_index[dst]):
            neighbours[src].append(dst)
            neighbours[dst].append(src)

    def separate(band):
        for i in range(1, len(band)):
            x[band[i]] = max(x[band[i]], x[band[i - 1]] + step)
        for i in range(len(band) - 2, -1, -1):
            x[band[i]] = min(x[band[i]], x[band[i + 1]] - step)

    for _ in range(rounds):
        for band in list(bands) + list(reversed(bands)):
            for m in band:
                vecinos = sorted(x[n] for n in neighbours[m])
                if vecinos:
                    mid = len(vecinos) // 2
                    x[m] = (vecinos[mid] if len(vecinos) % 2
                            else (vecinos[mid - 1] + vecinos[mid]) / 2)
            separate(band)

    shift = MARGIN - min(x.values())
    return {m: v + shift for m, v in x.items()}


def compact_x(bands, xs):
    """Squeeze the empty vertical corridors the alignment pass leaves behind.

    Pulling boxes toward their neighbours can spread a band far wider than its
    contents need -- on one test project the canvas tripled in width. Here we
    find the x ranges no box occupies at any height and shrink the wide ones,
    shifting everything to their right by the same amount. Because every band
    shifts identically, the vertical alignment just won earlier survives.

    A corridor is left wide enough for one note, so notes can still sit beside
    their box instead of stacking underneath it.
    """
    if not xs:
        return xs
    max_corridor = BOX_W + 2 * NOTE_GAP
    ocupado = sorted((xs[m], xs[m] + BOX_W) for band in bands for m in band)

    # Merge overlapping box footprints into solid spans.
    spans = []
    for ini, fin in ocupado:
        if spans and ini <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], fin)
        else:
            spans.append([ini, fin])

    # Walk the gaps between spans and accumulate how much to shift.
    desplazamiento = {}
    acumulado = 0.0
    for i, (ini, fin) in enumerate(spans):
        desplazamiento[ini] = acumulado
        if i + 1 < len(spans):
            hueco = spans[i + 1][0] - fin
            if hueco > max_corridor:
                acumulado -= hueco - max_corridor

    def shift_for(x):
        aplicable = [d for inicio, d in desplazamiento.items() if inicio <= x]
        return aplicable[-1] if aplicable else 0.0

    movido = {m: xs[m] + shift_for(xs[m]) for band in bands for m in band}
    ajuste = MARGIN - min(movido.values())
    return {m: v + ajuste for m, v in movido.items()}


def choose_note_sides(bands, xs, boxes):
    """Put each note beside its box when there is room, below when there isn't.

    Notes stacked below every box are what makes a band tall, and a tall band is
    what makes the arrows leaving it long. Moving a note into the empty corridor
    next to its box costs nothing and shortens everything downstream. Each gap
    is claimed by at most one note.
    """
    side = {}
    for band in bands:
        claimed = set()
        for i, m in enumerate(band):
            text = note_block(boxes[m])
            if not text:
                continue
            need = text_size(text, NOTE_FONT)[0] + NOTE_GAP
            hueco_dcha = (xs[band[i + 1]] - (xs[m] + BOX_W)
                          if i + 1 < len(band) else float("inf"))
            hueco_izda = (xs[m] - (xs[band[i - 1]] + BOX_W)
                          if i > 0 else float("inf"))
            if i not in claimed and hueco_dcha >= need:
                side[m], _ = "right", claimed.add(i)
            elif (i - 1) not in claimed and hueco_izda >= need:
                side[m], _ = "left", claimed.add(i - 1)
            else:
                side[m] = "below"
    return side


def segment_hits(p, q, box, inset=6, samples=64):
    """Does the straight segment p->q pass through `box`?

    Sampled rather than solved: a few dozen points is exact enough to decide
    whether an arrow needs to go around, and it stays readable.
    """
    x0, y0 = box["x"] + inset, box["y"] + inset
    x1, y1 = box["x"] + box["w"] - inset, box["y"] + box["h"] - inset
    for k in range(1, samples):
        t = k / samples
        px = p[0] + (q[0] - p[0]) * t
        py = p[1] + (q[1] - p[1]) * t
        if x0 <= px <= x1 and y0 <= py <= y1:
            return True
    return False


def rects_overlap(a, b, margin=4):
    return not (a["x"] + a["w"] - margin <= b["x"]
                or b["x"] + b["w"] - margin <= a["x"]
                or a["y"] + a["h"] - margin <= b["y"]
                or b["y"] + b["h"] - margin <= a["y"])


def build(groups, box_order, boxes, arrows):
    # Map each box to its group color.
    box_color = {}
    group_of = {}
    for gi, (gname, members) in enumerate(groups):
        color = GROUP_COLORS[gi % len(GROUP_COLORS)]
        for mem in members:
            box_color[mem] = color
            group_of[mem] = gname
    for label in box_order:
        box_color.setdefault(label, DEFAULT_COLOR)

    # ---- Layout: one horizontal band per group (top-to-bottom), boxes L->R.
    # Boxes not in any group go in a final "ungrouped" band.
    ordered_groups = [(gname, members) for gname, members in groups]
    grouped_members = {m for _, ms in groups for m in ms}
    ungrouped = [l for l in box_order if l not in grouped_members]
    if ungrouped:
        ordered_groups.append((None, ungrouped))

    # Align what is connected before drawing anything.
    bands = order_within_bands([[m for m in ms if m in boxes]
                                for _, ms in ordered_groups], arrows)
    ordered_groups = [(gname, bands[i])
                      for i, (gname, _) in enumerate(ordered_groups)]

    xs = compact_x(bands, assign_x(bands, arrows))
    lados = choose_note_sides(bands, xs, boxes)

    placed = {}      # label -> {"x","y","w","h"}
    notas_de = {}    # label -> its note's rectangle, an obstacle like any other
    band_of = {}     # label -> band index
    band_bottom = {} # band index -> y just below its content
    elements = []
    cur_y = MARGIN
    content_x0 = content_x1 = MARGIN

    for gi, (gname, members) in enumerate(ordered_groups):
        members = [m for m in members if m in boxes]
        if not members:
            continue
        color = (GROUP_COLORS[gi % len(GROUP_COLORS)]
                 if gname is not None else DEFAULT_COLOR)

        # The header sits above the leftmost box of its own band, not at the
        # page margin: after the x pass a band can start well to the right.
        if gname:
            elements.append(
                make_free_text(new_id(), min(xs[m] for m in members), cur_y,
                               color, gname, GROUP_HEADER_FONT))
            cur_y += GROUP_HEADER_FONT * LINE_H + HEADER_GAP

        box_top = cur_y
        alto_al_lado = BOX_H     # tallest thing level with the boxes
        alto_debajo = 0          # tallest note that had to go underneath

        for label in members:
            info = boxes[label]
            bx, by = xs[label], box_top

            rect_id = new_id()
            txt_id = new_id()
            rect = make_rectangle(rect_id, bx, by, BOX_W, BOX_H,
                                  box_color[label], txt_id)
            lw, lh = text_size(label, BOX_LABEL_FONT)
            ctext = make_container_text(
                txt_id, rect_id,
                bx + (BOX_W - min(lw, BOX_W - 10)) / 2,
                by + (BOX_H - lh) / 2,
                min(lw, BOX_W - 10), lh, box_color[label], label)
            elements.append(rect)
            elements.append(ctext)
            placed[label] = {"x": bx, "y": by, "w": BOX_W, "h": BOX_H,
                             "rect": rect}
            band_of[label] = gi
            content_x0 = min(content_x0, bx)
            content_x1 = max(content_x1, bx + BOX_W)

            texto = note_block(info)
            if texto:
                lado = lados.get(label, "below")
                w, _h = text_size(texto, NOTE_FONT)
                if lado == "right":
                    nx, ny = bx + BOX_W + NOTE_GAP, by
                elif lado == "left":
                    nx, ny = bx - w - NOTE_GAP, by
                else:
                    nx, ny = bx, by + BOX_H + NOTE_GAP
                note_el = make_free_text(new_id(), nx, ny, box_color[label],
                                         texto, NOTE_FONT, opacity=85)
                elements.append(note_el)
                notas_de[label] = {"x": nx, "y": ny,
                                   "w": note_el["width"], "h": note_el["height"]}
                if lado == "below":
                    alto_debajo = max(alto_debajo, NOTE_GAP + note_el["height"])
                else:
                    alto_al_lado = max(alto_al_lado, note_el["height"])
                content_x0 = min(content_x0, nx)
                content_x1 = max(content_x1, nx + note_el["width"])

        band_bottom[gi] = box_top + alto_al_lado + alto_debajo
        cur_y = band_bottom[gi] + BAND_GAP

    # ---- Arrows (bind to placed boxes) ----
    # Deterministic routing: a straight edge-to-edge line unless it would cross
    # something, in which case the arrow goes around. A scoring router that
    # proposed several routes per arrow was tried and measured worse -- it
    # traded many extra crossings for the occasional clear label -- so this
    # stays simple on purpose.
    #
    # Excalidraw pins an arrow's label to the middle of the path, which is why
    # the route matters for legibility and not just for tidiness.
    def medio_del_camino(pts):
        largos = [((pts[i + 1][0] - pts[i][0]) ** 2
                   + (pts[i + 1][1] - pts[i][1]) ** 2) ** 0.5
                  for i in range(len(pts) - 1)]
        objetivo = sum(largos) / 2
        acc = 0.0
        for i, L in enumerate(largos):
            if acc + L >= objetivo:
                t = (objetivo - acc) / (L or 1)
                return (pts[i][0] + t * (pts[i + 1][0] - pts[i][0]),
                        pts[i][1] + t * (pts[i + 1][1] - pts[i][1]))
            acc += L
        return pts[-1]

    lanes = {"left": 0, "right": 0}

    def span(par):
        if par[0] not in placed or par[1] not in placed:
            return 0
        return abs(band_of.get(par[1], 0) - band_of.get(par[0], 0))

    # Lane numbers grow outward, so serving the longest detour last gives it the
    # outermost gutter. Nesting the detours keeps them from crossing each other.
    arrows = sorted(arrows, key=span)

    for src, dst, label in arrows:
        if src not in placed or dst not in placed:
            continue
        a, b = placed[src], placed[dst]
        salto = band_of.get(dst, 0) - band_of.get(src, 0)

        # Notes count as obstacles: they are what the reader is trying to read.
        # A box's own note does not -- an arrow leaving the bottom edge is
        # expected to pass beside it.
        otras = ([r for etiq, r in placed.items() if etiq not in (src, dst)]
                 + [r for etiq, r in notas_de.items() if etiq not in (src, dst)])

        recto_a = edge_point(a, box_center(b))
        recto_b = edge_point(b, box_center(a))
        estorbo = any(segment_hits(recto_a, recto_b, otra) for otra in otras)
        if label and not estorbo:
            # A straight run that ends with its label on top of a note is no
            # better than one that crosses a box.
            lw, lh = text_size(label, ARROW_LABEL_FONT)
            caja_et = {"x": (recto_a[0] + recto_b[0]) / 2 - lw / 2,
                       "y": (recto_a[1] + recto_b[1]) / 2 - lh / 2,
                       "w": lw, "h": lh}
            estorbo = any(rects_overlap(caja_et, otra) for otra in otras)

        if estorbo and salto == 0:
            # Same band: down the empty corridor beside each box, along the gap
            # below the band, and back up.
            hacia_derecha = box_center(b)[0] > box_center(a)[0]
            lado_a = "right" if hacia_derecha else "left"
            lado_b = "left" if hacia_derecha else "right"
            start = side_point(a, lado_a)
            end = side_point(b, lado_b)
            paso = H_GAP / 2 if hacia_derecha else -H_GAP / 2
            y_gap = band_bottom.get(band_of[src], start[1]) + BAND_GAP / 2
            pts = [start, (start[0] + paso, start[1]),
                   (start[0] + paso, y_gap), (end[0] - paso, y_gap),
                   (end[0] - paso, end[1]), end]
        elif estorbo:
            # Different bands: around the side, through a gutter.
            centro = (content_x0 + content_x1) / 2
            side = ("left" if (box_center(a)[0] + box_center(b)[0]) / 2 < centro
                    else "right")
            carril = lanes[side]
            lanes[side] += 1
            if side == "left":
                gx = content_x0 - GUTTER_GAP - carril * GUTTER_LANE
            else:
                gx = content_x1 + GUTTER_GAP + carril * GUTTER_LANE
            start = side_point(a, side)
            end = side_point(b, side)
            pts = [start, (gx, start[1]), (gx, end[1]), end]
        else:
            pts = [recto_a, recto_b]

        ax, ay = pts[0]
        points = [[px - ax, py - ay] for px, py in pts]

        arrow_id = new_id()
        label_id = new_id() if label else None
        arrow = make_arrow(arrow_id, ax, ay, points,
                           a["rect"]["id"], b["rect"]["id"], label_id)
        elements.append(arrow)

        # back-reference the arrow on both boxes so bindings stick
        for box in (a["rect"], b["rect"]):
            box.setdefault("boundElements", [])
            if box["boundElements"] is None:
                box["boundElements"] = []
            box["boundElements"].append({"type": "arrow", "id": arrow_id})

        if label:
            mid_x, mid_y = medio_del_camino(pts)
            elements.append(
                make_arrow_label(label_id, arrow_id, mid_x, mid_y, label))


    return {
        "type": "excalidraw",
        "version": 2,
        "source": "brief-to-excalidraw",
        "elements": elements,
        "appState": {
            "gridSize": 20,
            "gridStep": 5,
            "gridModeEnabled": False,
            "viewBackgroundColor": "#ffffff",
        },
        "files": {},
    }


# --------------------------------------------------------------------------
# Layered layout (Sugiyama)
#
# The band layout gives each GROUP a row, in the order the brief declares them.
# That is fine for swim lanes, and wrong for flows: two groups that exchange
# data can end up six rows apart, and every arrow between them goes round the
# side. The layered layout gives each BOX a rank from the flow itself, so what
# feeds what sits next to it. Groups keep their colour; a group may now span
# several ranks, which is the point.
#
#   1. rank        -- longest path from the sources, then tightened
#   2. dummies     -- an arrow that skips ranks gets invisible waypoints, one per
#                     rank it crosses, so it is routed between the boxes instead
#                     of around the whole drawing
#   3. order       -- barycentre sweeps, keeping the order with fewest crossings;
#                     ties go to group, so a group's boxes stay together
#   4. position    -- each box pulled toward the median of its neighbours, then
#                     spaced by isotonic regression so nothing overlaps
#   5. route       -- through the waypoints, curved (roundness kept)
#
# Every arrow stays bound to its boxes, so the drawing can still be rearranged
# by hand in Excalidraw afterwards.
# --------------------------------------------------------------------------

LAYER_NODE_SEP = 46        # space between two boxes of the same rank
LAYER_MIN_GAP_LR = 170     # min space between two columns (left-to-right)
LAYER_MIN_GAP_TB = 120     # min space between two rows (top-to-bottom)
LABEL_WRAP = 24            # arrow labels wrapped to this many characters
DUMMY_SIZE = 18            # thickness of an unlabelled waypoint
LEGEND_GAP = 60            # space between the legend and the drawing


def wrap_label(s):
    return wrap_text(s, LABEL_WRAP) if s else s


def detect_direction(text):
    """Read the brief's `Layout:` line. Returns 'LR', 'TB' or 'bands'."""
    m = re.search(r"^\W*Layout\W*:\s*\**\s*(.+)$", text, re.I | re.M)
    if not m:
        return "LR"
    v = m.group(1).lower()
    if "left-to-right" in v or "left to right" in v or re.search(r"\blr\b", v):
        return "LR"
    if "top-to-bottom" in v or "top to bottom" in v or re.search(r"\btb\b", v):
        return "TB"
    # Swim lanes are the one case where a group really is a row.
    if "swim" in v or "lane" in v or "band" in v:
        return "bands"
    # "grouped" and anything else: groups as colour, laid out left to right.
    # Measured on real briefs, this beats bands on every count.
    return "LR"


def _break_cycles(nodes, edges):
    """Edges reversed (for ranking only) so the graph is acyclic."""
    succ = collections.defaultdict(list)
    for s, d in edges:
        succ[s].append(d)
    state, reversed_ = {}, set()

    def dfs(u):
        state[u] = 1
        for v in succ[u]:
            if state.get(v) == 1:
                reversed_.add((u, v))
            elif v not in state:
                dfs(v)
        state[u] = 2

    for n in nodes:
        if n not in state:
            dfs(n)
    return reversed_


def _rank(nodes, dag):
    preds = collections.defaultdict(list)
    succs = collections.defaultdict(list)
    for s, d in dag:
        preds[d].append(s)
        succs[s].append(d)

    # Longest path from the sources.
    rank = {}
    indeg = {n: len(preds[n]) for n in nodes}
    queue = [n for n in nodes if indeg[n] == 0]
    topo = []
    while queue:
        u = queue.pop(0)
        topo.append(u)
        rank[u] = max((rank[p] + 1 for p in preds[u]), default=0)
        for v in succs[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)

    # Tighten: a source sits just before its first consumer rather than at the
    # far left, so an orchestrator or a late input does not drag a long arrow
    # across the drawing.
    for u in reversed(topo):
        if not preds[u] and succs[u]:
            rank[u] = min(rank[v] for v in succs[u]) - 1

    # Balance: a box whose arrows in and out are equally many can slide within
    # its slack without lengthening anything; put it in the middle of the slack
    # so the long arrow is split in two shorter ones.
    for _ in range(3):
        for u in topo:
            if not preds[u] or not succs[u]:
                continue
            lo = max(rank[p] for p in preds[u]) + 1
            hi = min(rank[v] for v in succs[u]) - 1
            if hi > lo and len(preds[u]) == len(succs[u]):
                rank[u] = (lo + hi) // 2

    low = min(rank.values(), default=0)
    return {n: r - low for n, r in rank.items()}, topo


def _crossings(layers, succs_of):
    total = 0
    for r in range(len(layers) - 1):
        pos = {n: i for i, n in enumerate(layers[r + 1])}
        pairs = []
        for i, u in enumerate(layers[r]):
            for v in succs_of[u]:
                if v in pos:
                    pairs.append((i, pos[v]))
        pairs.sort()
        seconds = [b for _, b in pairs]
        for i in range(len(seconds)):
            for j in range(i + 1, len(seconds)):
                if seconds[i] > seconds[j]:
                    total += 1
    return total


def _isotonic(targets, sizes, sep):
    """Tops t_i, in order, with t_{i+1} >= t_i + size_i + sep, as close as
    possible (least squares) to the given targets. Pool-adjacent-violators on
    the offsets-removed targets."""
    offs, acc = [], 0.0
    for s in sizes:
        offs.append(acc)
        acc += s + sep
    z = [t - o for t, o in zip(targets, offs)]
    blocks = []                          # [value, weight, count]
    for v in z:
        blocks.append([v, 1.0, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, c2 = blocks.pop()
            v1, w1, c1 = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, c1 + c2])
    out = []
    for v, _, c in blocks:
        out += [v] * c
    return [q + o for q, o in zip(out, offs)]


def build_layered(groups, box_order, boxes, arrows, direction="LR"):
    LR = direction == "LR"

    # ---- colours --------------------------------------------------------
    color_of, group_idx = {}, {}
    for gi, (gname, members) in enumerate(groups):
        for m in members:
            color_of[m] = GROUP_COLORS[gi % len(GROUP_COLORS)]
            group_idx[m] = gi
    nodes = [b for b in box_order if b in boxes]
    for n in nodes:
        color_of.setdefault(n, DEFAULT_COLOR)
        group_idx.setdefault(n, len(groups))
    decl = {n: i for i, n in enumerate(nodes)}

    edges = [(s, d, l) for s, d, l in arrows
             if s in boxes and d in boxes and s != d]

    # ---- 1. ranks -------------------------------------------------------
    reversed_ = _break_cycles(nodes, [(s, d) for s, d, _ in edges])
    dag = [((d, s) if (s, d) in reversed_ else (s, d)) for s, d, _ in edges]
    rank, _topo = _rank(nodes, dag)

    # A box with no arrow at all has no rank of its own: put it with its group
    # (median rank of its group-mates) rather than stranded in the first column.
    linked = {x for e in dag for x in e}
    for n in nodes:
        if n in linked:
            continue
        mates = sorted(rank[m] for m in nodes
                       if m in linked and group_idx[m] == group_idx[n])
        rank[n] = mates[len(mates) // 2] if mates else 0

    # ---- 2. waypoints ---------------------------------------------------
    succs_of = collections.defaultdict(list)
    preds_of = collections.defaultdict(list)
    chain = {}                  # edge index -> [u, w1, w2, ..., v] in layout order
    size_extra = {}             # waypoint -> thickness
    for ei, ((u, v), (_, _, lab)) in enumerate(zip(dag, edges)):
        path = [u]
        for r in range(rank[u] + 1, rank[v]):
            w = ("~", ei, r)
            rank[w] = r
            path.append(w)
            size_extra[w] = DUMMY_SIZE
        path.append(v)
        chain[ei] = path
        for a, b in zip(path, path[1:]):
            succs_of[a].append(b)
            preds_of[b].append(a)

    # A label sits in the middle of its arrow. Give that waypoint room for it.
    label_size = {}
    for ei, (_, _, lab) in enumerate(edges):
        if lab:
            lw, lh = text_size(wrap_label(lab), ARROW_LABEL_FONT)
            label_size[ei] = (lw, lh)
            path = chain[ei]
            if len(path) > 2 and (len(path) - 1) % 2 == 0:
                mid = path[(len(path) - 1) // 2]
                size_extra[mid] = (lh if LR else lw) + 14

    n_ranks = max(rank.values()) + 1 if rank else 0
    layers = [[] for _ in range(n_ranks)]
    for n, r in sorted(rank.items(),
                       key=lambda kv: (decl.get(kv[0], 10**6)
                                       if not isinstance(kv[0], tuple)
                                       else 10**6 + kv[0][1])):
        layers[r].append(n)

    # ---- 3. order -------------------------------------------------------
    def gkey(n):
        if isinstance(n, tuple):
            return len(groups) + 1
        return group_idx[n]

    def sweep(layers, down):
        rng = range(1, n_ranks) if down else range(n_ranks - 2, -1, -1)
        for r in rng:
            ref = layers[r - 1] if down else layers[r + 1]
            pos = {n: i for i, n in enumerate(ref)}
            cur = {n: i for i, n in enumerate(layers[r])}

            def bary(n):
                nb = preds_of[n] if down else succs_of[n]
                ps = [pos[x] for x in nb if x in pos]
                return (sum(ps) / len(ps)) if ps else cur[n]
            layers[r] = sorted(layers[r], key=lambda n: (bary(n), gkey(n)))
        return layers

    best = [list(l) for l in layers]
    best_c = _crossings(best, succs_of)
    for it in range(24):
        layers = sweep(layers, down=(it % 2 == 0))
        c = _crossings(layers, succs_of)
        if c < best_c:
            best, best_c = [list(l) for l in layers], c
    layers = best

    # Pairwise swaps: local fix-up the sweeps miss.
    improved = True
    while improved:
        improved = False
        for r in range(n_ranks):
            for i in range(len(layers[r]) - 1):
                layers[r][i], layers[r][i + 1] = layers[r][i + 1], layers[r][i]
                c = _crossings(layers, succs_of)
                if c < best_c:
                    best_c, improved = c, True
                else:
                    layers[r][i], layers[r][i + 1] = layers[r][i + 1], layers[r][i]

    # ---- sizes ----------------------------------------------------------
    # A box that receives or sends many arrows is drawn taller (left-to-right)
    # so the arrows can arrive at distinct points instead of one knot.
    deg_in = collections.Counter(d for s, d, _ in edges)
    deg_out = collections.Counter(s for s, d, _ in edges)
    box_h = {}
    for n in nodes:
        k = max(deg_in[n], deg_out[n])
        box_h[n] = max(BOX_H, 22 * k + 20) if (LR and k >= 3) else BOX_H

    notes = {n: note_block(boxes[n]) for n in nodes}
    note_wh = {n: (text_size(t, NOTE_FONT) if t else (0, 0))
               for n, t in notes.items()}

    def along(n):      # extent along the rank axis
        if isinstance(n, tuple):
            return 0
        if LR:
            return BOX_W
        return max(box_h[n], note_wh[n][1])

    def across(n):     # extent within a rank
        if isinstance(n, tuple):
            return size_extra.get(n, DUMMY_SIZE)
        nw, nh = note_wh[n]
        if LR:
            return box_h[n] + (NOTE_GAP + nh if nh else 0)
        return BOX_W + (NOTE_GAP + nw if nw else 0)

    def port(n):       # where arrows attach, measured from the top/left
        if isinstance(n, tuple):
            return size_extra.get(n, DUMMY_SIZE) / 2
        return (box_h[n] if LR else BOX_W) / 2

    # ---- 4. positions within ranks --------------------------------------
    top = {}
    for r in range(n_ranks):
        acc = 0.0
        for n in layers[r]:
            top[n] = acc
            acc += across(n) + LAYER_NODE_SEP

    def settle(r, use):
        ts = []
        for n in layers[r]:
            nb = []
            if use in ("pred", "both"):
                nb += preds_of[n]
            if use in ("succ", "both"):
                nb += succs_of[n]
            ps = sorted(top[x] + port(x) for x in nb)
            if ps:
                mid = ps[len(ps) // 2] if len(ps) % 2 else (
                    ps[len(ps) // 2 - 1] + ps[len(ps) // 2]) / 2
                ts.append(mid - port(n))
            else:
                ts.append(top[n])
        new = _isotonic(ts, [across(n) for n in layers[r]], LAYER_NODE_SEP)
        for n, t in zip(layers[r], new):
            top[n] = t

    for it in range(30):
        if it % 3 == 0:
            for r in range(1, n_ranks):
                settle(r, "pred")
        elif it % 3 == 1:
            for r in range(n_ranks - 2, -1, -1):
                settle(r, "succ")
        else:
            for r in range(n_ranks):
                settle(r, "both")

    # Straighten long arrows: all the waypoints of one arrow at the same height
    # whenever their neighbours in each rank leave room for it. The feasible
    # heights of a chain are the intersection of each waypoint's free interval,
    # so this is exact, not a heuristic; it only moves waypoints, never boxes.
    index_in = {}
    for r in range(n_ranks):
        for i, n in enumerate(layers[r]):
            index_in[n] = (r, i)

    def free_interval(w):
        r, i = index_in[w]
        lo, hi = -1e18, 1e18
        if i > 0:
            p = layers[r][i - 1]
            lo = top[p] + across(p) + LAYER_NODE_SEP
        if i + 1 < len(layers[r]):
            q = layers[r][i + 1]
            hi = top[q] - across(w) - LAYER_NODE_SEP
        return lo, hi

    chains_ = sorted((c for c in chain.values() if len(c) > 2),
                     key=len, reverse=True)
    for _ in range(3):
        for c in chains_:
            ws = c[1:-1]
            lo = max(free_interval(w)[0] for w in ws)
            hi = min(free_interval(w)[1] for w in ws)
            if lo > hi:
                continue
            # Prefer the height that also lines up with one of the two ends.
            ends = [top[c[0]] + port(c[0]), top[c[-1]] + port(c[-1])]
            cur = sorted(top[w] + port(w) for w in ws)
            wanted = cur[len(cur) // 2]
            for e in ends:
                if lo <= e - port(ws[0]) <= hi:
                    wanted = e
                    break
            y = min(max(wanted - port(ws[0]), lo), hi)
            for w in ws:
                top[w] = y + port(ws[0]) - port(w)

    # ---- rank coordinates: gaps sized for the labels that land in them ---
    gap_need = collections.defaultdict(float)
    for ei, (lw, lh) in label_size.items():
        path = chain[ei]
        segs = len(path) - 1
        if segs % 2 == 1:           # midpoint falls between two ranks
            a = path[segs // 2]
            gap_need[rank[a]] = max(gap_need[rank[a]], (lw if LR else lh) + 50)
    rank_extent = [max((along(n) for n in layers[r]), default=0)
                   for r in range(n_ranks)]
    min_gap = LAYER_MIN_GAP_LR if LR else LAYER_MIN_GAP_TB
    rank_pos, acc = [], 0.0
    for r in range(n_ranks):
        rank_pos.append(acc)
        acc += rank_extent[r] + max(min_gap, gap_need[r])

    # ---- legend (top-left), then the drawing below it ------------------
    elements = []
    legend_h = 0
    if groups:
        lx, ly = MARGIN, MARGIN
        x = lx
        for gi, (gname, members) in enumerate(groups):
            if not any(m in boxes for m in members):
                continue
            col = GROUP_COLORS[gi % len(GROUP_COLORS)]
            swatch = base_props(new_id(), x, ly + 6, 26, 18, col)
            swatch["type"] = "rectangle"
            swatch["backgroundColor"] = col
            swatch["roundness"] = {"type": 3}
            elements.append(swatch)
            t = make_free_text(new_id(), x + 34, ly, col, gname, NOTE_FONT + 2)
            elements.append(t)
            x += 34 + t["width"] + 36
        legend_h = (NOTE_FONT + 2) * LINE_H + LEGEND_GAP

    min_top = min(top.values(), default=0)
    ox, oy = MARGIN, MARGIN + legend_h

    def xy(n):
        """Top-left of node n in drawing coordinates."""
        r = rank[n]
        if LR:
            return ox + rank_pos[r], oy + top[n] - min_top
        return ox + top[n] - min_top, oy + rank_pos[r]

    # ---- boxes and notes -------------------------------------------------
    placed = {}
    for n in nodes:
        bx, by = xy(n)
        rect_id, txt_id = new_id(), new_id()
        bh = box_h[n]
        rect = make_rectangle(rect_id, bx, by, BOX_W, bh, color_of[n], txt_id)
        lw, lh = text_size(n, BOX_LABEL_FONT)
        elements.append(rect)
        elements.append(make_container_text(
            txt_id, rect_id, bx + (BOX_W - min(lw, BOX_W - 10)) / 2,
            by + (bh - lh) / 2, min(lw, BOX_W - 10), lh, color_of[n], n))
        placed[n] = {"x": bx, "y": by, "w": BOX_W, "h": bh, "rect": rect}
        if notes[n]:
            if LR:
                nx, ny = bx, by + bh + NOTE_GAP
            else:
                nx, ny = bx + BOX_W + NOTE_GAP, by
            elements.append(make_free_text(new_id(), nx, ny, color_of[n],
                                           notes[n], NOTE_FONT, opacity=85))

    # ---- 5. arrows -------------------------------------------------------
    # Ports: several arrows on one side of a box are spread along it, in the
    # order of where they go, so they fan out instead of leaving from a dot.
    out_side = collections.defaultdict(list)
    in_side = collections.defaultdict(list)
    for ei, (s, d, _) in enumerate(edges):
        path = chain[ei]
        nxt, prv = path[1], path[-2]
        if (s, d) in reversed_:
            path = list(reversed(path))
            chain[ei] = path
            nxt, prv = path[1], path[-2]

        def cpos(n):
            x, y = (xy(n) if not isinstance(n, tuple) else xy(n))
            return (y + port(n)) if LR else (x + port(n))
        out_side[s].append((cpos(nxt), ei))
        in_side[d].append((cpos(prv), ei))

    start_off, end_off = {}, {}
    for side, store in ((out_side, start_off), (in_side, end_off)):
        for n, lst in side.items():
            lst.sort()
            k = len(lst)
            span = (box_h[n] if LR else BOX_W) * 0.8
            for i, (_, ei) in enumerate(lst):
                store[ei] = 0 if k == 1 else (-span / 2 + span * i / (k - 1))

    def mid_of(pts):
        L = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        half, acc = sum(L) / 2, 0.0
        for i, l in enumerate(L):
            if acc + l >= half:
                t = (half - acc) / (l or 1)
                return (pts[i][0] + t * (pts[i + 1][0] - pts[i][0]),
                        pts[i][1] + t * (pts[i + 1][1] - pts[i][1]))
            acc += l
        return pts[-1]

    arrow_paths = []
    for ei, (s, d, lab) in enumerate(edges):
        a, b = placed[s], placed[d]
        path = chain[ei]
        forward = rank[d] > rank[s]
        pts = []
        if LR:
            sy = a["y"] + a["h"] / 2 + start_off[ei]
            ey = b["y"] + b["h"] / 2 + end_off[ei]
            sx = a["x"] + BOX_W if forward else a["x"]
            ex = b["x"] if forward else b["x"] + BOX_W
            pts.append((sx, sy))
            # The note under the box can be a little wider than the box: run
            # flat past its edge before the first bend, so the bend cannot cut it.
            note_right = a["x"] + note_wh[s][0]
            if forward and note_wh[s][0] and note_right + 10 > sx + 6:
                pts.append((note_right + 10, sy))
            for w in path[1:-1]:
                wx, wy = xy(w)
                cy = wy + port(w)
                colw = rank_extent[rank[w]]
                first, last = (wx, wx + colw) if forward else (wx + colw, wx)
                pts += [(first, cy), (last, cy)]
            pts.append((ex, ey))
        else:
            sx = a["x"] + BOX_W / 2 + start_off[ei]
            ex = b["x"] + BOX_W / 2 + end_off[ei]
            sy = a["y"] + BOX_H if forward else a["y"]
            ey = b["y"] if forward else b["y"] + BOX_H
            pts.append((sx, sy))
            # Notes sit to the right of their box and can be taller than it.
            # Drop straight down to the bottom of the row first, through the
            # empty space under the box, so the arrow never cuts a note.
            if forward:
                row_bottom = oy + rank_pos[rank[s]] + rank_extent[rank[s]]
                if row_bottom > sy + 4:
                    pts.append((sx, row_bottom))
            for w in path[1:-1]:
                wx, wy = xy(w)
                cx = wx + port(w)
                rowh = rank_extent[rank[w]]
                first, last = (wy, wy + rowh) if forward else (wy + rowh, wy)
                pts += [(cx, first), (cx, last)]
            pts.append((ex, ey))

        # S-bends between ranks, computed here rather than left to Excalidraw.
        # Excalidraw rounds an arrow with a spline through its points, and a
        # spline through a few far-apart points overshoots sideways at every
        # bend -- the arrow swings past its height and comes back. So each bend
        # is a cubic with square ends (leaves and arrives parallel to the flow),
        # sampled densely; the spline then only rounds small angles.
        def s_bend(p, q, n=9):
            out = []
            if LR:
                c1, c2 = (p[0] + (q[0] - p[0]) / 2, p[1]), (q[0] - (q[0] - p[0]) / 2, q[1])
            else:
                c1, c2 = (p[0], p[1] + (q[1] - p[1]) / 2), (q[0], q[1] - (q[1] - p[1]) / 2)
            for i in range(1, n):
                u = i / n
                a_, b_, c_, d_ = (1 - u) ** 3, 3 * u * (1 - u) ** 2, 3 * u * u * (1 - u), u ** 3
                out.append((a_ * p[0] + b_ * c1[0] + c_ * c2[0] + d_ * q[0],
                            a_ * p[1] + b_ * c1[1] + c_ * c2[1] + d_ * q[1]))
            return out

        smooth = [pts[0]]
        for p, q in zip(pts, pts[1:]):
            cross = abs(p[1] - q[1]) if LR else abs(p[0] - q[0])
            run = abs(q[0] - p[0]) if LR else abs(q[1] - p[1])
            if cross > 4 and run > 20:
                smooth += s_bend(p, q)
            smooth.append(q)
        # Drop points that sit on a straight run: they add nothing and uneven
        # spacing is what makes the spline wobble.
        # Two points a pixel apart are just as bad: the spline loops back
        # between them. Keep points at least a few pixels from the last kept.
        pts = [smooth[0]]
        for i in range(1, len(smooth) - 1):
            (x0, y0), (x1, y1), (x2, y2) = pts[-1], smooth[i], smooth[i + 1]
            if math.dist((x0, y0), (x1, y1)) < 6:
                continue
            if abs((x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0)) > 1e-6:
                pts.append(smooth[i])
        if math.dist(pts[-1], smooth[-1]) < 6 and len(pts) > 1:
            pts[-1] = smooth[-1]
        else:
            pts.append(smooth[-1])

        # A long straight run between two short bends is the other source of
        # hooks: the spline takes its direction at a point from both
        # neighbours, so it leaves a 2000px run with an enormous momentum,
        # overshoots the next point and comes back. Spacing points by doubling
        # steps from each end (40, 80, 160, ...) keeps neighbouring segments
        # within a factor of two, and the run stays perfectly straight.
        dense = [pts[0]]
        for p, q in zip(pts, pts[1:]):
            L = math.dist(p, q)
            if L > 100:
                ds, step = [], 40.0
                while step < L / 2 - 20:
                    ds.append(step)
                    step *= 2
                marks = ds + [L / 2] + [L - x for x in reversed(ds)]
                for m in marks:
                    u = m / L
                    dense.append((p[0] + (q[0] - p[0]) * u, p[1] + (q[1] - p[1]) * u))
            dense.append(q)
        pts = dense

        arrow_paths.append(pts)
        ax, ay = pts[0]
        points = [[px - ax, py - ay] for px, py in pts]
        arrow_id = new_id()
        label_id = new_id() if lab else None
        elements.append(make_arrow(arrow_id, ax, ay, points,
                                   a["rect"]["id"], b["rect"]["id"], label_id))
        for box in (a["rect"], b["rect"]):
            if box.get("boundElements") is None:
                box["boundElements"] = []
            box["boundElements"].append({"type": "arrow", "id": arrow_id})
        if lab:
            mx, my = mid_of(pts)
            elements.append(make_arrow_label(label_id, arrow_id, mx, my,
                                             wrap_label(lab)))

    # ---- group titles, last, so they can dodge the arrows too -------------
    # Above the group's first box is the natural spot, but in top-to-bottom
    # that is exactly where arrows arrive; try beside it, then give up -- the
    # legend already names every colour.
    taken = [(e["x"], e["y"], e["width"], e["height"])
             for e in elements if e["type"] in ("rectangle", "text")
             and not e.get("containerId")]
    samples = []
    for pts in arrow_paths:
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            for k in range(12):
                samples.append((x1 + (x2 - x1) * k / 12, y1 + (y2 - y1) * k / 12))

    def free(x, y, w, h, pad=6):
        if any(not (x + w + pad < X or X + W + pad < x or y + h + pad < Y
                    or Y + H + pad < y) for X, Y, W, H in taken):
            return False
        return not any(x - pad <= px <= x + w + pad and y - pad <= py <= y + h + pad
                       for px, py in samples)

    for gi, (gname, members) in enumerate(groups):
        ms = [m for m in members if m in placed]
        if not ms:
            continue
        first = (min(ms, key=lambda m: (placed[m]["y"], placed[m]["x"])) if LR
                 else min(ms, key=lambda m: (placed[m]["x"], placed[m]["y"])))
        w, h = text_size(gname, NOTE_FONT + 4)
        bx, by, bw, bh = (placed[first][k] for k in ("x", "y", "w", "h"))
        candidates = [
            (bx, by - h - 8),                          # above
            (bx - w - 16, by + (bh - h) / 2),          # left
            (bx + bw - w, by - h - 8),                 # above, right-aligned
        ]
        for hx, hy in candidates:
            if free(hx, hy, w, h):
                col = GROUP_COLORS[gi % len(GROUP_COLORS)]
                elements.append(make_free_text(new_id(), hx, hy, col, gname,
                                               NOTE_FONT + 4))
                taken.append((hx, hy, w, h))
                break

    return {
        "type": "excalidraw",
        "version": 2,
        "source": "brief-to-excalidraw",
        "elements": elements,
        "appState": {"gridSize": 20, "gridStep": 5, "gridModeEnabled": False,
                     "viewBackgroundColor": "#ffffff"},
        "files": {},
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = [a for a in sys.argv[1:] if a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)
    brief_path = Path(args[0])
    out_path = (Path(args[1]) if len(args) > 1
                else brief_path.with_suffix(".excalidraw"))
    text = brief_path.read_text(encoding="utf-8")
    groups, box_order, boxes, arrows = parse_brief(text)

    direction = detect_direction(text)
    for o in opts:
        if o.startswith("--layout="):
            direction = {"lr": "LR", "tb": "TB", "bands": "bands"}[o.split("=", 1)[1].lower()]
    if not arrows:
        direction = "bands"      # nothing to lay out by flow
    if direction == "bands":
        doc = build(groups, box_order, boxes, arrows)
    else:
        doc = build_layered(groups, box_order, boxes, arrows, direction)
    out_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    n_rect = sum(1 for e in doc["elements"] if e["type"] == "rectangle"
                 and e.get("backgroundColor") == "transparent")
    n_arrow = sum(1 for e in doc["elements"] if e["type"] == "arrow")
    print(f"Wrote {out_path}")
    print(f"  layout: {direction}  groups: {len(groups)}  boxes: {n_rect}  arrows: {n_arrow}")
    missing = [(s, d) for s, d, _ in arrows
               if s not in boxes or d not in boxes]
    if missing:
        print(f"  WARNING: {len(missing)} arrow(s) referenced unknown boxes: {missing}")


if __name__ == "__main__":
    main()
