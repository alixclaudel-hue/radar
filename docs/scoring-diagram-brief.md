# Diagram Brief: Interactions entre les scores (scoring.py)

**Layout**: top-to-bottom
**Flow summary**: Montre comment les entrées de goût/comportement (styles suivis, artistes Cœur/Aimés, corpus d'écoute, collection Discogs, graphe de co-crédits) se combinent en signaux intermédiaires, puis en trois scores finaux — ascore (artiste), reco_rows (label) et album_score (disque/piste) — et comment ces trois scores finaux se ré-alimentent entre eux.

---

## Elements

Create a group labeled "Entrees (goût / comportement)" that contains: Poids de Style (wmap), Artistes Coeur-Aimes (manuel), Corpus (ecoutes), Collection Discogs, Graphe de co-credits (brut).

Create a group labeled "Signaux intermediaires" that contains: Seed Category Weight, Graph Rescore, Affinite Style Label, Affinite Style Morceau, Artiste-vers-Label Signal, Label-vers-Artiste Signal, Label DB Signal, Corpus Label Scores.

Create a group labeled "Scores finaux" that contains: Ascore (score artiste), Reco Rows (score label), Album Score (score disque/piste).

Create a box labeled "Poids de Style (wmap)". Note: Ctx._compute_wmap, poids par style depuis les categories de gout de la config.

Create a box labeled "Artistes Coeur-Aimes (manuel)". Note: Ctx.artist_tier_map, categories artiste saisies a la main dans les Reglages.

Create a box labeled "Corpus (ecoutes)". Note: taste_corpus.json, pistes YouTube/Spotify/Bandcamp/DJ sets ingerees.

Create a box labeled "Collection Discogs". Note: collection_cache.json, disques possedes + wantlist.

Create a box labeled "Graphe de co-credits (brut)". Note: producer_graph.json, aretes construites par le job build_graph (mode taste), pas un score en soi.

Create a box labeled "Seed Category Weight". Note: Ctx.seed_category_weight, poids par artiste-graine : Coeur/Aimes prioritaire, sinon meilleure source du corpus.

Create a box labeled "Graph Rescore". Note: Ctx.graph_rescore, proximite recalculee sur les aretes brutes ponderees par les graines - produit un sous-score artistes et un sous-score labels.

Create a box labeled "Affinite Style Label". Note: Ctx.label_affinities / affinity_score, 0-100, part du catalogue d'un label dans mes styles suivis.

Create a box labeled "Affinite Style Morceau". Note: Ctx.style_affinity_of, 0-100, styles d'une sortie/piste face a wmap.

Create a box labeled "Artiste-vers-Label Signal". Note: Ctx.artist_label_signal, l'artiste a-t-il un disque chez un label deja suivi, filtre par croisement de style sur la sortie precise.

Create a box labeled "Label-vers-Artiste Signal". Note: Ctx.label_artist_signal, labels reellement ecoutes dans le corpus, pondere par le score de chaque artiste credite.

Create a box labeled "Label DB Signal". Note: Ctx.label_db_signal, combine 0.65x lien direct (artistes aimes au catalogue local) + 0.35x Graph Rescore labels.

Create a box labeled "Corpus Label Scores". Note: Ctx.corpus_label_scores, poids par label selon la source d'ecoute (Bandcamp/collection > YouTube > DJ set).

Create a box labeled "Ascore (score artiste)". Note: Ctx.ascore, 0-100, moyenne ponderee normalisee de 6 termes: manuel, corpus, collection, graph, djset, label_link.

Create a box labeled "Reco Rows (score label)". Note: Ctx.reco_rows, 0-100 par label, moyenne ponderee normalisee de 5 termes: collection, corpus, artist, affinity, db_link.

Create a box labeled "Album Score (score disque/piste)". Note: Ctx.album_score, 0-100 par sortie/piste, moyenne de label + artiste + style, retrecie vers un a priori neutre (45) si peu de termes disponibles.

Draw an arrow from Poids de Style (wmap) to Affinite Style Label. Label it "poids par style".

Draw an arrow from Poids de Style (wmap) to Affinite Style Morceau. Label it "poids par style".

Draw an arrow from Poids de Style (wmap) to Artiste-vers-Label Signal. Label it "croisement de style (garde-fou)".

Draw an arrow from Artistes Coeur-Aimes (manuel) to Seed Category Weight. Label it "poids prioritaire des graines".

Draw an arrow from Artistes Coeur-Aimes (manuel) to Ascore (score artiste). Label it "terme manuel".

Draw an arrow from Artistes Coeur-Aimes (manuel) to Label DB Signal. Label it "artistes aimes -> labels au catalogue (lien direct)".

Draw an arrow from Corpus (ecoutes) to Seed Category Weight. Label it "meilleure source si non Coeur/Aimes".

Draw an arrow from Corpus (ecoutes) to Ascore (score artiste). Label it "terme corpus + terme DJ sets".

Draw an arrow from Corpus (ecoutes) to Label-vers-Artiste Signal. Label it "labels reellement ecoutes".

Draw an arrow from Corpus (ecoutes) to Corpus Label Scores. Label it "poids par source d'ecoute".

Draw an arrow from Collection Discogs to Ascore (score artiste). Label it "terme collection".

Draw an arrow from Collection Discogs to Reco Rows (score label). Label it "terme collection/want".

Draw an arrow from Graphe de co-credits (brut) to Graph Rescore. Label it "aretes pesees par graine".

Draw an arrow from Seed Category Weight to Graph Rescore. Label it "pondere chaque graine".

Draw an arrow from Graph Rescore to Ascore (score artiste). Label it "terme graph (proximite)".

Draw an arrow from Graph Rescore to Label DB Signal. Label it "composante graphe (35%)".

Draw an arrow from Artiste-vers-Label Signal to Ascore (score artiste). Label it "terme label_link".

Draw an arrow from Ascore (score artiste) to Label-vers-Artiste Signal. Label it "pondere chaque label par le score de l'artiste credite".

Draw an arrow from Ascore (score artiste) to Album Score (score disque/piste). Label it "terme artiste (credits piste)".

Draw an arrow from Label-vers-Artiste Signal to Reco Rows (score label). Label it "terme artist".

Draw an arrow from Label DB Signal to Reco Rows (score label). Label it "terme db_link".

Draw an arrow from Affinite Style Label to Reco Rows (score label). Label it "terme affinity".

Draw an arrow from Corpus Label Scores to Reco Rows (score label). Label it "terme corpus".

Draw an arrow from Reco Rows (score label) to Album Score (score disque/piste). Label it "terme label (via reco_index)".

Draw an arrow from Affinite Style Morceau to Album Score (score disque/piste). Label it "terme style".

Mark Corpus (ecoutes) as database.

Mark Collection Discogs as database.

Mark Graphe de co-credits (brut) as database.

Add a note to Ascore (score artiste): "Reinjecte dans Label-vers-Artiste Signal -> boucle artiste -> label -> reco_rows".

Add a note to Album Score (score disque/piste): "Score final consomme par recherche, veille, RECOS RADAR et Mes sets (djset_rows)".

---

## Notes
- Trois "boucles" a noter visuellement si possible : (1) Ascore -> Label-vers-Artiste Signal -> Reco Rows -> Album Score (un artiste aime tire aussi le label et la sortie) ; (2) Graph Rescore alimente a la fois Ascore (cote artistes) et Label DB Signal (cote labels) ; (3) wmap alimente trois points d'entree distincts (affinite label, affinite morceau, garde-fou de style sur artist_label_signal) plutot qu'un seul.
- "Graphe de co-credits (brut)" represente les aretes stockees par le job build_graph (mode taste) — ce n'est pas un score calcule par scoring.py, seulement son entree ; garde comme boite distincte de Graph Rescore pour ne pas confondre donnee brute et score derive.
- Les poids de ponderation (ex. w_manual=0.5, w_corpus=0.18...) et le mecanisme de retrecissement vers un a priori neutre (album_score, K=0.35/PRIOR=45) sont volontairement omis du diagramme (detail numerique, pas relation structurelle) — a mentionner en legende si le diagramme est presente a quelqu'un d'autre.
