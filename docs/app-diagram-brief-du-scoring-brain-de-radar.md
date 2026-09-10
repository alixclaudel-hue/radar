# Diagram Brief: Le "cerveau" de scoring de Radar

**Layout**: top-to-bottom
**Flow summary**: Montre comment les jobs de fond et l'utilisateur alimentent les fichiers JSON par utilisateur, comment `Ctx(uid)` les charge et calcule une signature de cache, comment les méthodes de scoring s'enchaînent (wmap → ascore/graph_rescore → reco_rows → album_score), et où les scores sont consommés (routes web, jobs, boucle de feedback qui reboucle sur la config).

---

## Elements

Create a group labeled "Entrées : jobs de fond + utilisateur" that contains: Jobs de fond (ingestion, fetch_collection, profile_labels, canonicalize), build_graph (job mensuel), Utilisateur (Réglages), Dump Discogs SQLite.

Create a group labeled "Fichiers JSON par utilisateur (état persistant)" that contains: crate_radar_config.json, taste_corpus.json, collection_cache.json, Profils & résolutions, producer_graph.json.

Create a group labeled "Ctx(uid) - coeur du scoring (scoring.py)" that contains: Ctx.__init__, wmap, ascore, graph_rescore, label_db_signal, reco_rows, album_score.

Create a group labeled "Consommateurs des scores" that contains: Routes web, Jobs consommateurs, Boucle de feedback (learn.py).

Create a box labeled "Jobs de fond (ingestion, fetch_collection, profile_labels, canonicalize)". Note: hors périmètre de ce document — voir docs/app-overview.md pour le détail de chaque job.

Create a box labeled "build_graph (job mensuel)". Note: mode "taste", écrit des arêtes brutes de co-crédits, jamais de scores.

Create a box labeled "Utilisateur (Réglages)". Note: édite les poids, catégories de goût, base de labels/artistes.

Create a box labeled "Dump Discogs SQLite". Note: discogs_dump.sqlite3, partagé entre utilisateurs, import mensuel, lecture seule, hors périmètre.

Create a box labeled "crate_radar_config.json". Note: poids de scoring, taste_categories, artist_categories, labels suivis, watchlist.

Create a box labeled "taste_corpus.json". Note: tout ce qui a été écouté (YouTube/Spotify/Bandcamp/DJ sets).

Create a box labeled "collection_cache.json". Note: disques possédés / en wantlist Discogs.

Create a box labeled "Profils & résolutions". Note: labels_profile.json + labels_resolved.json + artists_resolved.json.

Create a box labeled "producer_graph.json". Note: arêtes brutes de co-crédits (pas de score), écrites par build_graph.

Create a box labeled "Ctx.__init__". Note: entry point — charge les 6 fichiers + le dump, calcule une signature (uid, mtime+taille) qui clé le cache des calculs dérivés.

Create a box labeled "wmap". Note: poids par style, dérivés de taste_categories.

Create a box labeled "ascore". Note: score artiste 0-100, combine 6 signaux (manual/corpus/collection/graph/djset/label_link) pondérés et normalisés.

Create a box labeled "graph_rescore". Note: retransforme les arêtes brutes du graphe en scores, selon les catégories et poids COURANTS.

Create a box labeled "label_db_signal". Note: un artiste Coeur/Aimé a-t-il un disque chez ce label ? (référentiel local + graphe).

Create a box labeled "reco_rows". Note: classement des labels candidats, fusion de 5 signaux normalisés.

Create a box labeled "album_score". Note: la fonction la plus appelée — combine label + artiste + style, rétrécit vers un a priori neutre (K=0.35, PRIOR=45).

Create a box labeled "Routes web". Note: radar_web/app.py — Recherche, Discographie, Nouveautés, Univers (labels/artistes), Mes sets ; un Ctx() par requête.

Create a box labeled "Jobs consommateurs". Note: job_scan_recos (album_score, filtre RECOS RADAR) et job_scan_catalog (ascore via sellers.seller_affinity).

Create a box labeled "Boucle de feedback (learn.py)". Note: votes pouce haut/bas -> régression logistique maison -> proposition de poids, jamais appliquée seule.

Draw an arrow from Jobs de fond (ingestion, fetch_collection, profile_labels, canonicalize) to taste_corpus.json. Label it "écrit".

