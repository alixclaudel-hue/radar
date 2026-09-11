# Diagram Brief: Système de scoring Radar (Lot 1 — labels Cœur/Aimé)

**Layout**: top-to-bottom
**Flow summary**: La config utilisateur (poids + catégorisation Cœur/Aimé) alimente le moteur `Ctx`, qui calcule les notes dérivées et les renvoie aux routes web et aux jobs de fond qui les consomment ou les écrivent en retour.

---

## Elements

Create a group labeled "Entrées : config utilisateur" that contains: Poids de scoring, Catégorisation Cœur/Aimé.

Create a box labeled "Poids de scoring". Note: `DEFAULT_SCORING` (store.py) — reco, album, artist_score, graph, sources, recos, fusionné via `deep_merge` à chaque chargement.

Create a box labeled "Catégorisation Cœur/Aimé". Note: `label_categories` / `artist_categories` / `taste_categories`, forme `{"1":[...],"2":[...]}` — Lot 1 : remplace les anciennes listes à plat `labels`/`watchlist`.

Create a group labeled "Ctx — moteur de scoring (scoring.py)" that contains: wmap / affinité de style, label_tier_map / artist_tier_map, ascore, graph_rescore, reco_rows / reco_index, album_score.

Create a box labeled "wmap / affinité de style". Note: style -> poids 0-1 depuis taste_categories + taste_tiers ; alimente affinity_score, style_affinity_of.

Create a box labeled "label_tier_map / artist_tier_map". Note: relit label_categories/artist_categories en {clé: "1"|"2"} — point d'entrée lecture du Lot 1.

Create a box labeled "ascore". Note: note 0-100 par artiste — 6 signaux pondérés (manual, corpus, collection, graph, djset, label_link).

Create a box labeled "graph_rescore". Note: proximité de co-crédits recalculée depuis les arêtes brutes du graphe (job_build_graph), pondérée par tier Cœur/Aimé.

Create a box labeled "reco_rows / reco_index". Note: note 0-100 par label — 5 signaux (collection, corpus, artist, affinity, db_link).

Create a box labeled "album_score". Note: note 0-100 d'une sortie/piste — combine label + artiste + style, repli vers un a priori neutre si peu de signaux disponibles.

Create a group labeled "Consommateurs" that contains: Routes univers/reco/settings, Jobs de fond.

Create a box labeled "Routes univers/reco/settings". Note: app.py — /univers/*, /reco/label, /reco/artist, /settings ; lisent et écrivent la config.

Create a box labeled "Jobs de fond". Note: crate_jobs.py — job_scan_veille, job_scan_recos, job_profile_labels, job_canonicalize, job_fetch_collection, job_merge_corpus.

Create a box labeled "Référentiel Discogs local". Note: discogs_dump.py, dump mensuel en SQLite — externe, interrogé mais pas construit ici.

Create a box labeled "API Discogs". Note: externe — appelée par les jobs, jamais par Ctx directement.

Create a box labeled "YouTube / ytcache". Note: externe — consommé par job_publish_recos, chaîné après job_scan_recos.

Create a box labeled "UI HTMX/Jinja". Note: externe — rendu des pages, hors périmètre de ce document.

Draw an arrow from Routes univers/reco/settings to Poids de scoring. Label it "POST /settings édite les poids".

Draw an arrow from Routes univers/reco/settings to Catégorisation Cœur/Aimé. Label it "ajoute / retire / déplace de tier".

Draw an arrow from Jobs de fond to Catégorisation Cœur/Aimé. Label it "nouveaux labels détectés -> Aimé par défaut".

Draw an arrow from Poids de scoring to wmap / affinité de style. Label it "taste_tiers".

Draw an arrow from Catégorisation Cœur/Aimé to wmap / affinité de style. Label it "taste_categories".

Draw an arrow from Catégorisation Cœur/Aimé to label_tier_map / artist_tier_map. Label it "label_categories / artist_categories".

Draw an arrow from Poids de scoring to ascore. Label it "artist_score, artist_tiers".

Draw an arrow from label_tier_map / artist_tier_map to ascore. Label it "tier manuel + label_link".

Draw an arrow from Référentiel Discogs local to ascore. Label it "artist_label_signal (croisement style)".

Draw an arrow from label_tier_map / artist_tier_map to graph_rescore. Label it "seed_category_weight".

Draw an arrow from Poids de scoring to reco_rows / reco_index. Label it "scoring.reco".

Draw an arrow from label_tier_map / artist_tier_map to reco_rows / reco_index. Label it "labels suivis (Cœur+Aimé)".

Draw an arrow from Référentiel Discogs local to reco_rows / reco_index. Label it "label_affinities, label_db_signal".

Draw an arrow from ascore to album_score. Label it "note artiste crédité".

Draw an arrow from reco_rows / reco_index to album_score. Label it "note label de la sortie".

Draw an arrow from wmap / affinité de style to album_score. Label it "style_affinity_of".

Draw an arrow from album_score to Routes univers/reco/settings. Label it "note affichée (recherche, univers)".

Draw an arrow from album_score to Jobs de fond. Label it "job_scan_recos note les candidats".

Draw an arrow from graph_rescore to Routes univers/reco/settings. Label it "suggestions (reco/labels, reco/artists)".

Draw an arrow from Jobs de fond to API Discogs. Label it "profile_labels, canonicalize, fetch_collection".

Draw an arrow from Jobs de fond to YouTube / ytcache. Label it "job_scan_recos chaîne job_publish_recos".

Draw an arrow from Routes univers/reco/settings to UI HTMX/Jinja. Label it "rendu des pages".

Mark Référentiel Discogs local as external.

Mark API Discogs as external.

Mark YouTube / ytcache as external.

Mark UI HTMX/Jinja as external.

Mark Routes univers/reco/settings as entry point.

---

## Notes
- Périmètre : ce diagramme couvre `radar_web/radar/scoring.py`, `radar_web/radar/store.py`, et la partie de `app.py`/`crate_jobs.py` qui lit ou écrit la catégorisation labels/artistes et les poids de scoring. Le référentiel Discogs local, l'API Discogs, YouTube/ytcache et le rendu HTMX/Jinja sont traités comme externes (boîte unique, non détaillés) — de même que le worker/scheduling (qui décide QUAND lancer un job, non représenté séparément ici).
- **Changement Lot 1** (à faire ressortir visuellement, ex. couleur ou bordure distincte sur la boîte "Catégorisation Cœur/Aimé") : les 2 anciennes listes à plat `labels` (base) et `watchlist` (veille nouveautés) sont remplacées par `label_categories = {"1":[...],"2":[...]}`, même modèle que `artist_categories`. Conséquence comportementale : `job_scan_veille` scanne désormais TOUS les labels suivis (Cœur ET Aimé) pour les nouveautés, alors qu'avant seule `watchlist` était scannée.
- `job_build_graph` (dans "Jobs de fond") ne lit PAS la catégorisation labels pour choisir ses graines — seulement `artist_categories` + le corpus. Ne pas dessiner de flèche "Catégorisation Cœur/Aimé -> job_build_graph" : ce serait inexact d'après le document source.
- `graph_rescore` a aussi une flèche entrante implicite depuis le graphe brut (`producer_graph.json`, écrit par `job_build_graph`) — non représentée comme boîte séparée pour rester sous la limite d'éléments ; mentionné ici pour ne pas perdre l'information.
