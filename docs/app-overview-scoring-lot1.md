# Radar - Le systeme de scoring (Lot 1)

Ce document couvre UNE partie de l'application Radar : le moteur qui calcule
"est-ce que ce disque/cet artiste/ce label est fait pour mon gout ?", et la
categorisation Coeur/Aime des labels qui l'alimente depuis le "Lot 1" d'une
refonte en cours. Ce n'est pas un aperçu de toute l'appli.

## 0. Perimetre - ce qui est dedans, ce qui est dehors

**Dedans (ce que ce document decrit) :**
- `radar_web/radar/scoring.py` - la classe `Ctx` : tous les calculs de note.
- `radar_web/radar/store.py` - chargement/sauvegarde de la config utilisateur,
  les poids par defaut (`DEFAULT_SCORING`), et la migration
  `labels`/`watchlist` -> `label_categories`.
- Les routes de `radar_web/app.py` qui LISENT ou MODIFIENT ce scoring :
  la page "Mes labels & artistes" (`/univers/...`), les routes de
  recommandation rapide (`/reco/label`, `/reco/artist`), et la page Reglages
  (`/settings`) qui edite les poids.
- Les jobs de fond de `crate_jobs.py` qui alimentent ou consomment ce
  systeme : `job_scan_recos`, `job_scan_veille`, `job_profile_labels`,
  `job_canonicalize`, `job_fetch_collection`, `job_merge_corpus`,
  `job_build_graph`.

**Dehors (traite comme externe, mentionne seulement pour situer le flux) :**
- L'UI HTMX/Jinja elle-meme (templates `.html`, rendu des pages) - ce
  document dit quelle route appelle quel calcul, pas comment le HTML est
  construit.
- Le referentiel Discogs local (`radar_web/radar/discogs_dump.py`, dump
  mensuel importe en SQLite) - `Ctx` l'interroge (styles d'un label, artistes
  d'un label) mais ne le construit pas.
- L'API Discogs elle-meme (`discogs.py`, `discogs_get`/`discogs_search`) et
  ytcache/YouTube - sources de donnees pour les jobs, pas du scoring.
- Le worker/scheduling (`radar_web/worker.py`, `radar_web/radar/jobs.py`) -
  qui decide QUAND lancer un job. Ce document decrit ce que fait un job une
  fois lance, pas la logique de planification.
- Les autres jobs de `crate_jobs.py` non listes (ingestion YouTube/Spotify/
  Bandcamp/DJ sets, scan vendeurs, `job_publish_recos`) : ils alimentent le
  corpus ou consomment ses candidats mais ne sont pas detailles ici, sauf
  breve mention pour situer `job_scan_recos` dans sa chaine.

Si un element decrit plus bas s'avere ne pas exister, ou etre bien plus
etendu que decrit, cela est signale explicitement plutot que suppose.

## 1. Ce que fait ce systeme, en une phrase

Radar transforme "les labels et artistes que tu as ranges en Coeur/Aime" +
"ce que tu as ecoute/possede" en une note sur 100 pour a peu pres n'importe
quoi qui a un label, un artiste ou un style Discogs - une sortie dans une
recherche, une nouveaute detectee, une piste de DJ set, un candidat pour la
playlist RECOS RADAR.

## 2. Vocabulaire cle (a poser avant tout le reste)

| Terme | Sens dans ce systeme |
|---|---|
| Coeur (tier "1") | Categorie de gout la plus forte - labels/artistes/styles que tu adores. |
| Aime (tier "2") | Categorie de gout intermediaire. |
| `label_categories` | `{"1": [noms...], "2": [noms...]}` - LA structure qui remplace les anciennes listes a plat `labels` (base) et `watchlist` (veille nouveautes). |
| `artist_categories` | Meme forme, pour les artistes (deja a 2 tiers depuis une fusion anterieure). |
| `taste_categories` | Meme forme, mais pour des STYLES Discogs (ex. "Deep House" en Coeur). |
| `wmap` | Le dictionnaire style -> poids (0 a 1) derive de `taste_categories` + `taste_tiers`. C'est la brique de base de toute affinite de style. |
| `ascore` | Note 0-100 par artiste (cle canonique), calculee par `Ctx`. |
| `reco_index` / `reco_rows` | Note 0-100 par label - "dois-je suivre ce label ?". |
| `album_score` | Note 0-100 pour UNE sortie/piste (label + artiste + style combines). |
| Cle canonique (`ck`) | `id:<discogs_id>` si l'artiste est resolu sur Discogs, sinon le nom normalise. Unifie "Four Tet" et une graphie legerement differente issue d'une autre source. |

