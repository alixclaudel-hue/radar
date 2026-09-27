# Radar — archive détaillée (historique complet de CLAUDE.md)

Contenu déplacé de `CLAUDE.md` le 2026-09-06 pour économiser des tokens en lecture
automatique (résumé condensé conservé dans `CLAUDE.md`, ce fichier est dans
`.claudeignore` — non lu automatiquement, à consulter explicitement pour le détail).

## Structure

- **`radar_web/`** — FastAPI + HTMX, port 8600. **L'interface.**
- **`crate_jobs.py`** — worker des tâches longues, lancé par `radar_web/worker.py`
  (file d'attente) ou en direct. Partagé, actif.
- **`archive/`** — ancienne appli Streamlit (`crate_radar.py`), retirée le 2026-09-01.
  Non maintenue. Ne pas y toucher.

## Où ça tourne / déploiement

Édition en local → `git push` → GitHub (`alixclaudel-hue/radar`) → un merge sur `main`
déclenche le workflow `.github/workflows/deploy.yml` qui se connecte au VPS OVH, fait
`git pull` + rebuild Docker + health-check (rollback si KO).

Coordonnées du VPS (hôte, utilisateur, clé de déploiement) : dans les *Secrets* Actions du
repo (`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY_B64`) et dans une note perso non versionnée.

Déploiement manuel (dépannage, avec ta propre clé SSH) :
```
ssh -i ~/.ssh/<ta-cle> <user>@<vps-host> \
  'cd ~/radar && git fetch -q origin && git reset --hard origin/main -q \
   && sudo docker compose up -d --build radar-web'
```
**Toujours `git fetch` avant `reset --hard origin/main`** (sinon on redéploie l'ancien
commit). Si le conteneur affiche "Running" au lieu de "Recreated" après un changement,
ajouter `--force-recreate`. Un `curl` non authentifié renvoie 303 → `/login` : normal.

## Données

`/data` sur le VPS (monté dans les conteneurs sous `/data`) : fichiers JSON — labels,
corpus, graphe, profils, `crate_radar_config.json` (contient le token Discogs, saisi via
l'UI). **Rien de tout ça n'est dans git.** En local : `export CRATE_DATA_DIR=$PWD/data`.

## Session cloud (Claude Code sur le web / mobile)

Pilotable depuis le téléphone, indépendant du Mac. La session tourne sur une VM
Ubuntu jetable, dépôt cloné frais depuis GitHub. `scripts/cloud-setup.sh` (câblé en
hook `SessionStart` dans `.claude/settings.json`, gardé par `CLAUDE_CODE_REMOTE`)
installe la stack FastAPI au démarrage — sans effet en local.

Ce qui marche : éditer le code, `py_compile`, le smoke test des routes, ouvrir des
PR. Le déploiement se fait tout seul au merge sur `main` (workflow GitHub Actions),
rien à pousser à la main.

Ce qui n'est PAS là : `/data`, `.env`, le token Discogs → les jobs qui appellent
Discogs / YouTube / Bandcamp ne tournent pas ici ; Playwright + yt-dlp non installés
(extraction DJ sets KO) ; pas d'accès SSH au VPS ; la mémoire perso de Claude Code
(`~/.claude/`) n'est pas clonée — ce fichier et `docs/` sont la source de vérité.

Smoke test (comme la CI, sans `/data`) :
```bash
CRATE_DATA_DIR=$(mktemp -d) RADAR_NO_AUTH=1 \
  python -m uvicorn radar_web.app:app --port 8600 --log-level warning &
curl -sf localhost:8600/health && for r in / /search /univers /settings /veille /patte; do
  curl -s -o /dev/null -w "%{http_code} $r\n" "localhost:8600$r"; done   # attendu 200/303
```

Réseau : niveau **Trusted** de l'environnement cloud = registres de paquets + GitHub,
suffisant pour développer. Pour tester en vrai un appel Discogs/Bandcamp, passer
l'environnement en **Custom** et ajouter `api.discogs.com`, `bandcamp.com`.

## Deux sessions : dev cloud + diagnostic VPS

Deux sessions Claude travaillent sur ce projet, avec des accès complémentaires :

- **Dev cloud** (celle-ci) — code, PR, merge. Ne voit ni `/data`, ni les conteneurs,
  ni les jobs. C'est la seule à qui l'utilisateur parle.
- **Diagnostic VPS** (`session_01KbkY8jHGMbLLgkkQb8Kj6d`) — voit la base réelle, les
  conteneurs, les jobs, les logs. **Lecture seule sur le code et les données** : elle
  constate et prouve, elle ne corrige jamais. Son outillage de diagnostic vit dans
  `~/radar-diag/` sur le VPS (dépôt à part, versionné), jamais dans ce dépôt-ci.

La boucle : merge → déploiement auto → la session cloud réveille la diag (routine
`diag-vps`, déclenchée à la demande) → la diag poste son rapport en commentaire d'une
issue GitHub `Diag <sha>` → ça réveille la session cloud, qui corrige et fait la
synthèse à l'utilisateur.

Contrats : `.claude/skills/diag/SKILL.md` (côté VPS) et `.claude/skills/dev-loop/SKILL.md`
(côté cloud : quand déclencher, comment consommer). Trois allers-retours maximum par
déploiement, ensuite on escalade à l'utilisateur.

## Conventions

- **Avant chaque push** : `python3 -m py_compile` sur les fichiers touchés, puis lancer
  uvicorn en local (`CRATE_DATA_DIR` pointé sur un `data/` local) et `curl` les routes
  modifiées — vérifier 200/303, pas de traceback. Double du filet CI
  (`.github/workflows/ci.yml`), qui rejoue py_compile + import + balayage des routes.
- **Jamais `git add -A`** (a déjà committé un fichier de session par erreur). Ajouter les
  fichiers nommément. Relire `git status` avant de commiter.
- Commits, commentaires, messages : **en français**.
- Pas de commentaires superflus dans le code ; expliquer le *pourquoi* quand il n'est pas
  évident, pas le *quoi*.

## Pièges connus (ne pas refaire)

- **Marketplace Discogs** : prix livré, liste des annonces, décompte « en vente en
  France » — **inobtenable**. Pas d'API, page marketplace protégée par Cloudflare (403
  côté serveur, challenge en boucle même avec le vrai Chrome piloté). Abandonné. On garde
  le lien `🇫🇷 voir` + la pastille API (note ★, « dès X € hors port », num_for_sale).
- **Streamlit** : ne pas y reporter les évolutions de `radar_web`.
- **Bandcamp** (`radar/bandcamp.py`) : s'appuie sur `bcsearch_public_api`, endpoint
  **non documenté** — peut disparaître sans préavis. Le repli sur l'URL de recherche
  couvre ; ne pas partir en quête d'une « vraie » API Bandcamp (il n'y en a plus depuis
  2022).
- `discogs_get()` **ne lève pas d'exception** : renvoie `{}` sur réponse non-ok.

## Référentiel Discogs local (dump mensuel)

`radar_web/radar/discogs_dump.py` — index SQLite local (`discogs_dump.sqlite3` sous
`SHARED_DIR`, pas de serveur, un fichier comme les autres) construit à partir du dump
mensuel officiel Discogs (`data.discogs.com/data/{year}/discogs_{date}_releases.xml.gz`) :
catalogue (titre, artiste, label, catno, année, pays, formats, genres, styles, master_id),
**pas le marketplace** (vendeurs/prix/inventaire — ça n'existe pas dans le dump, reste
toujours en API live via `sellers.py`). Filtré au vinyle 12"/LP uniquement (`_is_vinyl`),
~18-20M sorties tous formats dans le dump réduites à un sous-ensemble gérable.

Rempli par le job `import_discogs_dump` (`crate_jobs.py`) : télécharge (reprise via
`Range` si interrompu), vérifie la somme de contrôle de chacun des 3 fichiers
(`CHECKSUM.txt`, best-effort — absence n'empêche pas l'import), parse en flux
(`ET.iterparse`, `root.clear()` **et** `elem.clear()` après chaque élément — memory-leak
sinon sur un fichier de cette taille), reconstruit l'index en entier (pas de delta, un
dump mensuel est toujours un instantané complet) dans `discogs_dump.sqlite3.new`, jamais
dans le fichier servi par l'appli : les index ne sont créés qu'une fois la table remplie
(`ANALYZE` ensuite), puis bascule atomique (`os.replace`) à la toute fin. L'appli lit
l'ancienne base valide jusqu'à la dernière seconde — pas de fenêtre où `available()`
mentirait pendant les ~1h45 que dure un import. `journal_mode=WAL` + `synchronous=OFF`
pendant la construction (vrai gain de vitesse), repassé en `DELETE` juste avant la
bascule — le fichier livré n'est jamais en WAL (tous les lecteurs ne font que des
`SELECT`, un `-wal`/`-shm` orphelin au prochain remplacement mensuel serait un risque de
lecture corrompue). Bouton manuel dans Réglages ; veille automatique mensuelle via
`RADAR_DISCOGS_DUMP_SYNC=1` (à poser sur le `.env` du service `radar-worker` sur le VPS,
comme `RADAR_SELLER_SCAN=1`).

**Reprenable.** Chaque merge sur `main` redéploie le conteneur du worker — pas seulement
les merges qui touchent au dump — ce qui tue ce job s'il tombe en plein import. Un
checkpoint (`discogs_dump_import.state.json` : étape atteinte + position dans Releases)
survit à l'interruption ; au prochain lancement (même bouton, y compris « forcer »),
l'import reprend où il s'était arrêté au lieu de repartir de zéro. Releases (le gros
morceau, ~1h45) reprend précisément où il en était (les sorties déjà committées sont
retraversées sans être réimportées, pour retomber au même endroit dans le flux XML) ;
Labels/Artists (quelques minutes chacun) repartent de zéro sur eux-mêmes si interrompus,
sans jamais reperdre Releases. État purgé une fois l'import entièrement terminé.

