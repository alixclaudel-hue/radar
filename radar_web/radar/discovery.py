"""voie artiste seule du mode Découverte de la playlist Reco Radar.

On conserve les K meilleurs voisins de chaque graine, exigeant au moins
``min_n`` co-crédits, puis on exclut les artistes déjà connus. La voie label
est abandonnée car elle est polluée par les majors.
"""


def discovery_artists(ctx, k=10, min_n=3) -> dict:
    """Découvre des artistes voisins des graines de l’utilisateur.

    Les scores et explications proviennent du rescoring global lorsqu’il
    existe ; sinon, les co-crédits retenus fournissent un score simple et
    traçable. Aucun accès réseau ni disque n’est effectué ici.
    """
    graph = ctx.graph or {}
    edges = graph.get("edges") or {}
    if not edges:
        return {}

    tiers = ctx.artist_tier_map() or {}
    if not tiers:
        return {}

    # On inverse les relations pour pouvoir limiter chaque graine séparément.
    neighbors_by_seed = {}
    for seed_key in tiers:
        neighbors = []
        for artist_key, edge in edges.items():
            co = edge.get("co") or {}
            relation = co.get(seed_key)
            if relation is not None:
                n = relation.get("n", 0)
                if n >= min_n:
                    neighbors.append((artist_key, n))

        neighbors.sort(key=lambda item: (-item[1], item[0]))
        neighbors_by_seed[seed_key] = neighbors[:k]

    # Chaque artiste peut être retenu par plusieurs graines : on conserve
    # toutes les paires pour pouvoir expliquer son score et ses graines.
    retained = {}
    for seed_key, neighbors in neighbors_by_seed.items():
        for artist_key, n in neighbors:
            retained.setdefault(artist_key, []).append((seed_key, n))

    known = set(tiers)

    corpus = ctx.corpus or []
    for record in corpus:
        artist_name = record.get("artist")
        if artist_name:
            known.add(ctx.canon_artist_key(artist_name))

    collection = ctx.collection or {}
    artist_counts = collection.get("artist_counts") or {}
    for artist_name in artist_counts:
        known.add(ctx.canon_artist_key(artist_name))

    remaining = {
        artist_key: relations
        for artist_key, relations in retained.items()
        if artist_key not in known
    }
    if not remaining:
        return {}

    # Un seul rescoring est demandé, même si plusieurs artistes sont retenus.
    rescoring = ctx.graph_rescore() or {}
    rescored_artists = rescoring.get("artists") or {}
    seed_metadata = graph.get("seeds") or {}

    result = {}
    for artist_key in sorted(remaining):
        edge = edges.get(artist_key) or {}
        relations = sorted(
            remaining[artist_key],
            key=lambda item: (-item[1], item[0]),
        )

        seeds = []
        for seed_key, _n in relations:
            # graph["seeds"] associe la clé au NOM (chaîne), comme le lit
            # Ctx._compute_graph_rescore ; un dict est toléré pour les vieux graphes.
            meta = seed_metadata.get(seed_key)
            if isinstance(meta, dict):
                meta = meta.get("name")
            seed_name = meta or seed_key
            seeds.append(seed_name)

        rescored = rescored_artists.get(artist_key)
        if rescored is not None:
            score = float(rescored.get("score", 0))
            why = list(rescored.get("why") or [])
        else:
            score = float(sum(n for _seed_key, n in relations))
            why = [
                f"{n}× avec {seed_name}"
                for (_seed_key, n), seed_name in zip(relations, seeds)
            ]

        result[artist_key] = {
            "name": edge.get("name") or artist_key,
            "id": edge.get("id"),
            "score": score,
            "why": why,
            "seeds": seeds,
        }

    return result