## 3. `store.py` - config utilisateur, poids par defaut, migration

### 3.1 Role du fichier

`store.py` ne calcule rien : il lit/ecrit `crate_radar_config.json` (un
fichier JSON par utilisateur, chemin donne par `paths.user_paths(uid)`),
normalise les noms (`normalize_label`, `style_key`), et applique des
migrations de schema a la volee a chaque chargement.

### 3.2 `DEFAULT_SCORING` - la table des poids

C'est LE dictionnaire de reference pour tous les poids du moteur de scoring.
Chaque sous-cle correspond a un groupe de poids edite depuis `/settings` :

| Groupe | Poids | Usage |
|---|---|---|
| `taste_tiers` | `1: 1.0, 2: 0.6, 3: 0.3` | Poids d'un style selon son tier de gout (le tier "3" existe encore ici en valeur par defaut mais n'est plus utilise par `taste_categories`, fusionne dans "2"). |
| `artist_tiers` | `1: 1.0, 2: 0.5` | Poids "manuel" d'un artiste Coeur/Aime dans `ascore`. |
| `reco` | `collection, corpus, artist, affinity, want_factor, db_link` | Poids des 5 signaux qui composent la note d'un LABEL (`reco_rows`). |
| `album` | `label, artist, style, artist_max_vs_mean` | Poids des 3 signaux qui composent la note d'un ALBUM/piste (`album_score`). |
| `artist_score` | `manual, corpus, collection, graph, djset, label_link` | Poids des 6 signaux qui composent `ascore`. |
| `graph` | `tier_w, artist_breadth, label_breadth, cat1_bonus, role_*, max_credits, max_levels, level_decay, node_cap` | Parametres du graphe de co-credits (construit par `job_build_graph`, recalcule cote scoring par `graph_rescore()`). |
| `sources` | `discogs_collection, discogs_want, youtube, spotify, bandcamp, djset` | Poids d'une ligne de corpus selon sa provenance (Bandcamp/collection possedee comptent plus qu'une simple ecoute YouTube). |
| `recos` | `min_score, max_new_releases, searches_per_run, max_tracks` | Reglages de la feature RECOS RADAR (ce ne sont pas des poids de note, mais des seuils/plafonds). |
| `label_affinity_floor` | entier | Seuil d'affinite en-dessous duquel un label est exclu de `reco_rows` (0 = desactive). |
| `learn` | `l2, min_feedback, min_per_class` | Parametres du module d'apprentissage a partir du feedback (`learn.py`, hors perimetre de ce document). |

`deep_merge(DEFAULT_SCORING, data.get("scoring", {}))` est applique a CHAQUE
chargement de config : un utilisateur dont le fichier ne connait pas encore
une cle recente (ex. `recos.searches_per_run` ajoutee plus tard) recoit la
valeur par defaut sans migration explicite necessaire.

### 3.3 La migration `label_categories` (coeur du Lot 1)

Avant : deux listes de noms de labels a plat dans la config -
`labels` (la "base" suivie de pres) et `watchlist` (surveillance de
nouveautes uniquement, scannee par `job_scan_veille`). Ces deux listes
n'avaient pas de notion de tier Coeur/Aime.

Apres : une seule structure `label_categories = {"1": [...], "2": [...]}`,
exactement le meme modele que `artist_categories`. `labels` (base, deja
curee, signal fort) devient le tier Coeur ("1") ; `watchlist` (surveillance
plus large, signal plus faible) devient le tier Aime ("2"), pour les entrees
pas deja presentes cote Coeur.

`store.load_config()` (appele par tout chargement de config) fait cette
migration automatiquement et une seule fois :

```
si "labels" ou "watchlist" existe encore dans le fichier config :
    lc = label_categories (cree si absent, {"1": [], "2": []})
    seen = noms deja normalises presents dans lc["1"] + lc["2"]
    pour chaque nom de "labels"    -> ajoute a lc["1"] si pas deja vu
    pour chaque nom de "watchlist" -> ajoute a lc["2"] si pas deja vu
    supprime les cles "labels" et "watchlist" du fichier
```

Consequence directe cote jobs : **`job_scan_veille` ne fait plus de
distinction base/watchlist** - TOUT label suivi (Coeur ou Aime) est
desormais scanne pour ses nouveautes (cf. section 6.2). C'est le changement
comportemental principal introduit par cette migration, au-dela du simple
renommage de structure.

