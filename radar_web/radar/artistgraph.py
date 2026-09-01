"""Graphe artiste ↔ artiste : relie des artistes-graines à leurs artistes
co-crédités, d'après le graphe producteur (`producer_graph.json` → `edges`).

Contrairement au graphe de labels, `edges` est biparti (graine ↔ candidat
uniquement — aucune donnée candidat ↔ candidat n'est stockée), donc seul un
réseau à un niveau (ego-network) est honnête ici sans nouveaux appels API.
"""


def build(graph, seed_items, max_nodes=60, max_fanout=15):
    """seed_items: [(key, name), ...] déjà résolus.
    -> {"nodes": {key: {name, level}}, "edges": [{a,b,w}], "seeds": [names]}"""
    edges_raw = (graph or {}).get("edges", {}) or {}
    nodes = {}
    for k, name in seed_items:
        if k and k not in nodes:
            nodes[k] = {"name": name, "level": 0}
    seed_set = set(nodes)
    if not seed_set:
        return {"nodes": {}, "edges": [], "seeds": []}

    edges, seen_edges = [], set()
    for sk in list(seed_set):
        ranked = []
        for ck, ce in edges_raw.items():
            if ck in seed_set:
                continue
            d = (ce.get("co") or {}).get(sk)
            if d:
                ranked.append((ck, ce.get("name") or ck, d.get("n", 0)))
        ranked.sort(key=lambda t: -t[2])
        for ck, name, w in ranked[:max_fanout]:
            if ck not in nodes:
                if len(nodes) >= max_nodes:
                    continue
                nodes[ck] = {"name": name, "level": 1}
            sig = (sk, ck)
            if sig not in seen_edges:
                seen_edges.add(sig)
                edges.append({"a": sk, "b": ck, "w": w})
    return {"nodes": nodes, "edges": edges, "seeds": [n for _, n in seed_items]}
