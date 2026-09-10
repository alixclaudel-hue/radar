# Radar - le "cerveau" de scoring - vue d'ensemble

## Perimetre de ce document

Ce document couvre UNIQUEMENT le moteur de notation gout de Radar : le code qui
repond a la question "est-ce que ce disque / cet artiste / ce label est dans mon
gout ?". C'est un sous-systeme dans une appli plus large (Radar = outil perso de
crate-digging vinyle base sur Discogs, avec ingestion d'ecoute, scan de
nouveautes, playlist YouTube "RECOS RADAR", etc.) - ces autres parties ne sont
decrites ici que pour situer les points d'entree/sortie du moteur de notation.

**Dedans** :
- `radar_web/radar/scoring.py` en entier - la classe `Ctx` et toutes ses methodes
  (`album_score`, `ascore`, `reco_rows`, `graph_rescore`, `artist_label_signal`,
  `label_db_signal`, `label_artist_signal`, `affinity_score`, `label_affinities`,
  `style_affinity_of`, `seed_category_weight`, `djset_rows`, `stats`, etc.)
- les poids par defaut et le format de la config de scoring (`DEFAULT_SCORING`
  dans `radar_web/radar/store.py`)
- la boucle d'apprentissage par feedback qui ajuste ces poids (`radar_web/radar/learn.py`)

**Dehors** (traite comme externe, decrit seulement "vu depuis le scoring") :
- `radar_web/radar/discogs_dump.py` - l'index SQLite du dump mensuel Discogs
  (catalogue). Le scoring l'interroge (`label_style_counts`, `label_ids_for_artists`,
  `artist_ids_for_labels`, `search_local`) mais ne construit ni ne maintient cet
  index.
- `crate_jobs.py` et la mecanique de file de jobs (`radar_web/radar/jobs.py`) -
  l'orchestration, la reprise, les statuts. Seules les DEUX lignes ou un job
  appelle reellement dans `Ctx` sont couvertes ici (`job_scan_recos` ->
  `Ctx.album_score`, `job_scan_catalog` -> `sellers.seller_affinity` -> `Ctx.ascore`).
  `job_build_graph`, qui alimente le fichier que `Ctx.graph_rescore` relit, est
  decrit comme fournisseur de donnees, pas comme partie du scoring.
- l'ingestion YouTube/Spotify/Bandcamp/DJ sets (remplissage de `taste_corpus.json`)
  - le scoring lit ce fichier, ne l'ecrit jamais.
- la couche web/HTMX (`radar_web/app.py`, templates Jinja) - seulement listee ici
  comme table des appelants, pas expliquee route par route.

Si une notion citee ici (dump Discogs, jobs, ingestion) merite plus de detail,
elle a sa propre documentation ailleurs (`CLAUDE.md`, `docs/architecture.md`,
`claude_archive.md`) - ce document n'y revient pas.

---

## 1. En une phrase

Le "cerveau" transforme des signaux epars sur ce que l'utilisateur ecoute, possede
et a classe "Coeur/Aime" en une note sur 100 pour n'importe quel disque, artiste ou
label Discogs - recalculee a la volee a chaque page vue, jamais stockee comme
verite figee, a partir d'une poignee de fichiers JSON (et d'un index SQLite en
lecture seule) mis a jour par des jobs de fond.

## 2. Pourquoi ce n'est pas un "modele ML" au sens classique

Pas d'entrainement en arriere-plan, pas de poids appris par defaut. Le score est
une **moyenne ponderee de signaux**, avec des poids par defaut regles a la main
(`DEFAULT_SCORING`, `radar_web/radar/store.py`) et ajustables par l'utilisateur
dans Reglages. Une seule brique fait vraiment de l'apprentissage supervise (une
regression logistique maison de 40 lignes, `learn.py`) qui propose de nouveaux
poids a partir des votes pouce haut/bas - mais elle ne s'applique jamais toute
seule : l'utilisateur doit cliquer "appliquer" la proposition.

---

## 3. Le fichier central : `radar_web/radar/scoring.py`