Draw an arrow from Jobs de fond (ingestion, fetch_collection, profile_labels, canonicalize) to collection_cache.json. Label it "écrit".

Draw an arrow from Jobs de fond (ingestion, fetch_collection, profile_labels, canonicalize) to Profils & résolutions. Label it "écrit".

Draw an arrow from build_graph (job mensuel) to producer_graph.json. Label it "écrit (arêtes brutes)".

Draw an arrow from Utilisateur (Réglages) to crate_radar_config.json. Label it "écrit (poids, catégories, base)".

Draw an arrow from crate_radar_config.json to Ctx.__init__. Label it "poids + config".

Draw an arrow from taste_corpus.json to Ctx.__init__. Label it "lu (cache mtime)".

Draw an arrow from collection_cache.json to Ctx.__init__. Label it "lu (cache mtime)".

Draw an arrow from Profils & résolutions to Ctx.__init__. Label it "lu (cache mtime)".

Draw an arrow from producer_graph.json to Ctx.__init__. Label it "lu (cache mtime)".

Draw an arrow from Dump Discogs SQLite to Ctx.__init__. Label it "signature cache + requêtes en lecture".

Draw an arrow from Ctx.__init__ to wmap. Label it "poids par style".

Draw an arrow from Ctx.__init__ to graph_rescore. Label it "arêtes brutes + poids courants".

Draw an arrow from wmap to ascore. Label it "signal manual/tiers".

Draw an arrow from graph_rescore to ascore. Label it "terme graph".

Draw an arrow from graph_rescore to label_db_signal. Label it "score graphe des labels".

Draw an arrow from ascore to reco_rows. Label it "label_artist_signal".

Draw an arrow from label_db_signal to reco_rows. Label it "signal structurel".

Draw an arrow from ascore to album_score. Label it "terme artiste".

Draw an arrow from reco_rows to album_score. Label it "terme label (reco_index)".

Draw an arrow from album_score to Routes web. Label it "note une sortie".

Draw an arrow from reco_rows to Routes web. Label it "labels candidats".

Draw an arrow from ascore to Routes web. Label it "note artiste".

Draw an arrow from album_score to Jobs consommateurs. Label it "job_scan_recos".

Draw an arrow from ascore to Jobs consommateurs. Label it "job_scan_catalog".

Draw an arrow from Routes web to Boucle de feedback (learn.py). Label it "vote pouce haut/bas".

Draw an arrow from Boucle de feedback (learn.py) to crate_radar_config.json. Label it "propose poids, écrit si l'utilisateur applique".

Mark Ctx.__init__ as entry point.

Mark Dump Discogs SQLite as external.

Mark Jobs de fond (ingestion, fetch_collection, profile_labels, canonicalize) as external.

Mark build_graph (job mensuel) as external.

---

## Notes
- Ce diagramme couvre uniquement le "cerveau" de scoring de Radar (`radar_web/radar/scoring.py`, classe `Ctx`) — pas le reste de l'application. Sont traités comme externes, en une seule boîte chacun, sans être détaillés : les jobs d'ingestion YouTube/Spotify/Bandcamp/DJ sets, `build_graph`, la mécanique de file de jobs elle-même, la construction du dump Discogs, et la couche web/HTMX (`radar_web/app.py`, templates Jinja) — représentée uniquement comme point de consommation, pas route par route.
- La flèche de "Boucle de feedback" vers `crate_radar_config.json` boucle le flux : une sauvegarde de poids (manuelle en Réglages, ou via "appliquer" la proposition) invalide automatiquement le cache de `Ctx` au prochain appel (signature de fichiers, pas de TTL) — ce rebouclage est la seule flèche qui remonte vers le haut du diagramme.
- `label_artist_signal` et `artist_label_signal` (signaux additionnels utilisés par `ascore`/`reco_rows`) ne sont pas dessinés comme boîtes séparées pour rester sous la limite d'éléments — ils sont mentionnés dans les notes de `ascore` et `reco_rows` respectivement.
- "Jobs consommateurs" regroupe `job_scan_recos` et `job_scan_catalog` (le seul autre appelant est `sellers.seller_affinity`, hors périmètre, appelé par `job_scan_catalog`).
