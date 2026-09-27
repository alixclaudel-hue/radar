"""Jobs longs de Crate Radar, lancés par `crate_jobs.py` (un module par domaine).

    common      répertoires/chemins de données, config, E/S JSON, classe `Job`
    sources     clients Discogs, YouTube (parsing), Spotify, Bandcamp/Subsonic
    tracks      règles pistes/sorties : placeholders, mixes continus, identité
    ingest      ingest_youtube/spotify/bandcamp, fetch_collection, merge_corpus, profile_labels
    graph       build_graph, resolve_artists
    djsets      ingest_djsets (yt-dlp + Playwright)
    search      search_base, scan_sellers, scan_veille, seller_inventory, scan_catalog
    curation    enrich, canonicalize, prune_labels
    recos       scan_recos, publish_recos (playlist RECOS RADAR)
    dump        import_discogs_dump, build_catalog_labelgraph
    scorestore  scorestore_releases, scorestore_tracks, reco_index

Les tests patchent les noms (`cfg_load`, `discogs_get`, chemins…) dans le module
qui définit le job testé : c'est là qu'ils sont résolus, pas dans `common`.
"""