```
radar_web/radar/scoring.py
  ARTIST_STOPWORDS         ->  noms a ignorer partout ("various", "dj", ...)
  track_row_id(r)          ->  id stable d'une ligne de corpus DJ set (pour la
                                supprimer precisement depuis Mes sets)
  _robust_scale(counts)    ->  p95 des valeurs (jamais le max) - echelle robuste
  _log_ratio(n, scale)     ->  log1p(n)/log1p(scale), plafonne a 1
  _files_sig(*paths)       ->  signature (mtime, taille) de fichiers, pour le cache
  _DERIVED / _DERIVED_MAX  ->  cache process-wide des calculs derives, cle par
                                signature de fichiers (8 signatures max, purge simple)

  class Ctx
    __init__(uid)           ->  charge tous les fichiers JSON de cet utilisateur
                                 + calcule la signature de cache
    _memo(name, compute)    ->  memoise un calcul sous la signature courante

    # gout / styles
    wmap                     (property, memo)  poids par style, depuis taste_categories
    affinity_score(entry)                       affinite 0-100 + couverture, sur un
                                                  {style_counts}
    label_affinities(label_keys)                 affinite par label (dump Discogs
                                                  prioritaire, profil API en repli)
    style_affinity_of(styles)                    affinite moyenne sur une liste de styles

    # artistes
    canon_artist_key(name) / canon_artist_name(name)  normalisation -> "id:<discogs_id>"
                                                        si resolu, sinon nom normalise
    artist_tier_map()                            {cle: "1"|"2"} Coeur/Aime
    seed_category_weight()          (memo)        poids d'une graine du graphe selon
                                                    sa provenance (Coeur/Aime > source
                                                    corpus > plancher)
    artist_disp()                                 {cle: nom d'affichage}
    djset_rows()                                   lignes DJ set notees, groupees
                                                    par DJ puis par video
    graph_rescore()                  (memo)        relit les arretes brutes du graphe
                                                    et recalcule un score de proximite
                                                    pour artistes ET labels
    ascore                (property, memo)          {cle: 0-100} - score artiste final
    corpus_by_source() / stats()                    compteurs pour la page Mes sources
    split_credit_artists(s)                         decoupe un credit multi-artiste

    # labels / reco
    corpus_label_scores()                          poids par label depuis le corpus ecoute
    label_artist_signal()                          labels ecoutes pondere par score artiste
    label_db_signal() / label_db_names()  (memo)    labels ou un artiste Coeur/Aime a
                                                     un disque (referentiel local, 2 voies)
    artist_label_signal()               (memo)      inverse : l'artiste a-t-il un disque
                                                      chez un label suivi ? (croise le style)
    reco_index          (property, memo)             {label_key: score} - vue rapide de
                                                       reco_rows() pour album_score
    reco_rows()                          (memo)       classement complet des labels
                                                       candidats (hors base ou pas)

    # score album
    album_score(r)                                   LA fonction consommee partout :
                                                       {label,title,style} -> (score, detail)

def real_tracks(tracklist)      filtre les pseudo-pistes ("Side A", vinyle vide, ...)
def yt_search_url(query)        construit une URL de recherche YouTube (utilitaire)
```

---

## 4. `Ctx` : instantane de donnees, reconstruit a chaque requete

`Ctx(uid=None)` est cree a chaque route qui en a besoin (`c = Ctx()` dans
`radar_web/app.py`, ou `Ctx(uid=RADAR_UID)` / `Ctx(uid="owner")` dans un job). A la
construction :

1. il resout l'utilisateur courant (`store.current_uid()` si `uid` absent) et ses
   chemins de fichiers (`paths.user_paths`) ;
