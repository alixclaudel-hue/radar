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
Groups are stacked top-to-bottom as bands, but within a band the boxes are not
evenly spaced: they are ordered and then positioned so that connected boxes end
up above one another, which is what turns a mess of diagonals into something
readable. In order, the passes are:

1. `order_within_bands` -- barycentre ordering, to decide left-to-right order.
2. `assign_x`           -- median coordinate assignment, to align what connects.
3. `compact_x`          -- squeeze the empty corridors that pass leaves behind.
4. `choose_note_sides`  -- put notes beside their box when a corridor is free.
5. routing              -- straight lines, and detours only where a line would
                           actually cross a box or drop its label on a note.

None of this assumes anything about the project: bands of one box and bands of
ten both work, as do briefs with no arrows at all.

Known limit: Excalidraw pins an arrow's label to the middle of the arrow's path
and recomputes that position on load, so the label cannot be placed
independently. On a dense diagram some labels still land on a note. The
alternative -- unbinding the labels -- would place them freely but stop them
following the arrow when the user drags a box, which is worse for a drawing
meant to be rearranged by hand.

Usage
-----
    python3 brief_to_excalidraw.py <brief.md> [output.excalidraw]

If output is omitted, writes "<brief-stem>.excalidraw" next to the brief.
"""

import collections
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
            boxes[label] = {"notes": [], "marks": set()}
            box_order.append(label)

    re_group = re.compile(
        r'^Create a group labeled\s+"(.+?)"\s+that contains:\s*(.+)$', re.I | re.S)
    re_box = re.compile(
        r'^Create a box labeled\s+"(.+?)"\s*\.\s*(?:Note:\s*(.+))?$', re.I | re.S)
    re_arrow = re.compile(r'^Draw an arrow from\s+(.+)$', re.I | re.S)
    re_mark = re.compile(
        r'^Mark\s+(.+?)\s+as\s+(entry point|external)\s*\.?$', re.I | re.S)
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

def wrap_text(s, width=NOTE_WRAP_CHARS):
    """Greedy word-wrap. Preserves explicit newlines in the source."""
    out_lines = []
    for raw in s.split("\n"):
        words = raw.split(" ")
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


def note_block(info):
    """The note text that belongs beside a box, or None."""
    parts = []
    if "entry point" in info["marks"]:
        parts.append("▸ ENTRY POINT")
    if "external" in info["marks"]:
        parts.append("▸ external")
    for n in info["notes"]:
        parts.append(wrap_text(n))
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


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    brief_path = Path(sys.argv[1])
    out_path = (Path(sys.argv[2]) if len(sys.argv) > 2
                else brief_path.with_suffix(".excalidraw"))
    text = brief_path.read_text(encoding="utf-8")
    groups, box_order, boxes, arrows = parse_brief(text)
    doc = build(groups, box_order, boxes, arrows)
    out_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    n_rect = sum(1 for e in doc["elements"] if e["type"] == "rectangle")
    n_arrow = sum(1 for e in doc["elements"] if e["type"] == "arrow")
    print(f"Wrote {out_path}")
    print(f"  groups: {len(groups)}  boxes: {n_rect}  arrows: {n_arrow}")
    missing = [(s, d) for s, d, _ in arrows
               if s not in boxes or d not in boxes]
    if missing:
        print(f"  WARNING: {len(missing)} arrow(s) referenced unknown boxes: {missing}")


if __name__ == "__main__":
    main()