Table `release_styles` (`release_id`, `style`) à part, indexée : la colonne `releases.styles`
(genres/styles joints par virgule) n'est interrogeable qu'en `LIKE '%…%'`, jamais par index.
`suggest_labels()` interroge `label_key` par plage (`>= préfixe AND < préfixe + '￿'`), pas
par `LIKE 'préfixe%'` — un `LIKE` sur une colonne insensible à la casse ne peut pas servir de
borne d'index (confirmé à l'`EXPLAIN QUERY PLAN` : `SCAN`, pas `SEARCH`, malgré l'index
présent).

Le job télécharge aussi les dumps `labels` et `artists` (86 Mo + 474 Mo, contre 10+ Go pour
`releases` — négligeable en plus) et les importe dans la MÊME base (`open_new_db()` /
`finalize_new_db()` : une seule bascule atomique à la fin, jamais de base à moitié à jour) :

- `labels` (`id`, `name`, `name_key`, `parent`) — le lien parent/sous-label n'existe dans le
  dump que dans un sens (`<sublabels><label id="X">` posé sur l'entrée du PARENT) ;
  `import_labels()` accumule id→nom et enfant→id_parent en mémoire pendant le flux et résout
  `parent` en un seul `UPDATE` à la fin.
- `artists` (`id`, `name`, `name_key`, `real_name`) + `artist_aliases` (`name_key`,
  `artist_id`) pour les `namevariations` (variantes de graphie du même artiste — les
  `<aliases>` du dump, autres identités avec leur propre entrée `<artist>` ailleurs dans le
  même dump, n'ont pas besoin d'un lien de plus : chercher leur nom résout déjà directement
  sur leur propre id via `artists`).
- `release_artists` (`release_id`, `artist_id`, `role`) — crédits par sortie, limités aux
  rôles qui pèsent dans le scoring (`Main`, `Producer`, `Remix`, `Written-By`, `Featuring`),
  remplie pendant le parsing des sorties (`<artists>` + `<extraartists>` filtrés). Branché
  (Lot 5) : `job_build_graph` construit le graphe de co-crédits par jointure SQL
  (`_graph_edges_from_sql`, `crate_jobs.py`) quand le référentiel local est disponible —
  aucun appel API, repli sur l'ancien comportement (`_graph_edges_from_api`, 1 seul saut)
  sinon. Deux fonctions dédiées dans `discogs_dump.py`, `label_ids_for_artists`/
  `artist_ids_for_labels`, interrogent directement cette table (sans passer par un job)
  pour le ranking labels/artistes — cf. `Ctx.label_db_signal`/`Ctx.artist_label_signal`
  plus bas.

  **Graphe multi-niveaux** (2026-09-04) : `_graph_edges_from_sql` fait une BFS bornée à
  `scoring.graph.max_levels` sauts (4 par défaut) depuis chaque graine — niveau 1 = co-crédit
  direct (poids plein), chaque niveau suivant multiplié par `scoring.graph.level_decay`
  (0.5) — plutôt qu'un seul saut. `scoring.graph.node_cap` (150) plafonne le nombre de
  nœuds explorés PAR GRAINE : un graphe de co-crédits est un "petit monde", sans plafond
  4 sauts depuis des centaines de graines toucherait l'essentiel du catalogue et ferait
  perdre tout son sens de "proche de mes goûts" à la reco (en plus d'exploser le temps du
  job). Le plafond ne tronque jamais les co-crédits DIRECTS d'une graine (toujours un seul
  aller-retour SQL, coût nul) — seule l'exploration plus profonde est coupée. Une sortie
  qui ne relie qu'à des nœuds déjà visités à un niveau égal ou antérieur n'est jamais
  recréditée (ni artistes ni labels) : chaque sortie ne compte qu'une fois, là où elle a
  été découverte pour la première fois.

  Nouveau mode `taste` (seul mode utilisé par l'entretien de fond, cf. plus bas) : graines
  = Cœur + Aimés + tous les artistes du corpus (toutes sources, DJ sets compris, dédoublonnés).
  `Ctx.seed_category_weight()` (`scoring.py`) pondère chaque graine selon sa provenance au
  moment du SCORE (pas de la construction, comme le reste du graphe — pas besoin de
  reconstruire si tu change un poids) : Cœur/Aimés (`scoring.graph.tier_w`, prioritaire),
  sinon la meilleure source où l'artiste apparaît dans le corpus (`scoring.sources` — mêmes
  poids que pour le score label : Bandcamp/collection Discogs > YouTube/Spotify > DJ sets),
  sinon le poids plancher (`tier_w.none`) pour tout autre artiste devenu graine (modes
  `top`/`global`, toujours disponibles).

  Le terme `graph` de `Ctx.ascore` (artistes) est normalisé par p95 + `log1p`
  (`_robust_scale`/`_log_ratio`, même principe que N5) plutôt que par le maximum brut de la
  population : sans ça, un seul artiste très prolifique avec une graine Cœur (ex. un alias
  à 30 sorties partagées) comprime le terme graphe de tous les autres candidats en tirant
  le maximum vers le haut, même ceux avec une vraie collaboration solide (ex. 10 sorties
  partagées) — cf. échange du 2026-09-04, exemple chiffré à l'appui.
- `resolve_name(name, kind, con=None)` — résolution de nom vers id Discogs canonique sans
  appel API (`name_key` direct, puis repli `artist_aliases` pour un artiste). **Encore
  inutilisée** par les jobs de résolution — prochaine étape (cf. `docs/etat.md`).
- `<label>`/`<artist>` en tags XML réapparaissent imbriqués dans le dump lui-même
  (`<sublabels><label id="X">`) : un compteur de profondeur dans `import_labels()`/
  `import_artists()` ignore les balises refermées à une profondeur non nulle (référence
  imbriquée), ne traite que celles qui reviennent à 0 (enregistrement racine).
- Champs XML des dumps labels/artists non vérifiés contre un vrai fichier (accès réseau à
  `data.discogs.com` indisponible depuis le sandbox de dev cloud, cf. plus haut) — écrits
  d'après le schéma documenté des dumps Discogs, à l'instar du parsing releases existant. À
  confirmer/ajuster au premier import réel sur le VPS.

## Entretien de fond (plus de boutons dans Réglages)