2. il lit la config (`store.read_config`) et en extrait `self.scoring` (les poids) ;
3. il charge 6 fichiers JSON via `store.load_cached` (cache process-local invalide
   sur mtime+taille - relire un fichier qui n'a pas change ne coute rien) :
   `profile`, `collection`, `corpus`, `resolved`, `artists_res`, `graph` ;
4. il calcule une **signature** = `(uid, sig(config, graph, artists_res, corpus,
   collection, profile, discogs_dump.sqlite3))`.

Cette signature est la cle du cache `_DERIVED` (dictionnaire module-level, partage
par tout le process, jusqu'a 8 signatures gardees). Tant qu'aucun de ces fichiers
n'a bouge, deux `Ctx()` crees dans la meme requete (ou des requetes qui se suivent)
reutilisent les memes `wmap`, `ascore`, `graph_rescore()`, `reco_rows()` deja
calcules - reconstruire un `Ctx` est donc bon marche, MAIS le premier appel apres
qu'un job a ecrit un fichier (ou que l'utilisateur a sauvegarde ses poids dans
Reglages) recalcule tout depuis zero, sans intervention manuelle.

Constrution < 3 Mo de JSON par utilisateur (commentaire du code) - le cout reel
est dans les calculs derives memoises, pas dans le chargement des fichiers.

---

## 5. Les donnees en entree

| Fichier (par utilisateur, `<DATA>/users/<uid>/...`) | Contenu | Qui l'ecrit | Ce que `Ctx` en fait |
|---|---|---|---|
| `crate_radar_config.json` (`self.cfg`) | labels suivis (`labels`), veille (`watchlist`), categories de gout (`taste_categories`), categories d'artistes (`artist_categories`, Coeur/Aime), poids de scoring (`scoring`, voir section 7) | l'utilisateur (Reglages, Mes labels & artistes) | source de `wmap`, `artist_tier_map`, tous les poids |
| `labels_profile.json` (`self.profile`) | par label : `style_counts` (repartition des styles vus, echantillon API historique) | job `profile_labels` (hebdo) | repli de `label_affinities` quand le label est absent du dump local |
| `collection_cache.json` (`self.collection`) | `label_counts`, `want_label_counts`, `label_ids`, `artist_counts` - disques possedes/en wantlist Discogs | job `fetch_collection` | `reco_rows` (poids "collection"), `artist_disp`, `ascore` (poids "collection") |
| `taste_corpus.json` (`self.corpus`) | liste de lignes `{source, artist, title, label, style, dj, video, added_at, ...}` - tout ce qui a ete ecoute (YouTube/Spotify/Bandcamp/DJ sets) | jobs d'ingestion (`ingest_youtube/spotify/bandcamp/djsets`) | `ascore` (poids "corpus"/"djset"), `corpus_label_scores`, `label_artist_signal`, `djset_rows`, graines du graphe |
| `labels_resolved.json` (`self.resolved`) | nom Discogs canonique par label suivi + statut (`exact`/`approx`/`not_found`) | job `canonicalize` (hebdo) | affichage des noms de label, statuts "a verifier" |
| `artists_resolved.json` (`self.artists_res`) | idem, pour les artistes (+ `discogs_id`) | job `resolve_artists` / `canonicalize` | `canon_artist_key`/`canon_artist_name` - la cle canonique `id:<discogs_id>` est ce qui permet de fusionner alias et graphe |
| `producer_graph.json` (`self.graph`) | **aretes brutes** : `{edges: {artiste_cle: {name, id, co: {graine_cle: {n, rw}}}}, label_edges: {label_cle: {name, co: {graine_cle: n}}}, seeds: {graine_cle: nom}}` | job `build_graph` (mensuel, mode `taste`) | `graph_rescore()` retransforme ces comptes bruts en scores, a chaque requete, selon les categories et poids COURANTS (donc jamais besoin de relancer le job apres avoir change un poids) |
| `discogs_dump.sqlite3` (partage, hors `users/`) | index SQLite du dump mensuel officiel Discogs (releases, artistes, labels, styles) | import mensuel (`radar/discogs_dump.py`, hors perimetre) | interroge en lecture seule via `discogs_dump.label_style_counts`, `label_ids_for_artists`, `artist_ids_for_labels`, `search_local` - jamais ecrit par le scoring |

Tous ces fichiers sont relus a chaque requete (`store.load_cached`), mais le cout
reel est absorbe par le cache mtime : rien n'est jamais recharge tant qu'un job ou
une sauvegarde de config n'a pas ecrit dedans.

---

## 6. Les fonctions de score - ce qu'elles calculent et pourquoi

### `affinity_score(entry)` -> (affinite 0-100 ou `None`, couverture 0-100)
Prend `{style_counts}` (repartition brute des styles d'un label ou d'une sortie),
ne compte que les styles presents dans `wmap` (les categories de gout de
l'utilisateur), et exclut le reste du calcul plutot que de le compter comme 0 -
un style "non classe" n'est pas la meme chose qu'un style "deteste". La part
exclue redevient visible a part, dans `coverage`.

### `label_affinities(label_keys)` -> `{cle: {aff, coverage}}`
Pour chaque label : priorite au profil materialise depuis le dump Discogs local
(exhaustif sur tout le catalogue vinyle importe), repli sur `labels_profile.json`
(echantillon API) si le label est absent du dump. `aff=None` si le label n'a
jamais ete profile par aucune des deux sources - distinct d'une affinite basse
mais reelle.

### `ascore` (property memoisee) -> `{cle: 0-100}`, le score artiste
Combine 6 signaux, chacun ramene a 0-1 puis pondere (poids `scoring.artist_score`,
par defaut `manual .5 / corpus .18 / collection .1 / graph .14 / djset .08 /
label_link .15`), le tout **normalise par la somme des poids reellement en jeu**
(pas une constante fixe - ajouter un terme rééquilibre les poids relatifs au lieu
de faire deriver le plafond au-dela de 100) :
- `manual` : poids du palier Coeur/Aime (`scoring.artist_tiers`)
- `corpus` : nombre d'ecoutes hors DJ set, ramene par `_log_ratio` a l'echelle
  robuste (p95) du corpus
- `collection` : nombre de disques possedes de cet artiste
- `graph` : score de proximite du graphe de co-credits (`graph_rescore()`)
- `djset` : nombre de passages en DJ set, compte a part du corpus general (sinon
  double compte)
- `label_link` : l'artiste a-t-il un disque chez un label deja suivi ?
  (`artist_label_signal()`)

### `graph_rescore()` (memoisee) -> `{"artists": {...}, "labels": {...}}`
Relit les aretes BRUTES stockees par le job `build_graph` (comptes de co-credits
par graine, poids de role) et recalcule, a partir des categories/poids COURANTS
(`scoring.graph` : `tier_w`, `artist_breadth`, `label_breadth`, `cat1_bonus`), un
score de proximite par artiste et par label candidat. C'est cette separation
(aretes brutes figees par le job, score recalcule a la volee) qui permet de
changer un poids dans Reglages sans avoir a relancer le job de construction du
graphe (couteux, appels API/SQL).

### `artist_label_signal()` (memoisee) -> `{cle canonique: 0-1}`
"Cet artiste a-t-il une sortie chez un label deja suivi (base ou veille) ?" -
interroge `discogs_dump.artist_ids_for_labels` (referentiel local). Depuis le
correctif du 2026-09-10, ne compte un credit que si au moins un style de LA
SORTIE PRECISE est dans le gout courant (`wmap`) - sinon un featuring hors-style
sur un label par ailleurs dans le gout suffisait a booster n'importe quel artiste.
Une sortie sans style renseigne reste comptee telle quelle (indecidable, pas
rejetee a tort).

### `label_db_signal()` / `label_db_names()` (memoisees) -> `{cle: 0-1}` / `{cle: nom}`
Miroir cote label : "un artiste Coeur/Aime a-t-il un disque chez ce label ?".
Combine deux sources du referentiel local, ponderees 0.65/0.35 : le lien DIRECT
(`discogs_dump.label_ids_for_artists`, uniquement Coeur/Aime, disponible sans
job) et le GRAPHE (`graph_rescore()["labels"]`, plus large mais dependant du
dernier `build_graph`).

### `label_artist_signal()` -> `{label_norm: (somme_scores/100, n)}`
Distinct de `label_db_signal` : ne regarde que les labels reellement ECOUTES
(corpus), pondere par le score de l'artiste credite - signal comportemental
etroit mais fiable, complementaire du signal structurel large de `label_db_signal`.

### `reco_rows()` (memoisee) -> liste de labels candidats, triee par score
Fusionne 5 signaux (collection possedee+wantlist, corpus ecoute, `label_artist_signal`,
`affinity_score`, `label_db_signal`), chacun normalise par son propre maximum
observe, puis combine avec les poids `scoring.reco` (`collection .6 / corpus .5 /
artist .4 / affinity .4 / db_link .35`), encore normalise par la somme des poids
en jeu. Univers couvert : labels de la base, labels ecoutes, labels d'un artiste
aime, labels du referentiel local ou un artiste aime a une sortie - pas seulement
la base explicite.

### `album_score(r)` -> `(score 0-100 ou None, detail {label, artist, style})`
**La fonction la plus appelee du fichier** - prend une sortie sous forme
`{label: [...], title: "Artiste - Titre", style: [...]}` et combine jusqu'a 3
termes (poids `scoring.album` : `label .4 / artist .4 / style .2`) :
- `label` : le meilleur score de `reco_index` (= `reco_rows()`) parmi les labels
  credites
- `artist` : melange (`artist_max_vs_mean`, defaut 0.6) du meilleur `ascore` et de
  la moyenne des `ascore` des artistes crédités, extraits du titre par
  `split_credit_artists`
- `style` : `style_affinity_of` sur les styles de la sortie

Un terme absent (label inconnu, aucun artiste identifie, pas de style) est
simplement exclu, pas compte comme 0. Puis **retrecissement vers un a priori
neutre** : `score = (somme(poids*val) + K*PRIOR) / (somme(poids) + K)` avec
`K=0.35, PRIOR=45` - un score fonde sur un seul critere disponible ne s'affiche
plus avec la meme confiance apparente qu'un score fonde sur les trois.

---

## 7. Les poids : `DEFAULT_SCORING` (`radar_web/radar/store.py`)

```
taste_tiers    : poids par palier de style (Coeur/Aime/autre)      {"1":1.0,"2":.6,"3":.3}
artist_tiers   : poids par palier d'artiste (Coeur/Aime)            {"1":1.0,"2":.5}
reco           : poids du score label (section 6, reco_rows)        collection/corpus/artist/affinity/want_factor/db_link
album          : poids du score album (section 6, album_score)      label/artist/style/artist_max_vs_mean
artist_score   : poids du score artiste (section 6, ascore)         manual/corpus/collection/graph/djset/label_link
graph          : parametres du graphe de co-credits (job ET rescore) tier_w, breadth, cat1_bonus, roles, max_levels, node_cap...
sources        : poids par source du corpus (a l'ecoute)            discogs_collection 1.0 / bandcamp .9 / youtube .5 / spotify .5 / discogs_want .6 / djset .4
recos          : seuil et volume pour RECOS RADAR (job_scan_recos)  min_score 60, max_new_releases 20
label_affinity_floor : seuil plancher d'affinite pour reco_rows      0 (desactive par defaut)
learn          : parametres de la regression logistique de learn.py  l2, min_feedback, min_per_class
```

Ces poids sont modifiables dans la page Reglages (7 groupes de champs), stockes
dans `config["scoring"]` (fusion avec `DEFAULT_SCORING` via `deep_merge` a chaque
lecture de config - un champ absent recupere sa valeur par defaut sans migration
explicite). Toute sauvegarde change la mtime de `crate_radar_config.json`, ce qui
invalide automatiquement le cache `_DERIVED` de `Ctx` au prochain appel.

### La boucle de feedback (`radar_web/radar/learn.py`)
Chaque vote pouce haut/bas sur un album ou un label (`POST /feedback`) enregistre
`{kind, key, verdict, score_shown, feat}` dans `reco_feedback.json`, ou `feat` est
le detail deja calcule par `album_score`/`reco_rows` (les memes cles : `label`,
`artist`, `style` ou `collection`, `corpus`, `artist`, `affinity`). Des qu'il y a
assez de votes (`min_feedback=12`, au moins 3 par classe), `learn.summary()` fait
tourner une petite regression logistique (`fit()`, descente de gradient a la
main, 600 iterations, regularisation L2 vers les poids actuels comme a priori) et
propose de nouveaux poids. L'utilisateur voit la proposition dans Reglages et
choisit de l'appliquer ou non (`POST /settings/learn/apply` ecrit directement dans
`config["scoring"]`) - **rien n'est jamais applique automatiquement**.

---

## 8. Comment les donnees circulent - vue d'ensemble

```
                     JOBS DE FOND (crate_jobs.py, hors perimetre)
  ingestion YouTube/Spotify/Bandcamp/DJ sets --> taste_corpus.json
  fetch_collection                            --> collection_cache.json
  profile_labels (hebdo)                      --> labels_profile.json
  canonicalize (hebdo)                        --> labels_resolved.json / artists_resolved.json
  build_graph (mensuel, mode "taste")         --> producer_graph.json  (ARETES BRUTES seulement)
  utilisateur (Reglages / Mes labels)         --> crate_radar_config.json (poids, categories, base)
                              |
                              v  (fichiers relus sur mtime, jamais push)
                    +-------------------+
                    |   Ctx(uid) __init__|   charge tout, calcule une signature
                    +-------------------+
                              |
                              v  (methodes memoisees sous cette signature)
        wmap -> ascore -> graph_rescore -> label_db_signal -> reco_rows -> album_score
                              |
                +-------------+--------------+
                |                            |
                v                            v
     routes radar_web/app.py         jobs qui notent des sorties
     (Recherche, Discographie,       (job_scan_recos -> album_score,
      Nouveautes, Mes labels &        job_scan_catalog -> sellers.seller_affinity
      artistes, Mes sets)                    -> ascore)
                |
                v
          templates Jinja -> page HTML/HTMX vue par l'utilisateur
                |
                v (vote pouce haut/bas)
          reco_feedback.json --> learn.summary() --> proposition de poids
                |                                          |
                +------------------ (clic "appliquer") ----+
                                     |
                                     v
                        crate_radar_config.json modifie
                        (invalide le cache Ctx au prochain appel)
```

Point cle : le score n'est **jamais stocke comme verite** a part dans deux
endroits transitoires - `recos_candidates.json`/`recos_playlist.json` (un
`album_score` fige au moment du scan, pour RECOS RADAR, qui n'est pas recalcule
tant que la sortie n'est pas rescannee) et `search_results.json`/l'historique de
recherche (un instantane de resultats notes, pour pouvoir rejouer une recherche
passee sans re-interroger Discogs). Partout ailleurs (Recherche live,
Discographie, Nouveautes, Mes labels & artistes), le score affiche est recalcule
a l'instant de la requete.

---

## 9. Qui appelle dans le scoring

### Routes web (`radar_web/app.py`) - toutes creent un `Ctx()` par requete

| Route (nom de fonction) | Methodes `Ctx` utilisees | Pour quoi |
|---|---|---|
| `POST /search` (`search_run`) via `_scored_rows` | `album_score` | note chaque sortie trouvee (dump local ou API Discogs) |
| `GET /disco` (`disco_page`) via `_scored_rows` | `album_score` | note la discographie d'un artiste/label |
| `GET /inbox/{veille,sellers}` (`_inbox`) | `album_score` | note les nouveautes detectees par les regles de veille / le scan vendeurs, filtre par seuil `mins` |
| `GET /univers/reco/labels` (`reco_labels_frag`) | `reco_rows`, `graph_rescore` | labels candidats hors base + candidats du graphe |
| `GET /univers/reco/artists` (`reco_artists_frag`) | `graph_rescore`, `ascore` | artistes candidats par proximite de graphe, tries par note affichee |
| `GET /univers/labels/table`, `_base_labels_ranked`, `_pick_base_labels` | `reco_index`, `label_affinities` | classement des labels de la base, pour le tableau et pour pre-remplir "mes meilleurs labels" en recherche |
| `GET /univers/artists/*` (table, recherche) | `artist_disp`, `artist_tier_map`, `ascore` | tableau des artistes identifies avec leur note |
| `/patte/djset/*` (Mes sets) | `djset_rows` | note chaque piste de chaque DJ set scanne |
| `GET /patte` (Mes sources) | `stats`, `scoring` (lecture) | compteurs de la page d'accueil |
| `GET/POST /settings` | `scoring` (lecture/ecriture via config) | edition des poids par defaut |
| `POST /feedback`, `GET /settings/learn`, `POST /settings/learn/apply` | (indirect, via `learn.py`) | boucle de feedback -> proposition/ecriture de nouveaux poids |

### Jobs de fond (`crate_jobs.py`) - les deux seuls a appeler dans `Ctx`

| Job | Appel | Pour quoi |
|---|---|---|
| `job_scan_recos` | `Ctx(uid=RADAR_UID).album_score(r)` sur chaque sortie des labels suivis (via `discogs_dump.search_local`) | filtre les candidats de la playlist RECOS RADAR au-dessus de `scoring.recos.min_score` |
| `job_scan_catalog` | `Ctx(uid="owner")` passe a `sellers.seller_affinity(inv, ctx)` (`radar_web/radar/sellers.py`, hors perimetre) qui lit `ctx.ascore` et `ctx.split_credit_artists` | note l'affinite gout du stock 12" d'un vendeur, pour prioriser les rescans |

`job_build_graph` n'appelle PAS dans `scoring.py` : il ecrit les aretes brutes du
graphe (`producer_graph.json`) que `Ctx.graph_rescore()` relit et retransforme en
scores a la demande - la separation est volontaire (cf. section 6).

---

## 10. Decisions de conception a connaitre (pas des bugs)

- **Echelle robuste (p95, pas max) + `log1p`** (`_robust_scale`, `_log_ratio`) :
  utilisee partout ou un comptage brut (ecoutes, co-credits, disques) est ramene
  a 0-1. Un seul artiste hyperactif (un alias qui revient 40 fois dans un DJ set,
  ou une graine tres prolifique dans le graphe) ne doit pas a lui seul deplacer
  l'echelle de tout le monde en tirant le maximum vers le haut.

- **Normalisation par la somme des poids EN JEU, jamais une constante fixe** :
  `ascore`, `reco_rows` et `album_score` divisent tous par `sum(poids des termes
  presents)`, pas par une somme fixe de tous les poids possibles. Ajouter un
  signal (ou en avoir un absent pour une entree donnee) rééquilibre les poids
  relatifs des autres au lieu de faire deriver le score au-dela de 100 ou de le
  faire baisser artificiellement.

- **Un signal absent (`None`) n'est jamais un 0** : `affinity_score` exclut les
  styles non classes du calcul plutot que de les compter comme "deteste" ;
  `album_score`/`reco_rows` excluent un terme sans donnee plutot que de le
  compter comme 0. Confondre "non classe" et "goût oppose" biaiserait tous les
  scores vers le bas des qu'une donnee manque (label jamais profile, artiste sans
  style connu, etc.).

- **Retrecissement vers un a priori neutre** (`album_score`, `K=0.35, PRIOR=45`) :
  un score fonde sur un seul critere disponible ne doit pas s'afficher avec la
  meme confiance visuelle (meme badge) qu'un score fonde sur les trois - le
  resultat est tire vers 45 d'autant plus fort que peu de poids est reellement en
  jeu.

- **Aretes de graphe brutes vs score de graphe recalcule** (`graph_rescore`) :
  `job_build_graph` (couteux - appels API/SQL, mensuel) stocke des comptes bruts
  de co-credits ; `Ctx.graph_rescore()` retransforme ces comptes en scores a
  chaque requete, selon les categories Coeur/Aime et les poids COURANTS. Changer
  un poids ou re-classer un artiste en Coeur prend effet immediatement, sans
  attendre le prochain `build_graph`.

- **Cache memoise par signature de fichiers, pas par TTL** (`_DERIVED`,
  `_files_sig`) : un `Ctx` recalcule ses valeurs derivees (couteuses : graphe,
  scores) une seule fois par etat reel des donnees (mtime+taille de 7 fichiers,
  dump SQLite inclus), jamais sur une horloge. Une sauvegarde de config ou
  l'ecriture d'un job invalide automatiquement le cache au prochain appel - rien
  a purger a la main.

- **Croisement de style entre `artist_label_signal` et le label suivi** (corrige
  le 2026-09-10, cf. `CLAUDE.md` piege 26) : partager un label suivi ne suffit
  plus a booster un artiste si la sortie precise creditee est hors du gout
  courant (ex. un featuring rap sur un label par ailleurs house) - seuls les
  styles DE LA SORTIE (pas le profil agrege du label) comptent. Une sortie sans
  style renseigne reste comptee (indecidable, pas rejetee a tort) - meme logique
  que pour `affinity_score`.

- **`label_artist_signal` et `label_db_signal` se recoupent volontairement** : le
  premier ne voit que le corpus reellement ecoute (comportemental, etroit,
  fiable) ; le second couvre tout le catalogue via le referentiel local
  (structurel, large, indirect). Un meme label peut apparaitre dans les deux -
  ce n'est pas une redondance a corriger.

- **`ascore` distingue le corpus general du corpus DJ set** (`corpus_c` vs
  `djset_c`, poids separes `corpus`/`djset`) : une ligne de corpus venant d'un DJ
  set n'est jamais comptee dans le terme "corpus" general, sinon elle pese deux
  fois.

- **La regression de `learn.py` ne s'applique jamais toute seule** : elle
  calcule une proposition (visible dans Reglages), l'utilisateur doit cliquer
  "appliquer" pour qu'elle ecrive dans `config["scoring"]`.

---

## 11. Fichiers cles

| Fichier | Role |
|---|---|
| `radar_web/radar/scoring.py` | le fichier central de ce document - classe `Ctx`, toutes les fonctions de score |
| `radar_web/radar/store.py` | `DEFAULT_SCORING` (poids par defaut), `load_config`/`save_config`, normalisation (`normalize_label`, `style_key`), cache fichier (`load_cached`) |
| `radar_web/radar/learn.py` | boucle de feedback pouce haut/bas -> proposition de nouveaux poids (regression logistique maison) |
| `radar_web/radar/paths.py` | chemins de tous les fichiers JSON par utilisateur (`_PER_USER`) et partages |
| `radar_web/radar/discogs_dump.py` | index SQLite du dump Discogs - fournisseur de donnees externe, interroge par `label_affinities`, `label_db_signal`, `artist_label_signal` |
| `radar_web/radar/sellers.py` (`seller_affinity`) | seul autre module (hors `app.py`/`crate_jobs.py`) a lire directement `Ctx.ascore` |
| `crate_jobs.py` (`job_scan_recos`, `job_build_graph`, `job_scan_catalog`) | jobs qui alimentent les fichiers d'entree du scoring ou appellent dedans |
| `radar_web/app.py` | toutes les routes qui creent un `Ctx()` et affichent ses scores (section 9) |
| `crate_radar_config.json` (par utilisateur) | `scoring` (poids), `taste_categories`, `artist_categories`, `labels`, `watchlist` - la config que `Ctx` lit a chaque requete |
| `producer_graph.json` (par utilisateur) | aretes brutes du graphe de co-credits, ecrites par `job_build_graph`, relues par `Ctx.graph_rescore()` |

### Ou regarder pour une tache donnee
- **Changer comment un score est calcule** (label, artiste, album, reco) ->
  `radar_web/radar/scoring.py`, la methode `Ctx` correspondante (section 6).
- **Changer un poids par defaut** -> `DEFAULT_SCORING` dans
  `radar_web/radar/store.py` (et le formulaire correspondant dans
  `radar_web/templates/pages/settings.html`, hors perimetre de ce document).
- **Ajouter/retirer une source de signal** (ex. une nouvelle source d'ecoute) ->
  `scoring.sources` (poids) + la ou le corpus est lu dans `scoring.py`
  (`ascore`, `corpus_label_scores`, `label_artist_signal`).
- **Le graphe de co-credits donne des resultats etranges** -> distinguer
  construction (`job_build_graph`, `crate_jobs.py`, aretes brutes) de
  recalcul du score (`Ctx.graph_rescore`, `scoring.py`, poids courants) - le
  probleme est souvent dans l'un des deux, jamais les deux a la fois.
- **La boucle de feedback ne propose rien** -> `learn.summary()` exige au moins
  12 votes au total et 3 par classe (up/down) - verifier `reco_feedback.json`.