`label_tier_map()` (dans `scoring.py`, section 4.4) est le miroir cote
lecture : il retransforme `label_categories` en `{label_key: "1"|"2"}`.

### 3.4 Secrets et portee utilisateur

`load_config` amorce les secrets d'environnement (`DISCOGS_TOKEN`,
`YOUTUBE_API_KEY`, etc.) UNIQUEMENT pour l'utilisateur proprietaire
(`paths.DEFAULT_UID`) - un detail multi-utilisateur, pas du scoring, mais
important pour comprendre pourquoi `Ctx(uid=...)` avec un autre uid n'aura
pas de token si son propre fichier config n'en a pas.

### 3.5 Cache de lecture

`read_config()` memorise le dict de config par (mtime, taille) du fichier -
reserve aux lecteurs (`Ctx`, rendu de page). Le dict renvoye est partage
entre requetes et NE DOIT PAS etre mute ; le chemin d'ecriture des routes
(`_cfg()` dans `app.py`) repasse par `load_config()` qui, lui, peut etre
mute avant `save_config()`.

## 4. `scoring.py` - la classe `Ctx`

### 4.1 Cycle de vie

`Ctx(uid=None)` est reconstruit a CHAQUE requete HTTP (ou a chaque job qui
en a besoin, via `Ctx(uid=RADAR_UID)`). A la construction, il charge en
memoire : la config (`store.read_config`), et les gros fichiers JSON de
l'utilisateur (`profile`, `collection`, `corpus`, `resolved`,
`artists_res`, `graph`) via `store.load_cached` - qui, lui, ne relit
vraiment le fichier que si son (mtime, taille) a change depuis le dernier
appel. Construire un `Ctx` est donc peu couteux en pratique (fichiers
< 3 Mo, caches deja chauds la plupart du temps).

### 4.2 Memoisation des calculs derives (`_memo` / `_DERIVED`)

Les calculs couteux (`wmap`, `ascore`, `graph_rescore`, `reco_rows`,
`reco_index`, `label_db_signal`, `artist_label_signal`,
`seed_category_weight`) sont memorises dans un cache MODULE (`_DERIVED`,
plafonne a 8 entrees, vide en bloc au-dela) sous une cle
`(uid, signature des fichiers d'entree)` - la signature est le tuple
`(mtime_ns, taille)` de config + graphe + artistes resolus + corpus +
collection + profil + le fichier SQLite du dump Discogs. Toute ecriture
d'un job ou de `save_config` invalide donc automatiquement l'entree
memoire correspondante au prochain `Ctx()`. Deux `Ctx` construits dans la
meme requete (ou deux requetes concurrentes sans ecriture entre les deux)
partagent le meme calcul deja fait.

### 4.3 Affinite de style - `wmap`, `affinity_score`, `style_affinity_of`

- `wmap` (propriete memoisee) : parcourt `taste_categories` (Coeur/Aime de
  STYLES) et `taste_tiers` (poids par tier), produit
  `{style_normalise: poids 0-1}`.
- `affinity_score(entry)` : a partir d'un `{style_counts: {style: n}}` (le
  profil d'un label), calcule une affinite 0-100 ET une couverture 0-100.
  Regle importante (documentee dans le code comme "diagnostic N4") : un
  style ABSENT de `wmap` (jamais classe par l'utilisateur) est EXCLU du
  calcul plutot que compte comme 0 - sinon "je n'ai jamais classe ce style"
  se confondrait avec "je deteste ce style". La part exclue redevient
  visible via `coverage`.
- `label_affinities(label_keys)` : pour un lot de labels, prend en priorite
  le profil du referentiel Discogs local (`discogs_dump.label_style_counts`,
  exhaustif sur le catalogue importe), et ne retombe sur
  `labels_profile.json` (echantillon API, biaisable) que pour les labels
  absents du dump.
- `style_affinity_of(styles)` : affinite moyenne d'une LISTE de styles (pas
  un profil de label) - utilise directement par `album_score`.

### 4.4 Artistes - cles canoniques, `artist_tier_map`, `label_tier_map`

- `canon_artist_key(name)` / `canon_artist_name(name)` : resolvent un nom
  brut vers sa cle canonique (`id:<discogs_id>` si resolu et confirme/exact/
  approx) via `artists_res` (le fichier `artists_resolved.json`, alimente
  par `job_canonicalize` et les jobs de graphe).