`RADAR_AUTO_MAINTENANCE=1` (même `.env` du service `radar-worker`) enfile automatiquement,
chacun à sa propre cadence : `canonicalize` (résolution canonique labels/artistes/corpus,
hebdo), `profile_labels` (profilage genre/style par label via l'API, hebdo), `build_graph`
en mode `taste` (graines = Cœur + Aimés + tous les artistes du corpus, alimente le ranking
labels/artistes de Mon univers, mensuel — cf. « Graphe multi-niveaux » plus bas). Ces trois
tâches n'ont plus de bouton dans l'interface — un utilisateur n'a plus rien à cliquer.
Cadence mémorisée dans `jobs/auto_maintenance.json` (survit aux redémarrages
du worker, donc aux déploiements).

`sellers.seller_affinity()` interroge ce référentiel (`lookup_release(release_id)`) pour le
genre/style réel de chaque sortie déjà repérée en inventaire vendeur (le `release_id` est
déjà connu gratuitement, aucun appel API en plus) ; repli sur le profilage label existant si
la sortie n'est pas encore dans l'index (pas encore importé, ou trop récente).

**Premier lancement réel (VPS, sept. 2026)** : `data.discogs.com` sert désormais les fichiers
via `?download=data/{year}/discogs_{date}_...` (paramètre de requête) et non plus via le
chemin direct `/data/{year}/...` — celui-ci répond 200 + une page HTML générique au lieu du
binaire. `find_latest_dump_date()` (repli HTML) trouvait déjà la bonne date malgré ça (la
regex matche le nom de fichier où qu'il apparaisse dans la page) ; seuls `dump_url()` et
`checksum_url()` étaient cassés. Corrigé. Le sondage mensuel (`_list_via_month_probe`, 3ᵉ
repli, pas atteint en pratique) est désormais durci : le host répond 200 pour à peu près
n'importe quel chemin, il faut en plus un `content-disposition: attachment`.

## Le « cerveau »

`radar_web/radar/scoring.py` — classe `Ctx` : affinités de style, `album_score`,
`ascore` (score artiste), re-scoring du graphe de producteurs, `reco_rows` (reco labels).
Recalculé à chaque requête à partir des fichiers de `/data` (cache mtime via
`store.load_cached`).

`reco_rows` (labels) et `ascore` (artistes) intègrent chacun un signal tiré du référentiel
local Discogs, sans appel API : `label_db_signal` (poids `reco.db_link`) — un artiste
Cœur/Aimés a un disque chez ce label (`discogs_dump.label_ids_for_artists`, direct,
disponible sans job) combiné au score du graphe de co-crédits (`job_build_graph`, pondéré
par palier) ; `artist_label_signal` (poids `artist_score.label_link`) — l'inverse, l'artiste
a-t-il un disque chez un label déjà suivi (base ou watchlist), via
`discogs_dump.artist_ids_for_labels`. Élargit l'univers classé : un label jamais possédé ni
écouté peut désormais apparaître dans `reco_rows` par ce seul signal.

## Jobs

`crate_jobs.py` — tâches longues (profilage, résolution, graphe, ingestion, veille),
lancées en sous-processus par l'appli. Statuts dans `/data/jobs/*.status.json`. Ne PAS
rebuild le conteneur pendant qu'un job tourne (ça le tue). Jobs reprenables via
`*.state.json`.

## RECOS RADAR (feature, sept. 2026)

Playlist YouTube auto-alimentée par les nouvelles sorties des labels suivis, scorée via
`Ctx.album_score`, écrite par OAuth2 (`radar_web/radar/ytwrite.py`, adapté du script perso
de l'utilisateur, flux "web application" plutôt que "installed app"), nettoyée par scraping
Playwright de l'historique de visionnage (`ytwatch.py`, session exportée manuellement via
`scripts/export_youtube_session.py`, pas d'automatisation de login Google). Livrée en 3 PR
(candidats / OAuth+écriture / nettoyage Playwright). Bug PKCE réel trouvé en prod par la
session diagnostic VPS et corrigé (`code_verifier` doit être transporté explicitement entre
`authorization_url()` et `exchange_code()`, via cookie côté `app.py` — deux objets `Flow`
distincts ne le partagent pas). Jobs : `scan_recos` (chaîné vers `publish_recos`),
`publish_recos`, `clean_recos` (volontairement PAS chaîné — `RADAR_RECOS_CLEANUP` distinct
de `RADAR_RECOS_SCAN`, tant que le scraping `/feed/history` n'est pas validé en conditions
réelles). Wantlist RADAR (deuxième feature du même chantier, lecture d'une playlist YouTube
pour peupler une file de validation vers la wantlist Discogs) : pas commencée.

---

# Archive détaillée — 11/09 → 19/09 (refonte scoring + suite)

Contenu déplacé de `CLAUDE.md` le 2026-09-19 pour le même motif que ci-dessus (nettoyage
demandé par l'utilisateur : trop d'historique PR par PR dans le fichier lu automatiquement).
`CLAUDE.md` garde désormais une version condensée par sujet ; le détail complet (mesures
VPS, numéros de PR, comptes de tests, diagnostics croisés cloud/VPS) est ici.

## Journal de session (chronologique, avant compression)

**Session du 19/09 (cloud)** :
- **PR à suivre** (pt 68) : recherche vendeur exhaustive — retour « filtres de style en ET » : fausse piste (OU déjà correct, prouvé, figé par test) ; vraie cause : troncature à 1000 articles. `/search` filtre désormais snapshot COMPLET du stock, lu une fois par nouveau job de fond `seller_inventory`.
- **PR #176** (pt 67) : correctif seuil `best_video_uri` — chemin « vidéo Discogs » du pt 64 rejetait à tort majorité des bonnes vidéos (seuil global `artiste+titre` inadapté, uploadeur répète rarement l'artiste). `min_overlap` désactivé (`=0`) pour les deux appelants, seuil de titre relevé 0.5→0.6, garde-fou anti dub/remix ajouté. Codé + testé hors-ligne (135 tests) + smoke local. **Mergée** (`fb2cd1d`), **reste à vérifier VPS réel** (rejouer mesure du brief, relever compteur A/B corrigé).

**Session du 18/09 (cloud)** :
- **PR #170** (pt 61) : correctif B1 — `label_style_counts` renvoyait `{}` au-delà de 999 clés, tuant en silence le terme `affinity` pour ~90 % de l'univers. **Mergée.**
- **PR #171** (pt 62) : M1 couches 1 et 3 — `reco_index` dissocié de `reco_rows` (787 Mo → 20 Mo) + cache disque `recoindex.py` (supprime les 183 s à froid). Couches 0 et 2 du brief écartées sur décision utilisateur. **Mergée et déployée** (`d81a36b`, conteneurs recréés à 09:06 UTC).
- **PR à suivre** (pt 63) : contrat `vps-ops` élargi — session VPS peut basculer drapeaux `RADAR_*` du `.env` (sauf 5 clés sensibles en liste noire) avec accord préalable pour activation, tient mémoire persistante versionnée dans `radar-diag`. Aucun code applicatif.
- **Fait côté VPS dans la nuit du 17 au 18** (rapporté par session VPS, pas vérifié depuis le cloud) : `prune_labels apply=1` avec `max_known=9`/`min_pct=25` → **4 697 labels retirés, 5 490 gardés**, 0 possédé/écouté touché (pt 58 clos) ; **`build_catalog_labelgraph` sur dump COMPLET** (19,4 M sorties) → 41 min, 2,27 Go, 863 799 labels, 16 692 433 liens dont 139 569 parent/enfant (99,7 % des 140 011 du dump). Graphe **construit et lu en prod depuis le 17/09 21:59** : 3ᵉ composante du pt 60 (voisinage) n'est plus inerte.

**Session du 17/09 (cloud)** :
- **PR #162** (pt 55) : playlist RECOS — colonne « Track », repli vinyle par titre de PISTE, exclusion albums déjà en collection. **Mergée et déployée** (`751d9ed`).
- **PR #163** (pt 56) : « Nouveautés » (/veille) ET « Vendeurs » mises en PAUSE via `radar_web/radar/features.py`. **Mergée et déployée** (`ead6964`).
- **PR #165** (pt 57) : perf `/univers?tab=cart` (~190s→instantané) + route `/wantlist` dédiée. **Mergée et déployée** (`8dae861`).
- **Chantier C** (pt 58, 17/09) : job CLI `prune_labels` (élagage `label_categories` hors goût courant, seuils `max_known=5`/`min_pct=5.0`, suppression sèche, simulation par défaut) — codé, testé hors-ligne, **reste à lancer côté utilisateur sur VPS** (simulation puis `apply=1`).
- **Piège rencontré en ouvrant PR #165** : branche `claude/hello-e87dpo` portait encore son propre commit `034be62` (docs refresh session précédente), déjà mergé sur `main` sous autre sha (`683c8cf`, PR #164) — même contenu, hash différent (squash/re-commit côté GitHub). Merge direct aurait fait vrai conflit sur `CLAUDE.md` (deux insertions indépendantes du même bloc). Corrigé en rebasant `claude/hello-e87dpo` sur `origin/main` avant d'ouvrir la PR : `git rebase origin/main` a reconnu `034be62` comme déjà appliqué (contenu identique), l'a sauté seul, ne rejouant que le vrai commit de cette session. Push `--force-with-lease` donc sûr (contenu strictement identique moins doublon). À refaire si cas se représente (branche de travail gardant vieux commit déjà mergé ailleurs sous autre sha).
- **Action utilisateur attendue avant vérif pt 55** : relancer `fetch_collection` (Mon profil) — sans ce passage, filtre « déjà en collection » reste inactif — puis `scan_recos force=1`.

## Refonte scoring — chantier 37-43 (11/09, diagnostic Opus + arbitrages utilisateur)

37. **Lot 1 — labels Cœur(T1)/Aimé(T2)** : `cfg["labels"]`/`["watchlist"]` (2 listes plates) → `cfg["label_categories"]={"1":[...],"2":[...]}` (même modèle qu'`artist_categories`). Migration auto (`store.load_config`) : base→Cœur, watchlist→Aimé dédoublonné. `Ctx.label_tier_map()` remplace tous les accès directs. **Changement de comportement assumé** : `job_scan_veille` couvrait avant SEULEMENT le watchlist — désormais TOUT label suivi (Cœur+Aimé). UI `/univers` : sélecteur Cœur/Aimé + colonne Catégorie, `/reco/label?tier=1|2`, import/export CSV avec catégorie. Route `/univers/labels/import` (dupliquée, morte) supprimée. Nouveaux labels détectés auto → Aimé par défaut. **Mergé PR #132.**
38. **Lot 2 — graphe labels global** (`catalog_labelgraph.py`, nouveau module) : cartographie label↔label sur le référentiel Discogs PARTAGÉ — PAS `labelgraph.py` existant (sous-graphe par-utilisateur, nom distinct tranché avec l'utilisateur). Deux types de lien (colonne `kind`, jamais mélangés) :
    - `"artist"` (non dirigé) : labels partageant un artiste crédité — flux SQL trié, `Counter` flushé tous les 20 000 artistes, UPSERT. Garde-fou `MAX_LABELS_PER_ARTIST=40` (artiste écarté entièrement si dépassé, compte journalisé).
    - `"parent"` (dirigé, a=enfant/b=parent) : hiérarchie Discogs (`<sublabels>`) — nouvelle colonne `labels.parent_id`.
    Stocké `catalog_labelgraph.sqlite3` séparé, MÊME bascule atomique que `discogs_dump.py`. Job `build_catalog_labelgraph` : no-op si déjà à jour pour le dump courant. Déclencheur événementiel `worker.py` (opt-in `RADAR_CATALOG_LABELGRAPH=1`, check horaire).
    **Mergé PR #132. Testé VPS à 2 échelles** (5000 sorties → 1831 labels/3179 liens ; 100 000 sorties → 23 728 labels/153 271 liens).
39. **Référentiel élargi TOUS formats** (décision utilisateur, suite pt 38) : `discogs_dump.py` gardait avant SEULEMENT le vinyle 12"/LP — désormais TOUTE sortie gardée, nouvelle colonne `releases.is_vinyl`. `search_local()` RE-filtre `is_vinyl=1` depuis pt 45 (alimente `/search` et `job_scorestore_releases`→RECOS/wantlist). **Mergé PR #132** (même PR que pt 38). **Testé VPS à 2 échelles** : 5000→3568 vinyle/1432 non-vinyle ; 100 000→73 026 vinyle (73,0%).
40. **Import TEST à durée limitée** (outil de validation jetable, CLI seulement) : `params["limit"]` sur `import_discogs_dump`, écrit dans une base séparée jamais le référentiel réel. **3 runs VPS réussis** (5000 puis 100 000 sorties, chiffres cohérents). **Outil retiré (12/09)** : décision utilisateur de sauter l'étape test et lancer directement le réimport complet.
41. **Lot 3 — DAG explicite de `Ctx`** : demande initiale "casser un cycle circulaire" — aucun vrai cycle trouvé (objectif reformulé : rendre le DAG EXPLICITE). `NODE_DEPS` + `topological_order()` vérifié à l'import + `Ctx._memo()` vérifie à l'exécution. Aucun changement de comportement (scores identiques avant/après, vérifié). **Mergé PR #136.** Pur refactor.
42. **Lot 4 — précalcul scorestore** (nouveau module `scorestore.py`, par utilisateur) : précalcule `Ctx.album_score()` pour chaque sortie des labels suivis puis chaque piste des mieux notées. 2 tables SQLite WAL (`release_scores`/`track_scores`). 2 jobs chaînés : `scorestore_releases` → `scorestore_tracks`. Worker opt-in `RADAR_SCORESTORE=1`, check 6h. **Mergé PR #139/#140/#141. Vérifié VPS réel** : 378 208 sorties notées, +112 pistes/20 sorties, 0 erreur. **Clos.**
43. **Lot 5 — RECOS lit `track_scores`** : `job_scan_recos` refondu — lit directement `scorestore.track_scores` JOIN `release_scores` au lieu de rescanner Discogs. Chaque piste porte SON PROPRE score. Conséquence : `job_scan_recos` ne fait PLUS AUCUN appel réseau. `force` reconstruit la file depuis les scores actuels. **Mergé PR #143.**

## Points 44-68 (14/09 → 19/09)

44. **Piège `CRATE_DATA_DIR` par défaut sur la racine du repo** (`radar_web/radar/paths.py`, `crate_jobs.py`) : réimport complet du dump (12/09) lancé à la main sur le VPS sans exporter `CRATE_DATA_DIR` → tout écrit dans `<repo>/shared` et `<repo>/jobs` au lieu de `<repo>/data/shared`/`<repo>/data/jobs`, seul dossier monté par Docker. `crate_jobs.py` dupliquait la même logique en dur (pas d'import de `paths.py`). Corrigé : défaut `<repo>/data` dans les deux fichiers + `DATA` loggé au démarrage.
45. **Retours issue #62 traités (14/09)** : Marketplaces `/search` déjà résolus par pt 24 ; « Mes sources » renommé « Mon profil » partout ; `/reco-radar` 🗑️ suppression sans confirmation navigateur ; bouton wantlist de `/reco-radar` ajoutait parfois une sortie non-vinyle — corrigé, `search_local()` refiltre `is_vinyl=1`.
46. **Retours issue #62 (14/09, suite)** : capacité max playlist RECOS 100→300 ; « Mon profil » déplacé en haut à droite ; file de jobs avec priorité (`radar_web/radar/jobs.py`, `worker.py`) — champ `priority` (1=interactif, 0=fond), `worker._pick()` sert la priorité la plus haute d'abord. Pas de vrai parallélisme, exécution sérielle à dessein (rate-limit Discogs partagé).
47. **RECOS RADAR bloquée à 0/240 — diagnostic VPS avec données/API réelles (15/09)** : `_maybe_recos_scan` avait un `return` sec en tête (pause du 10/09) ignorant `RADAR_RECOS_SCAN=1`. Retiré. `RECOS_DAILY_SEARCH_BUDGET=45` ne bornait avant que la LONGUEUR de `recos_candidates.json`, jamais le nombre réel de recherches/jour — corrigé (compteur persistant `recos_search_budget.json`, minuit Paris). Artiste "Various" par piste (2ᵉ occurrence) : CAS A (Discogs ne fournit aucun crédit par piste) et CAS B (piste littéralement créditée "Various" dans une sortie par ailleurs créditée) — `_is_placeholder_artist()` traite ces valeurs comme absence de crédit ; piste sans artiste exploitable ÉCARTÉE des candidats RECOS. `_is_continuous_mix()` filtre aussi les megamix.
48. **Amendement au diagnostic du pt 47 (VPS, 15/09)** : tracklists PRÉSENTES dans le XML brut du dump (26,1% des sorties avec crédit par piste) — `discogs_dump.TRACKS_DB_PATH` (base séparée, ~5-10 Go extrapolés) remplie dans la même passe XML, `job_scorestore_tracks` lit le dump local en priorité (zéro appel réseau), repli API seulement pour sortie absente du dump. Filtre vinyle déplacé : `search_local(vinyl_only)` — `job_scorestore_releases` passe `vinyl_only=False` (découverte exhaustive tous formats), `/search` reste vinyle seul ; filtre vinyle fait désormais à l'AJOUT wantlist (`cart_add` cherche un pressage vinyle si la sortie proposée n'en est pas un). Backup : exclusion généralisée à `shared/*.sqlite3*` (récidive du piège pt 12 évitée avant qu'elle n'arrive).
49. **Gap `_is_placeholder_artist` (15/09)** : ratait "No Artist"/"Various Artists (N)"/"Unknown Artist (N)"/"Unknown". 1er correctif (fusion avec `ARTIST_STOPWORDS`) annulé — trop large, aurait cassé des artistes réels homonymes (ex. "Release (22)", vérifié réel via l'API). 2ᵉ correctif retenu : set dédié `_PLACEHOLDER_ARTISTS` distinct d'`ARTIST_STOPWORDS`.
50. **Budget de recherches RECOS : 45→80 et horloge recalée sur le Pacifique (16/09)** : `RECOS_DAILY_SEARCH_BUDGET` 45→80. Piège horloge : le compteur datait sur `Europe/Paris` alors que le quota YouTube se remet à zéro à minuit PACIFIQUE (9h Paris) — ~9h de recherches bloquées pour rien chaque jour. Corrigé (`YT_QUOTA_TZ`).
51. **Rate-limit YouTube confondu avec quota journalier (16/09)** : `_QUOTA_REASONS` mélangeait quota journalier réel et limite de débit transitoire, les deux levant `QuotaExhausted` et faisant abandonner tout le run. Séparé en `_DAILY_QUOTA_REASONS`/`_RATE_LIMIT_REASONS` (nouvelle exception `RateLimited`), retry+backoff exponentiel sur la même clé, throttle ~3 req/s. Budget ne décompte plus les recherches rate-limited.
52. **Angle mort symétrique du pt 51 (16/09)** : Google peut renvoyer `reason=rateLimitExceeded` pour un vrai quota JOURNALIER ("Search Queries per day") — seul le `message` tranche, pas la `reason`. `_error_kind` vérifie désormais le message contre un marqueur volontairement étroit (`"per day"`, pas le préfixe générique qui aurait réintroduit le bug inverse). `any_rate` testé avant le quota journalier sur plusieurs clés.
53. **4 correctifs suite relecture VPS des PR #158/#159 (16/09)** : BUG 1 (le plus important) — une exception réseau/API non classée faisait perdre tout l'état de fin de run (`recos_candidates.json`/budget), déplacé dans un `finally`. BUG 2 — `QuotaExhausted` incrémentait à tort le compteur de recherches. BUG 3 — un rate-limit sur un candidat ne coupait pas le run, chaque candidat suivant rejouait son propre backoff (jusqu'à 70s perdues/cycle) ; nouveau flag `rate_hit`. BUG 4 — docstring seulement (`_throttle` par-processus, pas partagé). Premiers tests committés (`tests/`, 22 cas).
54. **Mon profil (`/patte`) 135s à froid (diagnostic VPS 17/09)** : `stats()["artists_identified"]` forçait `ascore` → tout le graphe producteur pour un simple compte affiché dans 1 vignette. Décision utilisateur : les 2 vignettes concernées supprimées (`stats()` ne touche plus `ascore`/le graphe). `Ctx._key` n'empreinte plus que les sous-arbres de config réellement lus (`scoring`/`artist_categories`/`label_categories`/`taste_categories`). **Correction ultérieure (cf. pt 57)** : la cause réelle des 132s n'était PAS `graph_rescore()` (2,23s mesurés, négligeable) mais `artist_label_signal` → deux `GROUP BY` sur 19,4M/38M lignes — le correctif (suppression des vignettes) restait valide, seule l'explication initiale était fausse.
55. **Playlist RECOS : colonne « Track », vinyle introuvable, disques déjà possédés (17/09)** : colonnes Artiste+Piste fusionnées. Incohérence Release/Label signalée par l'utilisateur vérifiée FAUSSE (les deux affichaient la vérité Discogs, la piste vient d'une compilation). Vrai bug : le bouton wantlist postait le titre de la SORTIE au lieu du titre de la PISTE pour la recherche vinyle (`discogs.search(track=…)` filtre sur track) — corrigé, nouveau champ `track` prioritaire. Filtre albums déjà possédés : `job_fetch_collection` mémorise `owned_release_ids`/`owned_release_keys` (collection seulement, jamais la wantlist), `job_scan_recos` écarte par id ET par identité artiste/titre. **Mergé PR #162, déployé (`751d9ed`).**
56. **Nouveautés (/veille) et Vendeurs mis en PAUSE (17/09, décisions utilisateur)** : drapeaux `radar_web/radar/features.py` (`VEILLE_ENABLED`/`SELLERS_ENABLED`, tous deux `False`), lus par le web ET le worker (processus séparés, pas de source commune sinon). Routes en 404, jobs `scan_veille`/`scan_sellers`/`scan_catalog` retirés de `VALID_JOBS` et de la boucle hebdo worker. Rien supprimé (templates/routes/jobs CLI restent) — réactivation = drapeau `True` + redémarrage appli+worker. **Mergé PR #163, déployé (`ead6964`).**
57. **Wantlist ~190s à froid + route dédiée (17/09)** : `univers_page()` calculait les raccourcis de graphe labels (`top_aff`/`top_reco`) quel que soit l'onglet demandé — chaîne `reco_index`→`ascore`→`artist_label_signal`→deux `GROUP BY` sur 19,4M/38M lignes, 181s payés pour rien sur l'onglet wantlist. Corrigé : calcul limité à `tab=="labels"`, et `reco_index` du tri de table calculé seulement si le tri porte réellement dessus (`need_reco`). Nouvelle route `GET /wantlist` (sans `Ctx()` du tout) ; `/univers?tab=cart` redirige en 303. Volet C (écran de tri labels par `coverage`) volontairement PAS codé — remplacé ensuite par le job `prune_labels` (pt 58). **Mergé PR #165, déployé (`8dae861`).**
58. **Job `prune_labels` (17/09)** : élague `label_categories` des labels hors goût courant. Règle : combine `known` (absolu) ET `pct` (relatif, `known/tot` sur les styles reconnus), jamais un label possédé/écouté (garde-fous non désactivables). Suppression sèche (pas de 3ᵉ catégorie réversible — décision utilisateur, un tier "9" resterait lu de toute façon). Sécurité : simulation par défaut, `apply=1` sauvegarde la config avant + écrit un rapport JSON. CLI seulement, pas de bouton web. **Lancé en réel le 17/09 21:03** (`max_known=9`/`min_pct=25`) : 4 697 labels retirés, 5 490 gardés, 0 possédé/écouté touché. Sujet séparé non traité : catalogues géants (`Not On Label`, 1M+ sorties) qui passent la règle malgré un signal bruité par la taille — à chiffrer séparément.
59. **Périmètre de la session VPS élargi + contrat converti en skill actif (17/09)** : contrat auparavant dans `docs/archive/skill-diag.md`, gelé et jamais lu par la session VPS réellement utilisée (« Session VPS », pas « Radar — VPS (diagnostic) », archivée). Décision utilisateur : élargir ce qu'elle peut FAIRE (lancer/relancer jobs, commandes Docker changeant l'état) en gardant la séparation code (code/​`/data`​ en lecture seule pour elle, tout code passe par PR cloud) ; convertir le contrat en skill actif (`.claude/skills/vps-ops/SKILL.md`, auto-découvert par toute session Claude Code sur ce dépôt) plutôt que de le signaler à la main (solution manuelle qui ne survit pas à une nouvelle session).
60. **Élargir l'univers de labels — 3 chantiers dans `scoring.py` (17/09, brief VPS)** : (1) tier Cœur/Aimé désormais pondéré dans `reco_rows` (`scoring.label_tiers`, poids `scoring.reco.tier`) au lieu d'être un simple filtre d'appartenance — ne reclasse pas les labels eux-mêmes. (2) `reco_rows` n'incluait auparavant AUCUN terme au numérateur/dénominateur sauf s'il avait une donnée réelle — un label plafonnait très bas (24,7/100 mesuré) quelle que soit son affinité de style ; corrigé, chaque terme entre dans les deux seulement s'il a une donnée réelle. Changement non-additif assumé (reclasse toutes les recos existantes). (3) `catalog_labelgraph.neighbors_for()` câblé comme 3ᵉ composante de `label_db_signal` (voisinage, poids 0.2, à côté de direct 0.5/graphe producteur 0.3) — un label jamais possédé mais lié par artiste ou hiérarchie Discogs à un label suivi peut désormais remonter. Sans effet tant que le graphe catalogue réel n'est pas construit (fait dans la nuit du 17 au 18, cf. journal ci-dessus).
61. **B1 — `label_style_counts` renvoyait `{}` au-delà de 999 clés (18/09)** : bug silencieux — `IN (...)` avec autant de placeholders que de clés dépassait `SQLITE_MAX_VARIABLE_NUMBER`, l'`except` convertissait en `{}` indistinguable d'« aucun style ». `Ctx.label_affinities()` l'appelle avec la TOTALITÉ des clés de `reco_rows` (465 284 mesurées) — le terme `affinity` (le plus lourd) était donc absent pour ~90% de l'univers classé. Corrigé (découpage par lots, `_in_chunks`). Impact massif mesuré : bande 40+ passée d'environ 11 700 à 142 000 labels. Non-additif, à surveiller. **Mergé PR #170.**
62. **M1 — `reco_index` : 787 Mo de RAM et 183s à froid (18/09)** : `reco_index` dérivait de `reco_rows` (mémoïsé), gardant 787 Mo en mémoire pour un simple `{clé: score}` de 20 Mo. Couche 1 (mémoire) : `_compute_reco_rows` devient un générateur, `reco_index` n'en retient que le nécessaire. Couche 3 (temps) : cache disque par utilisateur (`recoindex.py`, `reco_index.json`+`reco_index_meta.json`), job CLI `reco_index`, opt-in `RADAR_RECO_INDEX=1` (pas actif par défaut — worker sériel, un job de 183s retarderait un clic utilisateur). Couches 0 et 2 du brief (seuil de coupe, poids min sur les liens graphe) délibérément pas faites (décision utilisateur).
63. **Contrat `vps-ops` élargi une 2ᵉ fois (18/09)** : la session VPS peut désormais basculer des drapeaux `RADAR_*` du `.env` — préfixe MOINS une liste noire de 5 clés sensibles (`RADAR_NO_AUTH`, `RADAR_WEB_BIND`, `RADAR_SECURE_COOKIE`, `RADAR_UID`, `RADAR_FEEDBACK_GH_TOKEN`) plutôt qu'une liste blanche trop stricte proposée initialement (qui se serait contredite). ACTIVER un drapeau demande l'accord préalable ; DÉSACTIVER pendant un incident reste autonome. Sauvegarde horodatée du `.env` dans `~/radar/` (jamais `~/radar-diag/`, qui est poussé sur GitHub). Mémoire opérationnelle persistante versionnée (`~/radar-diag/memory/`), distincte de `CLAUDE.md` (vérité projet, écrite uniquement par la session cloud).
64. **Playlist RECOS : tableau allégé, suppression sans rechargement, vidéo Discogs avant YouTube (18/09)** : colonnes Release+Label fusionnées, score en pastille. Suppression htmx sans recharger la page (appariement par `data-vid` au lieu de l'index de ligne). `job_publish_recos` essaie d'abord la vidéo déjà attachée par Discogs à la sortie (gratuit en quota recherche, juste 1 unité de vérification) avant la recherche YouTube — jusqu'à 40 appels Discogs/run. Défaut trouvé en écrivant les tests : le seuil d'appariement portait sur artiste+titre GLOBAL, une piste pouvait hériter de la vidéo d'une AUTRE piste de la même sortie — corrigé (seuil sur le titre de piste seul en plus). Version complète (parser `<videos>` du dump XML pour toutes les releases portant la piste, pas seulement celle du candidat) étudiée et reportée — nécessite réimport complet, décision à prendre une fois le taux de match mesuré.
65. **Recherche chez un vendeur Discogs (`/search`, 18/09)** : champ « Vendeur Discogs » change la source (stock du vendeur) — mêmes filtres appliqués ensuite via lookup local par identifiant (pas d'appel API par disque). Sortie absente du référentiel : gardée si aucun filtre ne porte dessus, écartée sinon (compte affiché). Filtre vinyle appliqué comme partout sur `/search`. Plafond initial `SEARCH_SELLER_MAX_PAGES=10` (1000 articles les plus récents) — repris au pt 68. Indépendant du catalogue de vendeurs en pause (pt 56).
66. **Bouton panier en mode vendeur → lien annonce Discogs (18/09)** : piste étudiée et vérifiée AVANT de coder — l'API Discogs publique n'expose aucun endpoint panier (confirmé via la doc d'un serveur MCP tiers couvrant toute la surface Marketplace). Décision utilisateur : lien direct vers l'annonce (`discogs.com/sell/item/<listing_id>`) plutôt qu'une fausse promesse d'automatisation. Rien stocké côté Radar (décision utilisateur), la mutualisation se fait entièrement côté panier Discogs.
67. **Le chemin « vidéo Discogs » du pt 64 rejetait la majorité des bonnes vidéos (19/09, brief VPS, sévérité majeure)** : `best_video_uri` imposait un seuil global artiste+titre EN PLUS de la couverture du titre de piste — un uploadeur qui ne répète pas le nom de l'artiste (la page Discogs le porte déjà) faisait plafonner une vidéo pourtant parfaite sous le seuil. Mesuré sur le brief : 7 sorties à vidéos correctes, 0 match. Corrigé : seuil global désactivé (`min_overlap=0`) sur ce chemin précis, seuil de couverture du titre de piste relevé (0.5→0.6), garde-fou anti dub/remix/instrumental/edit/live ajouté (une piste ne doit jamais hériter d'une version différente faute d'alternative, ni l'inverse). Le compteur A/B du pt 64 (vidéo Discogs vs recherche) était donc sous-estimé avant ce correctif — à re-mesurer avant d'arbitrer le chantier « vidéos du dump ». **Mergé PR #176 (`fb2cd1d`).**
68. **Recherche vendeur exhaustive (19/09)** : retour utilisateur « filtres de style en ET » — prémisse vérifiée FAUSSE avant de coder (le filtre était déjà un OU, figé par un nouveau test). Vraie cause : le plafond de 1000 articles (pt 65) écartait la quasi-totalité du stock d'un gros vendeur avant même le filtrage, donnant l'illusion d'un ET. Décision utilisateur : recherche exhaustive quel que soit le temps nécessaire. Nouveau job de fond `seller_inventory` : lit tout le stock une fois (`discogs.seller_inventory(max_pages=0)`), écrit un snapshot réutilisé par `sellers.py` (même format que le catalogue de vendeurs en pause) ; `/search` ne fait plus AUCUN appel API en mode vendeur, filtre le snapshot complet. Snapshot partiel (interruption) n'écrase jamais un complet déjà enregistré.

69. **Process d'amélioration des scripts (19/09)** : vaut pour TOUT script inscrit au registre, pas seulement la recherche YouTube.
    - **3 pièces** : skill `script-observe` (collecte des ÉCHECS de prod sur le VPS, pousse un lot sur `radar-diag`) ; agent `script-doctor` (diagnostic classé, écrit un fichier) ; skill `script-loop` (diagnostique, corrige, remesure, ouvre la PR). Socle mesurable : `scripts/loop/registry.json` (scripts éligibles + périmètre `touchable` modifiable) et `scripts/loop/bench.py` (verdict AMÉLIORATION/NEUTRE/RÉGRESSION).
    - **Qui fait quoi, décidé le 19/09** : la session VPS peut désormais mener la boucle ENTIÈRE (analyser, corriger, ouvrir la PR), pas seulement observer — 3ᵉ élargissement du contrat `vps-ops`. Elle travaille dans **`~/radar-work`, un second clone du dépôt `radar`**, jamais dans `~/radar` (le checkout que Docker fait tourner). La garantie « on ne touche pas la prod » vient du RÉPERTOIRE, pas du dépôt. La session cloud peut mener la même boucle depuis son clone habituel.
    - **Piste écartée, parce qu'elle ne marche pas** : « merger une branche de `radar-diag` dans `main` de `radar` ». Deux dépôts distincts sans ancêtre commun — GitHub refuse une PR entre eux. D'où le second clone de `radar` : la PR redevient une vraie PR intra-dépôt.
    - **Prérequis non vérifié** : pousser une branche sur `radar` depuis le VPS demande une clé de déploiement en ÉCRITURE sur ce dépôt ; celle du `git pull` de production peut être en lecture seule. À confirmer au premier essai (même nature que le prérequis clé `radar-diag` du pt 63).
    - **Pas de réveil automatique** (décision utilisateur, 19/09 — d'abord retenue puis écartée) : la collecte se déclenche à la main (`/script-observe <script>`). Cohérent avec la pause du pt 5, rien à réactiver.
    - **Règle qui tient tout le process** : aucun correctif gardé sans un cas de banc qui ÉCHOUAIT avant lui. Un cas qui passe déjà ne prouve rien et bloque le correctif. Anti-collision : une seule boucle ouverte à la fois par script, et la session qui ne la mène pas reste hors du `touchable` concerné.
    - **Leçon mesurée du 19/09, à ne pas réapprendre** : rejouer des SUCCÈS de production ne discrimine pas — les 25 cas capturés depuis `recos_playlist_history.json` passent encore avec le scoring entièrement court-circuité (sabordage vérifié). Marqués `kind: "floor"`, comptés à part, ils ne mesurent jamais un progrès ; seuls les `kind: "adversarial"`, construits depuis de vrais échecs, mesurent quelque chose. Deux autres pièges du même lot : 15 des 40 cas portaient un artiste placeholder « Various » (chemin mort depuis les pts 47/49) ; l'identifiant vidéo historique n'est pas une vérité terrain (plusieurs uploads valables — le code préfère la chaîne officielle « Artiste - Topic », ce n'est pas une régression).
    - **5 portes avant merge** (G1 gain mesuré, G2 cas prouvés, G3 rayon d'explosion ⊆ `touchable`, G4 suite projet, G5 CI), état écrit dans la PR. **Le merge reste une décision humaine** : il déclenche le déploiement prod, et le banc ne couvre qu'un chemin du script. L'utilisateur voulait un merge automatique ; écrire cette instruction dans un fichier du dépôt a été refusé deux fois par le garde-fou d'auto-mode — non contourné, à rouvrir explicitement s'il le souhaite toujours.
    - **Prochaine étape** : `/script-observe ytcache` côté VPS (le corpus n'a que 3 cas adverses, écrits à la main), puis `/script-loop ytcache`.

## Incidents et correctifs — 23/09 → 26/09 (déplacés de CLAUDE.md le 27/09/2026)

Récits détaillés des incidents et correctifs de cette période, déplacés depuis `CLAUDE.md`
(même motif que l'archive du 19/09 : `CLAUDE.md` ne conserve plus qu'un renvoi par point).
La numérotation d'origine est conservée pour ne casser aucune référence croisée
(« pt 46 », « pt 52 », …). Texte intégral ci-dessous — aucune perte de connaissance.

46. **Première boucle `/script-loop` menée jusqu'au merge : `ai_query`, PR #200 (24/09/2026)**. Chaîne complète `/script-observe` → `script-doctor` → correctif → PR → merge sur instruction explicite, exécutée depuis la session VPS. `script-doctor` a heurté le rate limit de session (429) avant d'écrire — diagnostic fait à sa place, même méthode. Finding retenue : `write_receipt()` (`scripts/ai_query.py`) traçait `status=error` sans le type ni le message — impossible de distinguer un 401 auth, un 429 quota, une cascade épuisée ou une réponse vide à partir du seul fichier de reçus. Correctif : deux champs `error_type` (slug : `cascade_exhausted`, `empty_response`, `syntax_error`, `http_<code>`, `api_error`) et `error_detail` (message tronqué 200 car.) ajoutés aux 3 sites `write_receipt(..., "error", ...)` de `main()`. 3 cas adversariaux ajoutés au banc (`scripts/bench_ai_query.py`), vérifiés échouants avant correctif : 12/15 → 15/15. Écarté du périmètre : les échecs auth/connexion (environnement, réglés par la passerelle du pt 44) et les timeouts (côté Google) n'appelaient aucun correctif de code. Vérifié en conditions réelles après déploiement (sha `38b5d4b`) : appel avec modèle invalide → vrai 404 Gemini → reçu contenant `error_type: "http_404"` et `error_detail`, mécanisme confirmé de bout en bout sur le code déployé, pas seulement sur le banc mocké.

47. **Deuxième boucle `/script-observe` + correctifs `ai_query` : PR #202 (24/09/2026)**. Observation : 69% des appels Gemini réussis avaient ≤10 chars de prompt (gate-gaming) ; le mode `diag` était 100% fantôme (45/45 à ≤7 chars). 5 findings, toutes traitées : (F1) seuil `GATE_MIN_PROMPT_CHARS = 50` dans `gemini_gate.py` — un reçu ok avec `prompt_chars < 50` ne débloque plus le gate (configurable via `RADAR_GEMINI_GATE_MIN_CHARS`, rétrocompat anciens reçus, porte de sortie `status=error` préservée) ; (F2) mode `diag` corrigé par F1 (ne peut plus être gamé) ; (F3) nouveau mode `read` (prompt d'analyse documentaire → JSON structuré : `title`, `sections`, `key_points`, `todos`, `dependencies`) ; (F4) cascades mises à jour avec `gemini-3.1-pro-preview` (heavy) et `gemini-3.5-flash` (fast) ; (F5) support `json_mode` + `--json-output` CLI (`responseMimeType: "application/json"` dans `generationConfig`, implicite en `--mode read`). Banc 19/19, gate 18/18, ai_query 83/83, suite complète 297 tests (seuls échecs pré-existants).

48. **Retours utilisateur sur PR #202 : garde-fou de lecture levé, chaîne passerelle+direct, télémétrie clarifiée (24/09/2026)**. Trois changements dans `scripts/ai_query.py`, un constat sans changement de code :
    - **Mode `read` sans exception de sujet** : le champ `uncertain` (liste `{location, question}`) s'ajoute aux 5 clés existantes du JSON — Gemini y signale tout passage dont il n'est pas sûr (nuance de sécurité, contrainte d'architecture, ambiguïté), avec un numéro de ligne exact quand il en a un. Les fichiers injectés via `-f` en mode `read` sont désormais numérotés (`numbered_lines()`, format `cat -n`) pour rendre ce pointeur exploitable — Claude ne rouvre alors que l'endroit signalé, jamais le document entier par défaut. Cf. l'exclusion réécrite au pt RÈGLE N°1 : la lecture est déléguée, le jugement sur l'architecture/la sécurité ne l'est pas.
    - **Chaîne passerelle → appel direct** (`query_gemini()`) : quand la passerelle locale du pt 44 est configurée, un échec de CE premier essai (panne réseau `URLError`, ou erreur HTTP relayée par la passerelle) déclenche désormais un second essai en appel direct à Google avec la clé locale, pour le MÊME modèle, avant de passer au modèle suivant de la cascade. Avant ce correctif, une passerelle éteinte ou en erreur épuisait toute la cascade de modèles sur un unique transport mort. Sans passerelle configurée (VPS/local), un seul essai comme avant — aucun changement de comportement sur ce chemin.
    - **Télémétrie `PostToolUse` : déjà complète, aucun changement de code**. Une note de `/script-observe` (24/09, lot élargi) affirmait que la télémétrie ne portait pas de champ `tool` exploitable — vérifié faux : le hook est bien branché sur `PostToolUse` dans `.claude/settings.json`, `scripts/hooks/telemetry.py` écrit `kind:"tool"` + `tool` + `label` + `ok` à chaque appel (confirmé sur les fichiers réels du 23-24/09), et `radar_ops/delegation.py` les agrège déjà par session (`tool_calls`, `top_tools`). Le constat de la note vient de l'outillage d'observation lui-même, qui cherchait un champ nommé `event` — absent, le champ s'appelle `kind`. Rien à corriger dans le produit ; à corriger, si besoin, côté `radar-diag` (hors périmètre de ce dépôt).
    - Tests : 6 nouveaux cas (`GatewayRoutingTests` ×4, `NumberedLinesTests` ×3, `MainTests` ×2 — dont un déjà existant modifié), `ai_query` 92/92, banc 19/19, aucune régression sur la suite (mêmes 9 erreurs `fastapi` + 1 échec `test_telemetry` pré-existants, liés à l'environnement de cette session et non au code).

49. **Correctif famine scorestore (24/09/2026)** : diagnostic posé sur les logs réels du worker (`docker logs radar-radar-worker-1`, horodatages UTC) après un retour utilisateur (« scan_recos/publish_recos/scan_catalog/scan_sellers semblent bloqués, scorestore_releases prend systématiquement le dessus »). Deux bugs distincts, tous deux dans `crate_jobs.py`, corrigés sans toucher `worker.py`/`jobs.py` :
    - **Priorité par défaut oubliée sur les jobs chaînés** : `jobs.launch(name, params=None, uid=None, priority=1)` défaute à l'interactif (1) — `_chain_scorestore_tracks()` et le chaînage `canonicalize`/`profile_labels` après `import_discogs_dump` appelaient `launch()` sans `priority=0`, doublant à tort les jobs de fond (0) d'autres utilisateurs déjà en file. Confirmé dans les logs : `scorestore_tracks` d'un utilisateur passait systématiquement devant les `scorestore_releases` en attente des 3 autres. Corrigé : `priority=0` explicite aux 3 sites.
    - **`job_scorestore_releases` sans plafond par lancement** : contrairement à `job_scorestore_tracks` (qui a déjà `SCORESTORE_LOCAL_BATCH_PER_RUN` + auto-rechaînage), `job_scorestore_releases` scorait TOUS les labels suivis d'un coup — ~17-23 min d'affilée mesurées en prod pour un profil à 561 labels suivis, contre un recheck (`SCORESTORE_CHECK_EVERY`) de 15 min : le cycle se re-remplissait systématiquement avant que `scan_recos`/`publish_recos` (horaires) n'aient leur fenêtre. Corrigé par extension du même motif : nouvelle constante `SCORESTORE_RELEASES_BATCH_PER_RUN` (100 labels/lancement, configurable via `scoring.scorestore.releases_batch_per_run`), reprise par curseur CUMULATIF persisté dans `scorestore_meta` (`label_cursor`, réutilise `scorestore.set_run_meta`/`read_run_meta` — pas de nouvelle table). Un tour est jugé complet quand le lot fait franchir un multiple du nombre de labels au curseur (`next_cursor // n > cursor // n`, robuste à un total non multiple du lot et à un label ajouté/retiré entre deux tours) : seul un tour complet chaîne `scorestore_tracks` et estampille la fraîcheur (`_stamp_scorestore_run`) ; un lot tronqué se rechaîne lui-même (`_chain_scorestore_releases`, nouveau, `priority=0`) sans attendre le prochain recheck à cadence fixe.
    - Scoping délibérément restreint : piste écartée (avis demandé à Gemini, cascade indisponible ce jour-là — 503 généralisé sur les deux tiers, reçus d'échec à l'appui) un scheduler générique à quota d'unités de travail pour TOUS les jobs (`scan_catalog`, `seller_inventory`, etc.) — jugé disproportionné pour une appli mono-utilisateur en pratique, et plusieurs jobs (`scan_catalog` notamment) n'ont aujourd'hui aucune notion de reprise partielle (« repris depuis le début » sur interruption, pas touché ici).
    - `scan_catalog`/`scan_sellers` bloqués depuis respectivement le 17/09 et le 10/09 (« interrompu par un redéploiement ») : diagnostiqués comme SANS RAPPORT avec ces bugs — reliquats de la pause Vendeurs (pt 21/33, `features.SELLERS_ENABLED=False`), `_maybe_weekly_scan()` retourne dès sa première ligne tant que le drapeau est faux. Rien corrigé, comportement voulu.
    - Tests : `tests/test_job_scorestore_releases_batching.py` (9 cas neufs — lot borné, rotation sans trou, rechaînage priority=0, tour complet, cas limites label vide/curseur corrompu), aucune régression sur la suite.

50. **Piège confusion `~/radar` / `~/radar-work` — régression télémétrie + clarification des contrats (24/09/2026)**. Retour utilisateur : plusieurs actions récentes avaient visé `~/radar` (checkout prod, lecture seule) au lieu de `~/radar-work` par erreur. Diagnostic : les deux checkouts partagent exactement la même arborescence git, donc toute commande à CHEMIN RELATIF (`python3 scripts/ai_query.py ...`, `python3 -m pytest ...`) réussit SANS AUCUNE ERREUR dans les deux répertoires — un `cwd` erroné ne produit aucun message, seul l'état modifié (receipts Gemini, historique git, données) diverge en silence selon le répertoire réel au moment de l'appel. En creusant ce jour-là :
    - **Un dossier `data/` existe réellement dans `~/radar-work`** (`jobs/`, `shared/`, `users/`, gitignoré donc jamais commité) — trace qu'un code y a déjà tourné avec `CRATE_DATA_DIR` non exporté (default `<repo>/data`, pt 27), en contradiction avec le contrat `vps-ops` (« jamais de `/data` monté [dans radar-work], c'est un atelier, pas un déploiement »). Non nettoyé faute de risque avéré (lecture seule, jamais consulté par l'appli réelle qui tourne dans `~/radar`) — signalé pour vigilance, pas traité comme un bug.
    - **Régression de télémétrie (introduite par la PR #204, 24/09 07:24 UTC)** : avant cette PR, `.claude/settings.json` portait `env.RADAR_TELEMETRY_DIR=/home/ubuntu/radar/data/ops`, qui forçait la télémétrie à toujours atterrir dans le vrai dossier ops de prod quel que soit le répertoire de la session. La PR #204 a retiré ce bloc (jugé « inutile » depuis la fusion des sessions, pt 45) au profit d'une auto-détection basée sur `CLAUDE_PROJECT_DIR` dans `scripts/hooks/telemetry.py::telemetry_path()` (repli sur `<projet>/data/ops` si le dossier existe, sinon `<projet>/.claude/telemetry.jsonl`). Cette auto-détection fonctionne dans `~/radar` (le vrai `data/ops` y existe, c'est le volume Docker) mais PAS dans `~/radar-work` (pas de sous-dossier `ops` sous le `data/` du point précédent) : le hook y retombait sur un fichier orphelin (`~/radar-work/.claude/telemetry.jsonl`) que plus personne ne transportait — le mécanisme `telemetry_ship.py`/`telemetry_pull.py` (branche git `telemetry`) avait été supprimé dans la MÊME PR, précisément parce qu'on le jugeait devenu inutile. Résultat mesuré : depuis le 24/09 7h24 UTC, toute la télémétrie produite en travaillant dans `~/radar-work` — c'est-à-dire l'essentiel du travail de code fait dans le respect du contrat `vps-ops` — était invisible de la page `/delegation` de `radar_ops`. **Corrigé** : réintroduction du bloc `env` dans `.claude/settings.json` (retour à l'état pré-PR#204) — un chemin absolu ne dépend pas du `cwd`, c'est délibérément la propriété recherchée sur un VPS à deux checkouts. La détection automatique reste en repli dans le code (inoffensive, utile si une session tourne un jour sans cet override).
    - **Contrats clarifiés** (`vps-ops/SKILL.md`, `ask-gemini/SKILL.md`) : avertissement explicite que les deux checkouts partagent la même arborescence donc qu'un chemin relatif ne signale jamais une erreur de répertoire, et consigne de vérifier `pwd`/`git remote -v` avant toute action qui écrit un état (commit, job, fichier) en cas de doute.
    - Cascade Gemini indisponible ce jour-là au moment du diagnostic (503 généralisé sur les deux tiers `heavy` et `fast`, même symptôme qu'au pt 49, reçus d'échec à l'appui, tentative faite avec contexte élevé et modèle le plus capable de la cascade avant d'y renoncer) : diagnostic et rédaction faits directement par Claude à partir de preuves de terrain (git log, diff du commit, arborescence réelle des deux répertoires).

51. **Retry avec backoff exponentiel avant repli de cascade (24/09/2026)**. Retour utilisateur : les 503 arrivaient systématiquement PENDANT la cascade (jamais sur les appels de l'agent PR, qui utilisent un modèle unique sans cascade) ; les 429 venaient d'un débit trop élevé plutôt que d'un quota journalier dépassé (RPM à limiter à 5, TPM à 250k selon les bonnes pratiques Google communiquées par l'utilisateur — 429/503 se gèrent par backoff+retry, pas par changement de modèle). Diagnostic confirmé : `query_with_fallback()` (`scripts/ai_query.py`) traitait un 429/503 exactement comme un 404 — bascule immédiate sur le modèle suivant — épuisant toute une cascade de 3 à 6 modèles sur un aléa qui se serait résorbé en quelques secondes.
    - **Correctif** : nouvelle fonction `_try_with_retries()` — chaque modèle (de la cascade ou passé en `--model` explicite) bénéficie désormais de `MAX_RETRIES_PER_MODEL = 2` tentatives supplémentaires avec backoff exponentiel (`RETRY_BASE_DELAY = 4.0`s, doublé à chaque essai, plafonné à `RETRY_MAX_DELAY = 30.0`s — calé sur RPM=5, soit ~12s entre requêtes) AVANT de basculer sur le modèle suivant. Nouvelle constante `RETRYABLE_STATUSES = {429, 503}` : retry sur ces deux statuts et sur tout 5xx générique et sur `RuntimeError` (panne réseau) ; **pas de retry sur 404** (modèle inexistant, retenter ne changerait rien) ni sur les 4xx d'authentification (400/401/403), qui échoueraient à l'identique. Le modèle explicite (`--model`) profite aussi du retry mais jamais du fallback — cohérent avec le contrat existant (l'appelant a demandé CE modèle).
    - Premier jet délégué à Gemini (`--mode code --model gemini-3.5-flash-lite`, cascade évitée à dessein pour ce prompt précis vu le sujet), relu et complété par Claude (retry appliqué aussi au modèle explicite, ce que le premier jet ne couvrait pas).
    - 8 nouveaux tests (`RetryTests`) + 6 tests existants adaptés au nouveau nombre de tentatives (`CascadeTests`, `time.sleep` mocké) — suite `ai_query` 108/108 nouveaux+existants sur ce périmètre (hors 4 échecs pré-existants sans rapport, clé `.env` locale qui pollue les tests « sans clé »).
    - Vérifié en conditions réelles : appel direct (`--model gemini-3.5-flash-lite`) et cascade complète (`-t fast`) aboutissent sans déclencher de retry (aucune surcharge au moment du test) — le mécanisme de retry lui-même n'a pas été observé en conditions réelles (nécessiterait un vrai 429/503 en direct, non provoqué ici).

52. **Correctif résolution des reçus Gemini : écriture, gate et lecture ops alignés (24/09/2026, PR #215)**. La page `/delegation` de `radar_ops` affichait des données gelées depuis le 24/09 07h16 UTC — les jetons Gemini ne se comptabilisaient plus. Cause racine : le commit e35af25 (PR #204) a supprimé le transport git (`telemetry_ship.py`/`telemetry_pull.py`) mais n'a jamais mis à jour le chemin d'écriture de `write_receipt()` dans `ai_query.py` — les reçus atterrissaient dans les dossiers `.claude/` de chaque checkout (`~/radar` 71 lignes, `~/radar-work` 529 lignes), séparés et invisibles du fichier ops partagé. `delegation.py::_receipts_path()` avait un piège supplémentaire : un `os.path.exists()` préférait indéfiniment une copie figée existante sur disque au lieu de pointer vers le dossier ops réel.
    - **3 fichiers corrigés** : `ai_query.py` (nouvelle `receipts_dir()` honore `RADAR_TELEMETRY_DIR`), `gemini_gate.py` (nouvelle `receipts_path_for()` avec exactement la même résolution — un écart entre écriture et lecture bloquerait le gate en permanence), `delegation.py` (`_receipts_path()` simplifiée, retire le repli `CLAUDE_PROJECT_DIR` mort et le piège `os.path.exists()`).
    - **Backfill des reçus orphelins** : 536 reçus uniques récupérés des deux `.claude/` et fusionnés (dédoublonnés par `ts`) dans le fichier ops partagé — 505 → 1041 reçus, page `/delegation` retrouve l'historique complet. Précédent pt 50 (pas de backfill) ne s'appliquait qu'à la télémétrie d'événements, pas aux reçus Gemini dont le gap masquait les jetons consommés.
    - 11 tests ajoutés (5 `ReceiptsDirTests` dans `test_ai_query.py`, 3 `ResolutionCheminRecuTests` dans `test_gemini_gate.py`, 3 `ReceiptsPathTests` dans `test_ops_delegation.py`). Suite complète 312 tests, zéro régression.
    - Vérifié en conditions réelles : fichier ops partagé a repris sa croissance dès le premier appel Gemini après le fix. `delegation.snapshot()` lit 1041 reçus et 795 événements, aucun fichier manquant.

56. **Contournement du gate par `git diff` brut + mode `search` trop littéral (26/09/2026)** : deux trous constatés en session mobile (captures à l'appui) — Claude a lu un `git diff -- f1 f2` en clair au lieu de le relayer à la passerelle (seul `git commit` était gaté, pas `git diff`), et `ai_broker.py --mode search` a échoué 3 fois d'affilée sur des requêtes légitimes (question en langage naturel, `num_have OR num_want`, alternatives `a\|b`) — `locate.chercher()` exige que la requête ENTIÈRE soit une sous-chaîne littérale (`_score_nom`/`_ligne_contenu` : `attribut in base`/`requete in ligne`, aucune tokenisation), donc 0 résultat malgré un fichier existant et pertinent (`app.py:1606`, jamais trouvé sans connaître le nom exact).
    - **Gate étendu** (`scripts/hooks/delegation_gate.py`) : `_is_real_diff`/`_relaie_au_courtier` (même principe que `_is_real_commit`, contenu cité retiré avant regex) — un `git diff` non relayé vers `ai_broker.py`/`ai_query.py` (pas de pipe, ou pipe vers autre chose comme `tail`) exige désormais un reçu récent (`pr`, `diag` ou `context`) ; la forme conforme `git diff ... | ai_broker.py --mode pr/diag --stdin` reste libre puisqu'elle EST déjà la délégation. Message d'erreur dédié (ne réutilise plus l'exemple `--mode pr` pour une simple exploration hors-commit).
    - **Recherche rendue exhaustive** (`scripts/ai/locate.py`) : nouveau repli tokenisé (`extraire_termes` + `_score_repli`), déclenché UNIQUEMENT quand la requête littérale ne trouve RIEN (zéro changement de comportement sur le chemin nominal, zéro coût s'il n'est jamais atteint) — découpe sur `' OR '`/`|`/`,` puis les espaces, filtre mots vides FR/EN + termes < 3 car., classe par nombre de termes distincts touchés (nom pondéré 3× plus que contenu). Un second parcours de l'arbre a un coût, mais réservé à ce cas déjà exceptionnel. Signalé dans la sortie via `resultat["fallback"]`/`["fallback_terms"]` pour la transparence. Vérifié : la requête `num_have OR num_want OR community` retrouve directement `app.py:1606`, et la question en langage naturel du screenshot fait remonter `results.html`/`search.html`/`app.py` dans le top 5 (contre 0 résultat avant correctif).
    - Premier jet des 4 fonctions ajoutées délégué (RÈGLE N°1, mode `code`, gratuit `openrouter:nvidia/nemotron-3-ultra-550b-a55b:free`), relu et intégré par Claude — scoring et point de déclenchement du repli (ne jamais mélanger les tiers littéral/OR, ne jamais relancer un parcours quand la tokenisation ne change rien) écrits à la main, chirurgicaux sur un fichier jugé stable (pt 55 le citait déjà comme dépendance du gate).
    - Tests : `RequiredModesGitDiffTests` (8 cas neufs, `tests/test_gemini_gate.py`), `tests/test_locate.py` créé (`ExtraireTermesTests` + `ChercherRepliTests`, 14 cas — premier fichier de tests pour `locate.py`). Suite complète 493 tests : mêmes 4 échecs + 9 erreurs pré-existants (pt 48/51, clé `.env` locale + `fastapi` absent), zéro régression.
    - Non vérifié en conditions réelles : que le gate `git diff` bloque effectivement en session (nécessite le matcher `PreToolUse` actif, cf. pt 55) sur le même scénario observé ; le repli `search` à l'échelle du dépôt entier (bornes `MAX_FICHIERS_DEFAUT`/`MAX_HITS_DEFAUT`) plutôt que le sous-ensemble `radar_web` testé en session.
    - Hors périmètre, volontairement : gater aussi `cat`/`head`/`tail`/`sed` en Bash (même classe de contournement du gate `Read`, mais pas observé dans les captures qui ont motivé ce correctif — risque de faux positifs sur des commandes anodines à évaluer séparément si le contournement se confirme en usage réel).


## TODO détaillé archivé le 19/09 (voir CLAUDE.md pour la version condensée courante)

- **Pt 68** : chercher un gros vendeur (ex. `hhv`), relever temps réel/nb de pages, vérifier que cocher un 2ᵉ style ÉLARGIT le résultat, surveiller le worker sériel pendant la lecture, tester le bouton « ■ arrêter », taille disque du snapshot.
- **Pt 67** : rejouer la mesure du brief sur `recos_candidates.json`, relever le compteur A/B corrigé sur 3-4 runs de `publish_recos` (décide le chantier « vidéos du dump »), écouter des pistes ajoutées par ce chemin (risque bascule côté faux positif), vérifier un vrai couple piste/vidéo dub-remix.
- **Pt 66** : cliquer le bouton panier en mode vendeur en réel, vérifier que le lien ouvre la bonne annonce et que « Ajouter au panier » Discogs fonctionne depuis ce lien.
- **Pt 64** : `/reco-radar` en portrait mobile, suppression sans rechargement (y compris piste en cours de lecture), compteur "via vidéo Discogs" vs "par recherche" au message de fin de `publish_recos`, vérifier qu'aucune vidéo hors-sujet n'entre par ce chemin, confirmer que le run continue en Discogs seul même budget YouTube épuisé sans allonger anormalement.
- **Pt 65** : nom de vendeur réel/inexistant, filtres qui réduisent réellement le stock, temps de réponse sur un gros vendeur, décision utilisateur en attente sur afficher prix/état.
- **Pt 63** : confirmer que « Session VPS » applique le nouveau périmètre (accord avant d'activer un drapeau, jamais de secret dans un rapport). Décisions utilisateur en attente : activer `RADAR_RECO_INDEX=1` et `RADAR_CATALOG_LABELGRAPH=1` dans le `.env` VPS.
- **Pt 61** : comparer un échantillon de scores `reco_rows` avant/après déploiement (effet massif attendu), rejouer la preuve du brief (~418 747 labels avec styles), confirmer qu'aucune route ne lève une erreur imprévue.
- **Pt 62** : mesurer le gain mémoire réel (`tracemalloc`), lancer le job CLI une fois et chronométrer la 1ère recherche après redémarrage, vérifier l'invalidation sur changement de réglage et l'absence de casse si les fichiers de cache sont supprimés.
- **Pt 60** : comparer un échantillon `reco_rows` avant/après, vérifier l'effet du curseur tier Cœur/Aimé, confirmer que des labels jamais vus apparaissent via la 3ᵉ composante (mesuré côté VPS : 5 380/5 490 graines avec au moins un voisin).
- **Pt 57** : temps de chargement réel de `/wantlist` à froid, redirection 303 de l'ancien lien, `/univers?tab=labels` toujours fonctionnel.
- **Pt 58** : confirmer `/univers?tab=labels` toujours fonctionnel après l'élagage réel (4 697 labels retirés).
- **Refonte scoring (pts 37-43)** : Lot 5 (0 appel réseau au scan, scores différenciés par piste, feedback qui reflète la bonne piste), Lot 1 (migration sans perte, `job_scan_veille` couvre tous les labels suivis), Lots 2/39 (réimport complet + graphe catalogue : FAIT le 17/09, reste `RADAR_CATALOG_LABELGRAPH=1` dans le `.env` et confirmer que `MAX_LABELS_PER_ARTIST=40` n'a pas été un goulot à cette échelle), pt 45 filtre vinyle (bouton wantlist propose le picker de pressages vinyle).
- **Pt 47/48/49** : `scorestore_tracks`+`scan_recos force=1` (0 appel réseau, artistes placeholder nettoyés, scores différenciés par piste, budget ≤80/j), taille finale `discogs_dump_tracks.sqlite3`, décider du sort de l'entrée "No Artist — Intro" déjà publiée en playlist live.
- **Pt 50/51/52/53** : budget/horloge Pacifique, rate-limit qui ne fait plus abandonner tout le run, quota journalier déguisé en rate-limit correctement classé, run qui ne perd plus ses fichiers d'état sur une exception réseau.
- **Pt 54** : `/patte` rapide à froid après déploiement, vignettes disparues, Reco artistes/labels/graphes toujours fonctionnels. Décision utilisateur en attente (proposée) : précalculer `ascore`/`graph_rescore` côté worker pour Mon univers/recos.
- **Pt 55** : picker de pressages vinyle sur la ligne "Inland Knights — 12 Till 8", `fetch_collection` puis `scan_recos force=1` sans piste d'album déjà possédé.
- **Pt 56** : plus de 404 sur les routes actives, `/veille` en 404, plus de bloc vendeurs sur la wantlist/Réglages, boucle hebdo `scan_catalog` qui ne lance plus rien.
- **RECOS — anciens correctifs jamais revérifiés en conditions réelles** (points 27-36 originels, 10/09) : budget=recherches, pas de double-scan, raison affichée si file pleine, retry avant abandon, sorties d'années variées, curseurs Réglages respectés, cas "Various"/candidats bloqués re-testés.
- **Ingestion réelle jamais testée depuis `_ingest_lookup_loop`** (PR #111, 10/09) : tester une vraie ingestion YouTube avant de clore le skill `factorize`.
- **Pt 46** : priorité de file en réel, curseur capacité RECOS jusqu'à 300 pris en compte, « Mon profil » visible en haut à droite sur mobile.
- **TODO code résiduel** (ancien pt 22) : multi-selects genre/style, `<label for>` non reliés, libellés FR Réglages, liens `/disco` reco/recherche, UI artistes « approx », suppression track/DJ dans Mes sets.
