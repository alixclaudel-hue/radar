"""Graphe label ↔ label : deux labels sont reliés s'ils partagent des artistes,
d'après le graphe producteur (`producer_graph.json` → `label_edges`, construit
depuis les crédits de release des artistes-graines). Aucun appel API — recalcul
instantané à partir des données déjà en cache.
"""
import math

from .store import normalize_label


def _neighbors(label_edges, lk, min_shared, exclude):
    le = label_edges.get(lk)
    if not le:
        return {}
    artists = set(le.get("co", {}) or {})
    if not artists:
        return {}
    out = {}
    for ok, oe in label_edges.items():
        if ok == lk or ok in exclude:
            continue
        shared = artists & set(oe.get("co", {}) or {})
        if len(shared) >= min_shared:
            out[ok] = {"name": oe.get("name") or ok, "shared": len(shared)}
    return out


def build(graph, seed_names, depth=1, min_shared=1, max_nodes=60, max_fanout=8):
    """-> {"nodes": {key: {name, level}}, "edges": [{a,b,w}], "seeds": [names]}"""
    label_edges = (graph or {}).get("label_edges", {}) or {}
    depth = max(1, min(2, int(depth or 1)))
    min_shared = max(1, int(min_shared or 1))

    nodes = {}
    for name in seed_names:
        k = normalize_label(name)
        if not k or k in nodes:
            continue
        le = label_edges.get(k)
        nodes[k] = {"name": (le or {}).get("name") or name, "level": 0}

    edges, seen_edges = [], set()
    frontier = list(nodes)
    for lvl in range(1, depth + 1):
        next_frontier = []
        for fk in frontier:
            neigh = _neighbors(label_edges, fk, min_shared, exclude=set(nodes))
            ranked = sorted(neigh.items(), key=lambda kv: -kv[1]["shared"])[:max_fanout]
            for ok, info in ranked:
                if ok not in nodes:
                    if len(nodes) >= max_nodes:
                        continue
                    nodes[ok] = {"name": info["name"], "level": lvl}
                    next_frontier.append(ok)
                sig = tuple(sorted((fk, ok)))
                if sig not in seen_edges:
                    seen_edges.add(sig)
                    edges.append({"a": fk, "b": ok, "w": info["shared"]})
        frontier = next_frontier
        if not frontier:
            break
    return {"nodes": nodes, "edges": edges, "seeds": list(seed_names)}


def layout(nodes, size=560):
    """{key: [x, y]} — graines au centre, niveaux suivants en anneaux concentriques."""
    by_level = {}
    for k, n in nodes.items():
        by_level.setdefault(n["level"], []).append(k)
    cx = cy = size / 2
    radii = {0: 0, 1: size * 0.30, 2: size * 0.46}
    pos = {}
    for lvl in sorted(by_level):
        keys = by_level[lvl]
        r = radii.get(lvl, size * 0.46 + 60 * (lvl - 2))
        n = len(keys)
        if lvl == 0 and n == 1:
            pos[keys[0]] = [cx, cy]
            continue
        r0 = 26 if lvl == 0 else r          # petites graines multiples = petit anneau
        r = r0 if lvl == 0 else r
        for i, k in enumerate(keys):
            ang = 2 * math.pi * i / max(1, n) - math.pi / 2
            pos[k] = [round(cx + r * math.cos(ang), 1), round(cy + r * math.sin(ang), 1)]
    return pos