- `artist_tier_map()` : `{cle canonique: "1"|"2"}` depuis
  `cfg["artist_categories"]`.
- `label_tier_map()` : `{label normalise: "1"|"2"}` depuis
  `cfg["label_categories"]` - **c'est directement le point d'entree cote
  lecture de la migration du Lot 1** ; tout code qui veut savoir "ce label
  est-il suivi, et a quel tier" passe par ici plutot que par deux listes
  separees.
- `artist_disp()` : nom d'affichage par cle canonique, agrege depuis les
  categories, le corpus, la collection et le graphe.

### 4.5 `ascore` - la note d'un artiste

`ascore` (propriete memoisee) combine SIX signaux, chacun ramene sur une
echelle 0-1 puis pondere par `scoring.artist_score` :

| Signal | Poids par defaut | Source |
|---|---|---|
| `manual` | 0.5 | Tier Coeur/Aime explicite (`artist_tiers`) |
| `corpus` | 0.18 | Nombre d'apparitions dans le corpus ecoute (hors DJ sets) |
| `collection` | 0.10 | Nombre de disques possedes de cet artiste |
| `graph` | 0.14 | Score de proximite dans le graphe de co-credits (`graph_rescore()`) |
| `djset` | 0.08 | Apparitions dans des sets DJ scannes (compte a part du corpus general) |
| `label_link` | 0.15 | L'artiste a-t-il une sortie chez un label Coeur/Aime, DANS UN STYLE QUE J'AIME (`artist_label_signal()`) |

Les comptages bruts (corpus/collection/DJ/graphe) passent par une echelle
"robuste" (`_robust_scale` = p95 des valeurs, jamais le max) puis un
`_log_ratio` (log1p plafonne a 1) avant ponderation - pour qu'un seul
artiste hyperactif (ex. 40 apparitions dans un DJ set) ne comprime pas
l'echelle de tous les autres. La somme ponderee est normalisee par la
somme des poids REELLEMENT en jeu (`tot`), pas par une constante fixe -
ajouter un signal rééquilibre les autres au lieu de faire deriver le
plafond au-dela de 100.

`artist_label_signal()` (le terme `label_link` ci-dessus) est le point ou
le croisement Coeur+Aime des labels influence directement la note d'un
ARTISTE : il regarde, via le referentiel Discogs local
(`discogs_dump.artist_ids_for_labels`), tous les artistes credites sur une
sortie d'un label suivi, mais ne compte le credit que si au moins un style
de CETTE sortie precise est dans `wmap` (le gout courant) - correction
recente (piege documente : partager un label suivi ne suffit plus a lui
seul, un featuring hors-style sur un label par ailleurs proche du gout
n'est plus compte).

### 4.6 `graph_rescore()` - proximite par co-credits

Recalcule, a partir des ARETES BRUTES stockees par `job_build_graph`
(comptes de co-credits, pas de score fige), une proximite courante pour
chaque artiste et chaque label candidats - c'est-a-dire ceux qui NE sont
PAS deja dans `artist_tier_map()`/`label_tier_map()`. Le score depend de
`seed_category_weight()` (le poids de la graine qui a genere l'arete :
Coeur/Aime prioritaire, sinon la meilleure source du corpus ou l'artiste
apparait), de la largeur (`artist_breadth`/`label_breadth`, bonus pour un
candidat lie a PLUSIEURS graines) et d'un bonus fixe (`cat1_bonus`) si au
moins une graine Coeur est impliquee. Comme les arretes brutes ne changent
qu'a la reconstruction du graphe, mais les poids/tiers peuvent changer a
tout moment dans Reglages, ce recalcul cote lecture permet de changer les
poids sans avoir a relancer le job de graphe.

### 4.7 `reco_rows()` / `reco_index` - la note d'un LABEL

`reco_rows()` calcule, pour chaque label connu (union de : possede,
present dans la wantlist, ecoute dans le corpus, lie via
`label_artist_signal`, ou lie via `label_db_signal`), une note 0-100
combinant 5 signaux ponderes par `scoring.reco` :

| Feature | Poids par defaut | Signification |
|---|---|---|
| `collection` | 0.6 | Nombre possede + `want_factor` x nombre en wantlist, normalise par le max |
| `corpus` | 0.5 | Score d'ecoute du corpus pour ce label (`corpus_label_scores`, pondere par source) |
| `artist` | 0.4 | `label_artist_signal` - des artistes que j'aime ont-ils ete ECOUTES sur ce label |
| `affinity` | 0.4 | Affinite de style du label (`label_affinities`) |
| `db_link` | 0.35 | `label_db_signal` - un artiste Coeur/Aime a-t-il un disque chez ce label au CATALOGUE (meme jamais ecoute), combine au graphe de co-credits |

`label_affinity_floor` (si non nul) filtre les labels dont l'affinite est
REELLEMENT basse - jamais ceux simplement "pas encore profiles"
(`aff is None`), pour ne pas masquer silencieusement des labels non
traites en faisant croire a un filtre qualite.

`reco_index` est juste `{label_key: score}` derive de `reco_rows()`,
memoise separement car `album_score()` y accede en boucle (jusqu'a
50-100 fois par recherche) et reconstruire ce dict a chaque acces avait un
cout mesurable.

### 4.8 `album_score(r)` - la note finale d'une sortie/piste

Prend un objet minimal `{"label": [...], "title": "Artiste - Titre",
"style": [...]}` (le format standard utilise partout : resultats Discogs,
lignes de corpus, candidats RECOS) et combine jusqu'a 3 termes, ponderes
par `scoring.album` :

- **label** (poids 0.4) : le `reco_index` du label de la sortie (le
  meilleur si plusieurs labels).
- **artist** (poids 0.4) : moyenne ponderee entre le MAX et la MOYENNE des
  `ascore` des artistes credites (`artist_max_vs_mean`, 0.6 par defaut -
  favorise le meilleur artiste credite sans ignorer completement les
  autres).
- **style** (poids 0.2) : `style_affinity_of` sur les styles de la sortie.

Un terme absent (ex. aucun artiste identifiable) est simplement omis de la
somme, et les poids restants sont renormalises par leur propre somme. Un
**retrecissement vers un a priori neutre** (`K=0.35`, `PRIOR=45`) tire le
resultat vers 45 d'autant plus fort que peu de poids total est en jeu -
concretement, un score fonde sur un seul critere disponible (ex. seulement
le style, aucun label ni artiste reconnu) n'affiche pas la meme confiance
qu'un score fonde sur les trois a la fois, sans que l'UI ait besoin
d'afficher un indicateur de confiance separe.

### 4.9 `stats()` - petits compteurs d'etat

Fonction utilitaire (pas un calcul de note) qui compte, entre autres,
`labels_coeur`/`labels_aimes` a partir de `label_categories["1"]`/`["2"]`
- consommee par la page d'accueil de "Mes labels & artistes" pour afficher
les totaux.

## 5. Les routes de `app.py` qui touchent ce systeme

### 5.1 Page "Mes labels & artistes" (`/univers/...`)

| Route | Role |
|---|---|
| `GET /univers` | Page principale - compte les labels/artistes par categorie, affiche le panneau "A verifier" (approx/not_found). |
| `GET /univers/labels/table` | Tableau paginable/triable des labels de `label_categories` avec note d'affinite et reco (`_base_labels_ranked`). |
| `POST /univers/labels/add` | Ajoute un nom a `label_categories[tier]` (1 ou 2), en verifiant qu'il n'est pas deja present dans L'UN OU L'AUTRE tier (dedoublonnage tous tiers confondus). Declenche `_queue_enrich` (job `enrich`, canonisation/resolution differee). |
| `POST /univers/labels/remove` | Retire un nom des DEUX tiers de `label_categories`. |
| `POST /univers/label/set` | Deplace un label vers un tier cible (retire des deux, rajoute dans le tier choisi) - c'est le "glisser vers Coeur/Aime" de l'UI. |
| `POST /univers/labels/graph/build` | Construit un sous-graphe visuel de labels voisins a partir de graines (`labelgraph.build`), a partir des aretes deja stockees dans `graph`. |
| `GET /univers/artists/table` | Tableau des artistes connus (union `ascore` + `artist_tier_map`), note et categorie affichees. |
| `POST /univers/artist/set` | Equivalent artiste de `/univers/label/set`. |
| `POST /univers/review/{kind}/{action}` | Confirme/rejette une resolution "approximative" (label ou artiste) issue de `job_canonicalize`. |

### 5.2 Routes de recommandation rapide

- `POST /reco/label` : ajoute un nom directement a
  `label_categories[tier]` depuis un contexte de recommandation (ex. un
  bouton dans une liste de suggestions), avec le meme dedoublonnage
  tous-tiers que `/univers/labels/add`.
- `POST /reco/artist` : equivalent pour `artist_categories`.
- `GET /univers/reco/labels` : suggestions de labels HORS base
  (`c.reco_rows()` filtre pour exclure ce qui est deja dans
  `label_tier_map()`) + candidats issus du graphe de co-credits
  (`graph_rescore()["labels"]`), affiches separement car le score de
  graphe n'est pas sur la meme echelle /100.
- `GET /univers/reco/artists` : equivalent pour les artistes, avec un tri
  d'AFFICHAGE par `ascore` (la note visible) different du tri INTERNE du
  graphe (par proximite brute) - deliberement decouple pour eviter une
  liste qui semble mal triee.

### 5.3 Reglages (`/settings`)

- `GET /settings` : affiche le formulaire avec les valeurs courantes de
  `c.scoring` (donc `cfg["scoring"]`, deja fusionne avec
  `DEFAULT_SCORING`).
- `POST /settings` : relit chaque groupe de poids depuis le formulaire
  (`reco`, `album`, `artist_score`, `recos`, plus `artist_tiers` et
  `taste_tiers` a part) et les ecrit directement dans `cfg["scoring"]`
  avant `store.save_config(c)`. Ne touche PAS a `label_categories`
  lui-meme (ca, c'est le role des routes `/univers/label/set` et
  `/reco/label`) - `/settings` edite uniquement les PONDERATIONS, jamais
  la liste des labels/artistes suivis.
- `GET/POST /settings/learn*` : propose/applique des poids suggeres par le
  module d'apprentissage a partir du feedback utilisateur (`learn.py`,
  hors perimetre detaille de ce document, mais ecrit dans les memes cles
  de `cfg["scoring"]`).

Un piege deja documente et corrige (point 23 de `CLAUDE.md`) : les champs
`<input type="number" step="...">` de ce formulaire doivent garder
`step="any"`, sinon un poids par defaut non multiple du pas bloque
silencieusement tout le formulaire des qu'on le modifie via la souris.

## 6. Les jobs de fond (`crate_jobs.py`)

Tous ces jobs tournent en sous-processus (lances par le worker, statut dans
`/data/jobs/*.status.json`), independamment de toute requete HTTP. Ils
lisent/ecrivent directement les fichiers JSON via `cfg_load()`/`save_json()`
(des alias locaux vers `store.load`/`store.save`), pas via un `Ctx` dans la
plupart des cas - sauf `job_scan_recos`, qui instancie un `Ctx(uid=RADAR_UID)`
pour reutiliser `album_score()` telle quelle.

### 6.1 `job_fetch_collection` - collection + wantlist Discogs

Recupere via l'API Discogs la collection et la wantlist de l'utilisateur,
construit `collection_cache.json` (comptes par label et par artiste). Si
`merge_base=True` (par defaut), fusionne les NOUVEAUX labels detectes dans
`label_categories["2"]` (Aime par defaut - un label juste possede ne
devient pas automatiquement Coeur) et seme `labels_resolved.json` avec les
ids Discogs deja connus (statut "confirmed", pas d'appel de resolution
necessaire).

### 6.2 `job_scan_veille` - detection de nouveautes

Lit maintenant `label_categories["1"] + label_categories["2"]` en UNE seule
liste (`wl`) pour construire la regle implicite "Labels suivis" - avant la
migration, seule `watchlist` alimentait cette regle et `labels` (base)
n'etait pas scannee pour ses nouveautes du tout. **C'est le changement de
comportement le plus visible du Lot 1** : tout label Coeur OU Aime produit
desormais des alertes de nouveaute, pas seulement ceux explicitement mis en
"watchlist" avant la migration. Les nouveautes trouvees sont empilees dans
`veille_new.json`, consomme par la page "Nouveautes" qui leur applique
`Ctx.album_score` a l'affichage (route `_inbox`, hors perimetre detaille de
ce document mais situee section 4.8/5).

### 6.3 `job_profile_labels` - profil style/genre par label API

Pour chaque label prioritaire (signal collection/corpus d'abord, puis TOUT
le reste de `label_categories["1"] + ["2"]` non encore couvert), interroge
l'API Discogs (recherche triee par "want") pour construire
`labels_profile.json` - le repli utilise par `Ctx.label_affinities` quand le
referentiel local n'a pas ce label. Lit `label_categories` en deux boucles
fusionnees (`for cid in ("1", "2")`), pas deux listes separees.

### 6.4 `job_canonicalize` - resolution des noms vers Discogs

Reecrit `label_categories["1"]` et `["2"]` (ainsi que
`artist_categories["1"/"2"/"3"]`) avec les noms canoniques Discogs
resolus localement (`discogs_dump.resolve_name`, repli API si absent du
dump). Chaque nom deja "exact"/"confirmed" est saute (pas d'appel reseau
reste). Alimente aussi `resolved`/`artists_res`, qui alimentent a leur tour
`Ctx.canon_artist_key`/`label_tier_map` indirectement (via les noms
renommes dans les listes elles-memes, pas via une reference d'id separee
pour les labels).

### 6.5 `job_merge_corpus` - nettoyage/fusion des labels du corpus

Detecte les nouveaux labels apparus dans le corpus ecoute (YouTube/Spotify/
Bandcamp/DJ sets), filtre le bruit (distributeurs, mentions "not on label",
societes de gestion de droits), et les ajoute a `label_categories["2"]`
(Aime par defaut, meme logique que `job_fetch_collection`) - encore une
fois une seule liste fusionnee au lieu d'une distinction base/watchlist.

### 6.6 `job_build_graph` - graphe de co-credits

Construit les ARETES BRUTES du graphe de proximite (stockees dans
`producer_graph.json`, ensuite recalculees a la lecture par
`Ctx.graph_rescore()`). Ce job choisit ses GRAINES parmi les ARTISTES
(`artist_categories`), pas les labels - le mode `"taste"` (celui utilise en
entretien de fond mensuel) prend Coeur + Aime + tout artiste du corpus comme
graines. **Constat a noter explicitement** : ce job ne lit PAS
`label_categories` directement pour choisir ses graines ; les labels
n'apparaissent dans son resultat que comme sous-produit (`label_edges`,
"quels labels partagent un artiste avec mes graines"), pas comme point de
depart. Une fonction plus ancienne, `_seed_artists(cfg)`, reste utilisee
par le mode par defaut `"top"` (graines = les N artistes les mieux notes
par un score de compatibilite local a cette fonction, distinct de
`Ctx.ascore`) - a verifier si ce mode est encore reellement invoque en
production, l'entretien automatique documente dans `CLAUDE.md` utilisant
le mode `"taste"`.

### 6.7 `job_scan_recos` - alimentation de la playlist RECOS RADAR

Le seul job de cette liste qui instancie un `Ctx` complet
(`Ctx(uid=RADAR_UID)`) pour reutiliser `album_score()` sans dupliquer la
formule. Lit `label_categories["1"] + ["2"]` (Coeur ET Aime, meme
fusion que partout ailleurs dans le Lot 1) pour batir la liste des labels a
scanner dans le referentiel Discogs local, note chaque sortie avec
`ctx.album_score(...)`, garde celles au-dessus de `scoring.recos.min_score`,
recupere leur tracklist reelle via l'API Discogs (le dump local n'a pas les
tracklists), et empile des candidats PAR PISTE dans
`recos_candidates.json`. Ne fait AUCUN appel YouTube lui-meme - c'est
`job_publish_recos` (job separe, hors perimetre detaille ici, mais chaine
automatiquement en fin de `job_scan_recos` via `_chain_publish_recos()`)
qui consomme cette file pour chercher une video par piste.

## 7. Vue d'ensemble du flux (uniquement la partie scoring)

```
Reglages utilisateur (poids)          Categorisation (listes de noms)
   /settings  -> cfg["scoring"]          /univers/*, /reco/*
        |                                  -> cfg["label_categories"]
        |                                  -> cfg["artist_categories"]
        |                                  -> cfg["taste_categories"]
        v                                       |
   store.save_config()  <----------------------+
        |
        v
   store.read_config() (mtime-cache)
        |
        v
      Ctx(uid)  <---- charge aussi : profile.json, collection_cache.json,
        |             taste_corpus.json, *_resolved.json, producer_graph.json,
        |             discogs_dump.sqlite3 (referentiel local, EXTERNE)
        |
        +-- wmap / affinity_score / style_affinity_of   (styles)
        +-- ascore                                       (artistes)
        +-- graph_rescore()                              (proximite graphe)
        +-- reco_rows() / reco_index                      (labels)
        +-- album_score(r)                                (sortie/piste)
                |
                +--> résultats de recherche (/search, _scored_rows)
                +--> Nouveautés (_inbox, veille_new.json)
                +--> job_scan_recos (candidats RECOS RADAR)
                +--> pages univers.html (tableaux labels/artistes)
```

En amont de `Ctx`, les jobs de fond (`job_fetch_collection`,
`job_merge_corpus`, `job_canonicalize`, `job_profile_labels`,
`job_scan_veille`) ecrivent dans les memes fichiers que `Ctx` lit -
notamment `label_categories`, desormais une seule structure a 2 tiers
partout, au lieu des deux listes `labels`/`watchlist` d'avant le Lot 1.

## 8. Etat / donnees - tableau recapitulatif

| Donnee | Fichier | Ecrit par | Lu par (dans ce perimetre) |
|---|---|---|---|
| Poids de scoring | `crate_radar_config.json` -> `scoring` | `/settings` (POST), migrations de `store.load_config` | `Ctx` (toutes les methodes) |
| Labels Coeur/Aime | `crate_radar_config.json` -> `label_categories` | `/univers/label*`, `/reco/label`, `job_fetch_collection`, `job_merge_corpus`, `job_canonicalize` | `Ctx.label_tier_map`, `job_scan_veille`, `job_scan_recos`, `job_profile_labels` |
| Artistes Coeur/Aime | `crate_radar_config.json` -> `artist_categories` | `/univers/artist*`, `/reco/artist`, `job_canonicalize` | `Ctx.artist_tier_map`, `job_build_graph` (graines) |
| Styles Coeur/Aime | `crate_radar_config.json` -> `taste_categories` | (edite ailleurs dans l'UI, hors perimetre liste ici) | `Ctx.wmap` |
| Profil style/genre par label | `labels_profile.json` | `job_profile_labels` | `Ctx.label_affinities` (repli) |
| Collection/wantlist | `collection_cache.json` | `job_fetch_collection` | `Ctx.reco_rows`, `Ctx.ascore` |
| Corpus ecoute | `taste_corpus.json` | jobs d'ingestion (hors perimetre) | `Ctx.ascore`, `Ctx.reco_rows`, `Ctx.djset_rows` |
| Resolution labels/artistes Discogs | `labels_resolved.json` / `artists_resolved.json` | `job_canonicalize`, `job_fetch_collection`, `job_build_graph` | `Ctx.canon_artist_key`, `Ctx.label_tier_map` (affichage) |
| Graphe de co-credits (aretes brutes) | `producer_graph.json` | `job_build_graph` | `Ctx.graph_rescore` |
| Candidats RECOS RADAR | `recos_candidates.json` | `job_scan_recos` | `job_publish_recos` (externe a ce document) |

## 9. Points a retenir pour qui modifie ce code

- `label_categories` et `artist_categories` ont exactement la meme forme
  (`{"1": [...], "2": [...]}`) et les memes conventions de dedoublonnage
  (par nom normalise, tous tiers confondus) - un changement fait sur l'un
  a de bonnes chances de devoir etre reflete sur l'autre.
- Un signal "manque de donnees" (`aff is None`, style absent de `wmap`,
  jamais profile) doit systematiquement rester DISTINCT d'un signal
  "valeur reelle basse/zero" dans tout nouveau calcul - c'est une regle
  explicite repetee a plusieurs endroits du code (`affinity_score`,
  `_compute_artist_label_signal`, filtre de `reco_rows`), documentee dans
  `CLAUDE.md` comme "diagnostic N4".
- Toute somme ponderee de plusieurs signaux (`ascore`, `reco_rows`,
  `album_score`) est normalisee par la somme des poids REELLEMENT non
  nuls en jeu pour cette ligne precise, jamais par une constante fixe -
  necessaire pour ajouter un nouveau signal sans faire deriver l'echelle
  0-100 existante.
- `job_build_graph` ne lit pas `label_categories` pour ses graines
  (uniquement `artist_categories` + corpus) - a garder en tete si on
  s'attend a ce que la migration du Lot 1 change aussi son comportement :
  ce n'est PAS le cas pour ce job precis.
- Aucun des fichiers/routes couverts ici ne fait d'appel reseau direct au
  moment de la LECTURE du score (`Ctx`) - seuls les JOBS appellent l'API
  Discogs ou le referentiel local. `Ctx` est donc rapide et sans effet de
  bord reseau, ce qui explique qu'il soit recree a chaque requete sans
  precaution particuliere.
