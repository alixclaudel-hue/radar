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

## CLAUDE.md compacté le 27/09/2026 — texte d'origine des passages retirés ou condensés

Copie verbatim avant compaction (le CLAUDE.md courant n'en garde qu'un résumé, numérotation des points inchangée).

### En-tête, RÈGLE N°1 détaillée et « Workflow v3 » (ai_query.py, remplacé par ai_broker.py)

## ⛔ RÈGLE N°1 — DÉLÉGUER D'ABORD, NON NÉGOCIABLE

**La passerelle multi-fournisseurs (`scripts/ai_broker.py`) passe AVANT Claude sur toute tâche déléguable.** Elle ordonne ses candidats **gratuit d'abord** (OpenRouter gratuit, Gemini) et n'atteint un modèle payant (DeepSeek) que sur critère explicite et sous plafond journalier. Claude ne prend le relais qu'une fois la passerelle épuisée (quota, panne, sortie inexploitable) — jamais l'inverse, jamais « par commodité ». Le but est de brûler les tokens gratuits en priorité, puis de payer le moins possible.

C'est binaire, il n'y a pas à arbitrer :

| Situation | Mode | Tier | Action obligatoire AVANT toute autre |
|---|---|---|---|
| Écrire un nouveau module/script/fonction | `code` | heavy | `ai_broker.py --mode code --check-syntax -o <f> "<spec>"` |
| Écrire des tests | `test` | heavy | `ai_broker.py --mode test --check-syntax -f <cible> -o <test> "<spec>"` |
| Commit, corps de PR, note de release | `pr` | heavy | `git diff origin/main \| ai_broker.py --mode pr --stdin` |
| Log / trace / dump > 50 lignes | `diag` | fast | `… \| ai_broker.py --mode diag --stdin` (ou `--mode summary`) |
| Pré-digérer un ou plusieurs documents volumineux | `context` | fast | `ai_broker.py --mode context -f <f1> -f <f2>` (JSON structuré, cache disque) |
| Localiser un symbole dans le dépôt | `search` | fast | `ai_broker.py --mode search --root <dir> "<question>"` |
| Raisonner quand le gratuit ne suffit pas (payant justifié) | `reasoning` | heavy | `ai_broker.py --mode reasoning --effort high "…"` |

Le tier se déduit du mode (`code`/`test`/`pr`/`reasoning` → `heavy`, le reste → `fast`) : ne forcer `-t` que pour une raison précise.

**« J'ai zappé » n'est pas une explication recevable** — et n'est plus possible : le hook `PreToolUse` `scripts/hooks/delegation_gate.py` **bloque** le commit, l'écriture d'un test ou d'un nouveau module `.py` tant qu'aucun reçu de moins d'une heure **et d'au moins 50 caractères de prompt** n'existe (`.claude/gemini-receipts.jsonl`, écrit par le courtier à chaque appel). Ce seuil (`GATE_MIN_PROMPT_CHARS`, configurable via `RADAR_GEMINI_GATE_MIN_CHARS`) empêche le gate-gaming (prompts vides pour obtenir un reçu sans vraie délégation). Les reçus anciens sans champ `prompt_chars` restent valides (rétrocompatibilité). Une tentative **ratée** écrit aussi son reçu et débloque l'action : c'est la porte de sortie légitime, et elle exige d'avoir réellement essayé. Le même hook signale **sans bloquer**, en télémétrie, tout reçu récent estampillé `paid` sans justification (seul le mode `reasoning`, ou une escalade tracée, la justifie).

Restent hors délégation (et seulement ceux-là) : le JUGEMENT sur le code (décision produit, architecture, sécurité) et la relecture finale de ce que la passerelle a produit. Claude assume ce qui part en commit — `--check-syntax` prouve que ça compile, pas que c'est juste. La LECTURE elle-même (documents comme code) n'est plus exemptée par défaut — cf. pt 55 : au-delà d'un seuil de taille, un `Read`/`Grep` direct est gaté par `scripts/hooks/delegation_gate.py`, qui exige un reçu récent en mode `context`/`search` avant de laisser passer. **Exception explicite (26/09/2026)** : la sortie du mode `pr` (titre de commit, message, corps de PR) est postée TELLE QUELLE — ni Claude ni l'utilisateur ne la relit. La « relecture finale » non-délégable ci-dessus vise le CODE (mode `code`/`test`), pas la prose d'accompagnement d'un commit.

**Tous les skills et agents du projet intègrent la RÈGLE N°1** : chaque skill/agent porte une section « Délégation » qui précise quelles de ses étapes passent par le skill `delegate` (`.claude/skills/delegate/SKILL.md`, alias `ask-gemini`, commande `scripts/ai_broker.py`). Un skill qui ne mentionne pas la délégation n'est pas exempté — c'est qu'il n'a pas encore été mis à jour. Les modes `code`, `test`, `context`, `search`, `diag`/`summary`, `pr` et `reasoning` s'appliquent dans le contexte de chaque skill comme dans le flux principal.

**Pré-digestion de documents, sans exception de sujet** (pt 48, 24/09) : le mode `context` s'applique aussi aux documents d'architecture et de sécurité (`docs/architecture.md`, `SKILL.md`, `CLAUDE.md` lui-même) — il n'y a plus de garde-fou qui réserve ces sujets à une lecture directe par Claude. Ce qui reste non délégable, c'est le JUGEMENT sur l'architecture et la sécurité (ligne ci-dessus), pas la lecture qui le précède. Le mode `context` numérote les fichiers injectés (`-f`) et le JSON de sortie porte un champ `uncertain` (liste de `{location, question}`) : quand le modèle a un doute sur un passage, il pointe la ligne exacte (ou une citation courte à défaut) au lieu de se taire ou d'inventer une lecture assurée — c'est cet endroit précis, et lui seul, que Claude rouvre pour trancher. Une liste `uncertain` vide sur un document trivial est normale ; sur un document de sécurité ou d'architecture, ça vaut la peine d'un coup d'œil si elle est vide trop souvent. Le mode `context` **pave plusieurs documents en un seul appel** et **met les résumés en cache disque** : un document déjà analysé n'est pas renvoyé au modèle.

**Workflow par requête (27/09/2026, décision utilisateur)** : toute requête suit le skill `workflow` — 1. cadrage, 2. WBS, 3. ordonnancement (séquence vs parallèle), 4. délégation de chaque tâche au courtier (mode adapté) ou à un sous-agent frais (`executant` sonnet, `Explore` haiku — **jamais `fork`**, qui hérite de tout le contexte), consolidation par Claude. But : baisser le cache relu du fil principal (58 M jetons relus pour 1,3 M frais mesurés sur 20 requêtes, pt 58).

**Reprise de contexte : ce fichier suffit** — état, TODO, pièges ci-dessous.

**Historique complet + détails techniques** : `claude_archive.md` + `docs/archive/` (`.claudeignore`, non lus auto) — lire explicitement (`Read <fichier>`) si détail manquant (numéros de PR, mesures VPS précises, diagnostics croisés cloud/VPS). **Avant de lire un doc non listé ici** (nouveau fichier, `docs/archive/`, `claude_archive.md`) : demander à l'utilisateur si pertinent.

**Docs techniques scoring** (générés session cloud 11/09 depuis le code, pas de MAJ auto — relire si le code évolue) :
- `docs/app-overview-scoring-lot1.md` + diagramme (`docs/app-diagram-brief-scoring-lot1.md`, `docs/app-overview-scoring-lot1.excalidraw`) : vue d'ensemble `scoring.py` (`Ctx`), migration `labels`/`watchlist` → `label_categories`.
- `docs/app-overview-discogs-dump-py-and-scoring-process.md` : périmètre plus étroit, uniquement `discogs_dump.py` + `scoring.py`.

**Mode caveman par défaut** : skill `caveman` (niveau `full`) en début de session. Réponses conversationnelles seulement — code/commits/doc/tickets restent en prose normale, sauf demande explicite.

**Vérif par défaut (session cloud)** : smoke test hors-ligne seulement, jamais en conditions réelles (pas de token/accès réseau/SSH VPS) — sauf mention explicite « vérifié VPS » / « conditions réelles » sur un point.

**Délégation Gemini** : cf. RÈGLE N°1 en tête de fichier — obligatoire, vérifiée par hook, jamais laissée à l'appréciation du moment.

**Ce fichier a été nettoyé le 19/09** (demande utilisateur : trop d'historique PR par PR pour un fichier relu à chaque session). Convention désormais : ce fichier garde les grandes lignes, fonctionnalités, pistes étudiées/écartées et l'état déployé ; le détail par PR/session part dans `claude_archive.md` dès qu'un chantier est clos ou stable en prod.

## Règle d'économie de tokens — Délégation Gemini (Workflow v3)

Répartition des rôles : **Claude reste architecte et responsable sécurité** (conception, relecture, décision, commit) ; **Gemini absorbe le volume et les premiers jets** via `scripts/ai_query.py` (gratuit). Le tier se déduit du `--mode` — `heavy` (`gemini-3.8-flash`, quota étroit) pour `code`/`test`, `fast` (`gemini-3.5-flash-lite`, quota large) pour `pr`/`diag`/`summary`/`read`/`general`. Cascade de repli automatique si un modèle est indisponible (429/404/5xx).

1. **Logs et sorties terminal (> 50 lignes)** : ne pas ingérer de log brut dans le contexte Claude. Prétraiter — `docker logs ... | python3 scripts/ai_query.py --mode diag --stdin` (ou `--mode summary` pour compresser sans diagnostiquer). Claude ne lit que la synthèse.
2. **Scripts et tests unitaires** : premier jet délégué — `--mode code --check-syntax -o <fichier> "spec"` / `--mode test --check-syntax -f <cible> -o <test> "spec"`. Claude audite l'architecture, fait la chirurgie fine, et valide avec `py_compile` + `unittest` (une sortie qui compile n'est pas une sortie juste).
3. **Release, PR et doc** : `git diff origin/main | python3 scripts/ai_query.py --mode pr --stdin` renvoie TITRE COMMIT / MESSAGE COMMIT / CORPS DE PR / MAJ CLAUDE.md. Posté tel quel (26/09/2026 : ni Claude ni l'utilisateur ne relit ce texte — seul le code lui-même reste sous revue). Claude fait les `git add` ciblés (jamais `-A`), commit et ouvre la PR.
4. **Pré-digestion de documents** : `python3 scripts/ai_query.py --mode read -f <fichier>` renvoie un JSON structuré (`title`, `sections`, `key_points`, `todos`, `dependencies`). Permet à Claude d'exploiter un résumé plutôt que de charger un fichier volumineux dans le contexte. `--json-output` force le format JSON structuré sur n'importe quel mode.

**Garde-fou** : jamais de token, clé, contenu de `.env` ou donnée personnelle dans un prompt Gemini — service externe. Les `docker logs` et dumps peuvent en contenir : filtrer avant de piper. Si `GEMINI_API_KEY` et l'identifiant réseau de la session cloud sont tous deux absents : le dire et continuer sans délégation plutôt qu'inventer une réponse.

### Points 4-5 (sessions cloud/VPS)

4. **Session cloud** (rôle fusionné dans la session VPS depuis le 23/09, cf. pt 45 — ce point décrit l'ancienne séparation, gardé pour l'historique) : dépôt frais, sans `/data`/`.env`/token/Playwright/yt-dlp/SSH VPS/mémoire perso. Marche à suivre : éditer, `py_compile` + `python3 -m unittest discover -s tests`, smoke test, PR. Réseau Trusted = paquets+GitHub seul (Custom + `api.discogs.com`/`bandcamp.com` pour appel réel). Dépannage VPS sans accès = guider utilisateur pas à pas, jamais demander identifiants.
5. **Boucle diag VPS automatique en pause depuis 2026-09-06** (jugée non fiable) — trigger `diag-vps` désactivé, ne pas réactiver sans demande explicite. **Session VPS** (bridge interactive, différente de la boucle auto) : contrat dans `.claude/skills/vps-ops/SKILL.md`, skill ACTIF auto-découvert par toute session Claude Code sur ce dépôt. Périmètre : peut lancer/relancer des jobs, exécuter des commandes Docker changeant l'état (restart/rebuild), et depuis le 18/09 basculer des drapeaux `RADAR_*` du `.env` (hors liste noire de 5 clés sensibles : `RADAR_NO_AUTH`, `RADAR_WEB_BIND`, `RADAR_SECURE_COOKIE`, `RADAR_UID`, `RADAR_FEEDBACK_GH_TOKEN`) — **depuis le 23/09 (pt 45), plus d'accord préalable pour activer**, désactiver restait déjà autonome. Code applicatif et `/data` restent en lecture seule pour elle en écriture directe ; tout code passe par une PR ouverte depuis `~/radar-work` + CI verte — **depuis le 23/09 (pt 45), cette session le fait elle-même, il n'y a plus de session cloud séparée**, mais le merge d'une PR reste conditionné à une instruction explicite de l'utilisateur pour cette PR précise (inchangé, pt 40). Tient une mémoire opérationnelle persistante versionnée dans un dépôt privé séparé (`radar-diag`) — distincte de ce fichier, qui reste la seule vérité projet.

### Points 19, 24, 26, 31, 32 (versions longues)

19. **RECOS RADAR** : playlist **interne** (`recos_playlist.json`), pas une vraie playlist YouTube. Page `/reco-radar`, lecture via API IFrame Player. Dédoublonnage par IDENTITÉ DE PISTE (artiste+titre) contre `recos_history.json` (jamais purgé). Alimentation automatique horaire (`RADAR_RECOS_SCAN=1`) ; boutons manuels de scan/alimentation retirés de la page le 23/09 (retour utilisateur : « inutiles et fastidieux », force au besoin via VPS — cf. pt 42). Renouvellement : clic sur une ligne marque la piste "écoutée", purge à minuit Paris (pas de FIFO par ancienneté). `job_scan_recos` lit directement les scores précalculés (`scorestore.track_scores`, cf. pt 22) — **zéro appel réseau au scan**. `publish_recos` essaie d'abord la vidéo déjà attachée par Discogs à la sortie (gratuit en quota recherche) avant de chercher sur YouTube (cf. pt 32). Suppression d'une piste sans recharger la page. Exclut les pistes d'albums déjà possédés — collection Discogs ET achats Bandcamp (id + identité artiste/titre, jamais la wantlist, cf. pt 41). Wantlist RADAR (2ᵉ feature, lecture playlist YouTube → wantlist Discogs) : pas commencée.
24. **Boutiques vérifiées en vente** (`radar/volumo.py`) — **bouton 🛍️ retiré du gabarit le 22/09** (retour utilisateur issue #62 : « fonctionnalité à développer plus tard »), partout où il apparaissait (`/search` ET `/disco`, labels comme artistes — partial partagé `results.html`). Backend intact (`/release/stores`, `volumo.py`, `partials/stores.html`) : Volumo seule boutique intégrée (API JSON stable), écartés (Cloudflare/WAF/fermé) Beatport/Traxsource/Bleep/7digital/Junodownload — réactivation = remettre le bouton dans `results.html`.
26. **Référentiel élargi tous formats** (décision utilisateur) : `discogs_dump.py` garde toute sortie (colonne `releases.is_vinyl`), pas seulement vinyle — alimente le graphe/le profilage sur tout le catalogue. `search_local()` refiltre vinyle par défaut (`vinyl_only=True`), sauf `job_scorestore_releases` (découverte exhaustive tous formats, décision utilisateur « exhaustif à la découverte, vinyle à l'achat »). Filtre vinyle définitif fait à l'AJOUT wantlist (`cart_add` propose un picker de pressages vinyle si la sortie n'en est pas un).
31. **RECOS — budget/quota YouTube** (`ytcache.py`) : distingue quota journalier réel (`QuotaExhausted`, la `reason` Google peut mentir — seul le `message` tranche pour un cas précis) vs limite de débit transitoire (`RateLimited`, retry+backoff exponentiel sur la même clé, throttle ~3 req/s). Compteur persistant `recos_search_budget.json`, remis à zéro à minuit **Pacifique** (pas Paris — piège corrigé, le quota Google réel s'y réinitialise). Budget ne décompte jamais une recherche ratée/rate-limited. Un run interrompu par un aléa réseau ne perd plus son état de fin (fichiers écrits dans un `finally`).
32. **Piège `best_video_uri` trop strict** (`textmatch.py`, partagé par le bouton play `/search` et RECOS) : un seuil global artiste+titre écartait à tort la majorité des bonnes vidéos déjà attachées par Discogs à une sortie (l'uploadeur répète rarement le nom de l'artiste). Corrigé : seuil désactivé sur ce chemin, seuil sur le titre de piste seul relevé, garde-fou anti dub/remix/instrumental/edit/live ajouté (une piste ne doit jamais hériter d'une version différente faute d'alternative). Piste étudiée et reportée : parser les vidéos de TOUTES les releases portant une piste (pas seulement celle du candidat) demanderait de parser `<videos>` dans le dump XML + réimport complet — décision à prendre une fois le taux de match (vidéo Discogs vs recherche) mesuré en prod.

### Points 39-45

39. **Process d'amélioration des scripts** (19/09, vaut pour tout script inscrit au registre, pas seulement YouTube) : 3 pièces — skill `script-observe` (collecte les échecs de prod sur le VPS), agent `script-doctor` (diagnostic classé), skill `script-loop` (corrige, remesure, ouvre la PR), socle mesurable `scripts/loop/registry.json`+`scripts/loop/bench.py` (verdict AMÉLIORATION/NEUTRE/RÉGRESSION). 3ᵉ élargissement du contrat `vps-ops` : la session VPS peut désormais mener toute la boucle (analyser, corriger, ouvrir la PR) depuis un second clone `~/radar-work` (jamais `~/radar`, le checkout que Docker fait tourner) — piste étudiée et écartée : merger une branche du dépôt privé `radar-diag` dans `radar` (deux dépôts sans ancêtre commun, GitHub refuse). Règle qui tient le process : aucun correctif gardé sans un cas de banc qui échouait avant lui ; le merge final reste une décision humaine (5 portes : gain mesuré, cas prouvés, rayon d'explosion contenu, suite projet, CI). Premier corpus de test (`tests/fixtures/ytcache/`, 25 cas réels) : leçon retenue — rejouer des succès de prod ne discrimine rien, seuls des cas construits depuis de vrais échecs mesurent un progrès.
40. **4ᵉ élargissement du contrat `vps-ops`** (19/09, PR #184) : après une boucle `/script-loop ytcache` menée à bien côté VPS mais bloquée faute d'accès GitHub, la session VPS peut désormais ouvrir elle-même la PR de boucle (`gh pr create`) ET exécuter le merge (`gh pr merge`) — mais seulement sur instruction explicite et univoque de l'utilisateur donnée **pour cette PR précise**, jamais par une règle automatique (« si les 5 portes sont vertes, merge ») ni par un accord antérieur repris implicitement pour autre chose. Garde-fou rédigé identiquement dans `vps-ops/SKILL.md` et `script-loop/SKILL.md` étape 5. Prérequis ajouté : un token GitHub (PAT fine-grained scopé au dépôt) + `gh` CLI sur le VPS, stocké hors dépôt git — même famille que les clés SSH `radar-diag`/`radar-work`, absent ou insuffisant ⇒ le dire et s'arrêter.
41. **Lot de retours issue #62 du 20/09** (traité 22/09) : panneau « À vérifier » de `/univers` — les labels/artistes « jamais identifiés » n'avaient aucun moyen de suppression (texte seul) ; ajouté un bouton 🗑 supprimer par ligne (`/univers/review/{kind}/delete`) qui retire l'entrée à la fois de `resolved`/`artists_res` et de `label_categories`/`artist_categories`. `job_scan_recos` (`_owned_releases`) écarte désormais aussi les pistes d'albums déjà achetés sur Bandcamp (corpus `source=bandcamp`), pas seulement la collection Discogs. Texte de `/reco-radar` réécrit (toute référence à l'implémentation réseau/recherche retirée, l'utilisateur préfère regarder les logs). Tooltip du score d'affinité (`/univers?tab=labels`) précisé (seule la couverture en avait un). Vérifiés déjà résolus sans changement de code : capacité playlist 300 (pt antérieur au 14/09), persistance du vote 👍/👎 au rechargement, exclusion des tracks déjà en collection Discogs (pt antérieur au 17/09) — smoke-testés hors-ligne, à reconfirmer en conditions réelles (cf. TODO). Bouton 🛍️ « Boutiques vérifiées en vente » retiré du gabarit partout (search + disco, décision utilisateur suite à question posée sur la portée) — détail pt 24. `/search` (dernier retour du lot, confirmé en session) : 3 modes explicites mutuellement exclusifs (Recherche normale/tout le catalogue · 🏷️ Mes labels · Vendeur) au lieu de 3 champs coexistants implicitement — `search_run` (backend) inchangé, il priorisait déjà vendeur > mes labels > label seul ; seule la page montre/cache désormais le bon bloc selon le mode choisi (JS pur, vide les champs du mode quitté pour éviter qu'une valeur oubliée influence un autre mode).
42. **Suivi retours sur le lot #62 déployé (23/09, commit `7307b60`)** : trois retours utilisateur après déploiement. (a) Le bouton 🗑 supprimer du panneau « À vérifier » n'apparaissait que sur les entrées « jamais identifiées » (`not_found`) ; ajouté AUSSI sur les entrées « devinées » (`approx`, celles avec ✓/✗) — c'est le cas courant que voyait l'utilisateur. Backend inchangé (le endpoint delete gère déjà n'importe quel statut), simple ajout de bouton dans `partials/review.html`, verrouillé par `tests/test_univers_review_approx_delete.py`. (b) Page `/reco-radar` : texte raccourci à 2 phrases exactes et boutons manuels de scan/alimentation retirés (« inutiles et fastidieux ») — le scan et l'alimentation tournent en fond en continu (worker horaire, PAS 1×/jour à 9h : la description utilisateur était approximative, texte gardé conforme au réel), force via VPS si besoin. Liens de journaux et affichage d'état de job conservés. (c) « Liste artistes cat1/cat2 disparue » : diagnostiqué comme bug d'affichage transitoire (code de `/univers?tab=artists` et lecture `artist_categories` intacts, confirmé par l'utilisateur) — aucune modif.

43. **Appli de diagnostic `radar_ops` — LOT 1** (chantier en cours, 3 lots) : service Docker SÉPARÉ (`radar-ops`, port 8610, même image et même volume que `radar-web`, servi depuis le 27/09/2026 sur son propre sous-domaine public `RADAR_OPS_DOMAIN` (ex. `ops.hubclaudel.fr`) via Caddy, derrière la même authentification propriétaire que l'appli — l'accès Tailscale/loopback par `RADAR_WEB_BIND` reste en place en parallèle). Séparé à dessein : un outil de diagnostic qui meurt avec l'appli qu'il surveille ne sert à rien au moment où on en a besoin. **Lecture seule sur les données** (aucune route ne lance/arrête/modifie quoi que ce soit). 4 pages : jobs+file d'attente (temps réel SSE, **débit et ETA dérivés** par `radar_ops/sampler.py` — le fichier de statut n'est qu'un instantané sans historique), inventaire des bases JSON+SQLite, fraîcheur du scoring, journal des actions owner (temps réel SSE). Auth propriétaire uniquement (`uid != DEFAULT_UID` ⇒ redirection login).
    - **Authentification factorisée** dans `radar_web/radar/websession.py` (extraite d'`app.py`, partagée par les deux applis — une signature de session dupliquée finit par diverger). **Piège** : la couture de test a bougé, les tests patchent désormais `appmod.websession.dev_mode` et non plus `appmod._dev_mode` (9 fichiers de tests mis à jour ; patcher l'alias n'a aucun effet).
    - **Journal des actions** : `radar_web/radar/opslog.py` (écriture ET lecture dans le même module — le format d'une ligne est un contrat entre deux processus). Posé DANS `_guard` d'`app.py`, pas dans un second middleware : Starlette exécute `call_next` dans une tâche au contexte distinct, l'uid n'y serait pas garanti. Jamais la chaîne de requête ni le corps (mot de passe, jeton Discogs) — méthode, chemin, code, durée seulement. Rotation à 5 Mo (cf. pt 12, le VPS a déjà saturé deux fois sur des traces non bornées).
    - **Fraîcheur du scoring** : `job_scorestore_releases` estampille désormais la base (`scorestore_meta` : empreinte `scoring.derived_fingerprint`, détail nommé fichier par fichier, date, SHA du code, nb de lignes). La page répond « à jour / périmé / non estampillé » ET nomme ce qui a changé (« la configuration de scoring a changé », « producer_graph.json a été réécrit »). `scoring._derived_inputs()` nomme les entrées une seule fois — `derived_key` et `derived_key_detail` ne peuvent plus diverger.
    - **SHA du code** : `radar_web/radar/codeversion.py`. `.git` est exclu de l'image (`.dockerignore`), la valeur vient de `RADAR_CODE_SHA` posé par `deploy.yml` (aller ET rollback) ; repli sur `.git` en local/cloud ; jamais de SHA inventé — « inconnu » est une information.
    - **CI** : second balayage de routes ajouté pour `radar_ops` (`/health`, `/login`, `/`, `/bases`, `/scoring`, `/actions`) — cf. piège pt 6, `unittest` seul ne couvre pas les routes.
    - **LOT 2 fait (23/09)** — télémétrie du développement, brief complet dans `docs/brief-telemetrie.md` (référence du chantier, décisions actées incluses). Chaîne : capture par hook (`scripts/hooks/telemetry.py`, branché sur `SessionStart`/`UserPromptSubmit`/`PostToolUse`/`Stop`/`SessionEnd` dans `.claude/settings.json`, donc actif dans TOUTE session sur ce dépôt, VPS compris) → expédition git (`scripts/telemetry_ship.py`, branche `telemetry` = boîte aux lettres, commit sans parent poussé en force, **jamais branchée sur un hook**) → réception VPS (`scripts/telemetry_pull.py`, fusion incrémentale par `ts` dans `<data>/ops/remote/`) → page `/delegation` de `radar_ops`. **Piège corrigé avant déploiement** : la réception DOIT viser un sous-dossier distinct de celui que le hook alimente — sur le VPS une session locale écrit dans `<data>/ops/` à l'instant présent, le `ts` plancher serait toujours plus récent que les lignes expédiées et la réception les rejetterait toutes en silence. `delegation.sources()` lit les deux origines et les fusionne triées. Le reçu de `ai_query.py` porte désormais modèle, tier, replis de cascade, jetons `usageMetadata` et durée ; `gemini_gate.py` ne lit toujours que `ts`/`mode`/`status`, un reçu ancien reste valide. **Décisions utilisateur** : transport git seulement (pas d'endpoint HTTP) ; jetons Gemini mesurés, jamais d'estimation d'économie côté Claude ; texte des demandes en `head` (200 premiers caractères, `RADAR_TELEMETRY_PROMPTS` accepte aussi `none`/`full`) ; étiquettes d'outils gardées (chemins/motifs), commande `Bash` JAMAIS journalisée. Variables : `RADAR_TELEMETRY_DIR` (où le hook écrit), `RADAR_OPS_TELEMETRY_DIR` (où la page lit, défaut `<data>/ops`).
    - Lot 3 restant : schémas de flux (skills `diagram-brief` + `brief-to-excalidraw`). Reste aussi le skill `telemetry` d'orchestration.

44. **Passerelle Gemini pour session cloud** (23/09/2026) : la RÈGLE N°1 (Gemini d'abord) échouait systématiquement en session cloud claude.ai. Diagnostic : `GEMINI_API_KEY` est injectée par le proxy sortant, mais c'est un ADC OAuth (`AQ.Ab8...`) que Gemini v1beta refuse en 401 `ACCESS_TOKEN_TYPE_UNSUPPORTED`. L'ancien chemin « identifiant réseau `x-goog-api-key` » ne marchait pas non plus (le proxy réécrit ce header). Correctif : un mini gateway HTTP local `scripts/gemini_gateway.py` (stdlib pure, ~180 lignes) qui écoute sur 127.0.0.1:8632, lit la clé utilisateur posée dans CCR (`~/.claude-code-router/config.sqlite`, prioritaire sur la clé env qui est bidon en cloud), et l'ajoute en `?key=` en query (le proxy ne réécrit PAS la query, contrairement au header). `ai_query.py` route via ce gateway par un marqueur éphémère `/tmp/radar-gemini-gateway.url` — aucune variable à exporter, auto-détecté. Démarrage automatique par le hook `scripts/cloud-gemini-gateway.sh` branché sur SessionStart (dans `.claude/settings.json`), gardé par `CLAUDE_CODE_REMOTE=true` : sur le VPS et en local, chemin direct inchangé. La « porte de sortie » de la règle N°1 (reçu d'échec = déblocage) reste : si le gateway meurt ou la clé manque, le workflow n'est jamais bloqué. **Prérequis** : l'utilisateur doit avoir posé sa clé Gemini dans CCR (`~/.claude-code-router/config.json`, provider `gemini`, ou via l'UI `ccr ui`). 5 tests unittest dédiés (`GatewayRoutingTests`).

45. **Fusion session cloud + session VPS (23/09/2026)** : décision utilisateur — la session cloud (dépôt frais, `claude.ai/code`) est fermée. Cette session (VPS) cumule désormais son rôle : elle code, ouvre les PR et les merge, depuis `~/radar-work` (jamais `~/radar`, resté en lecture seule comme avant). Deux garde-fous du contrat ne bougent pas malgré la fusion — une tentative de les assouplir dans `vps-ops/SKILL.md` a d'ailleurs été bloquée par le classifieur de sécurité du harnais avant même l'arbitrage utilisateur, confirmant que ce n'est pas qu'une question de préférence :
    - **Merge d'une PR (boucle de script comme changement général)** : reste une décision humaine par PR précise (pt 40, inchangé) — la session ouvre et exécute le merge, mais attend toujours l'instruction explicite de l'utilisateur pour CETTE PR avant `gh pr merge`.
    - **`~/radar` et `/data` restent en lecture seule** : tout changement de code passe par `~/radar-work` + PR + CI, jamais un commit direct dans le checkout de production.

    Ce qui change réellement : (1) plus besoin d'une session cloud séparée pour du code hors script-loop (docs, contrat, feature) — la session VPS le fait directement, avec le même gate PR+CI+merge-sur-feu-vert ; (2) les drapeaux `RADAR_*` du `.env` (hors les 5 clés interdites du pt 5) s'activent désormais sans accord préalable — la session juge seule et documente le choix dans son rapport/journal (`radar-diag/memory/journal/`), seule la désactivation était déjà autonome avant.

    `~/.claude/settings.json` (VPS, hors dépôt, modifié par l'utilisateur seul) élargi en conséquence : `git -C ~/radar-work {fetch,checkout,branch,add,commit,push,reset,diff,log}`, `gh pr {create,view,list,merge,close}`, `python3 -m py_compile`/`unittest`, `Write`/`Edit` sur `~/radar-work/**`.

### Points 53-58

53. **`radar_ops` — page jobs remplacée par un diagramme de process (24/09/2026)** : demande utilisateur, les deux tableaux HTML (statuts + file) ne montraient pas la mécanique de la boucle. `radar_ops/templates/jobs.html` réécrit intégralement : diagramme SVG d'un pipeline strictement sériel — 2 voies d'entrée convergentes (⚡ interactif priority=1, 🐢 fond priority=0, 3 jetons visibles par voie + badge de débordement `+N`), 1 nœud worker central (jauge circulaire %, ETA, débit/min, uid), 2 sorties (succès vert / erreur rouge, 3 jetons récents chacun). Payload JSON backend inchangé (`_jobs_payload()`, `/ops/stream/jobs`) — même format, consommé différemment côté client : réconciliation SVG par clé `uid|name` (créer/déplacer/supprimer les groupes existants) plutôt que régénération du DOM à chaque tick SSE de 2s, pour permettre des transitions CSS fluides au lieu d'un clignotement.
    - Conception ET premier jet de code délégués à Gemini (RÈGLE N°1, cascade `heavy`) avant toute écriture par Claude — brief de conception (comparaison anneau vs pipeline horizontal, gestion du débordement, SVG vs Canvas) puis génération du fichier complet. Revu et corrigé : texte d'intro tronqué en `...` littéral dans le premier jet restauré au texte complet, tooltip natif `<title>` ajouté par jeton (uid, attente, message/erreur — perdu dans le jet initial), fonction `esc()` inutilisée supprimée, CSS des couleurs de bordure déplacée de JS inline vers des classes (`tok-p1`/`tok-p0`/`tok-ok`/`tok-bad`), repli `overflow-x:auto` ajouté pour mobile.
    - Vérifié hors-ligne (session sans `fastapi` ni accès VPS, cf. pt 48/51) : rendu Jinja2 direct sans dépendre de `TestClient`/`radar_ops.app` (`jinja2.Environment(loader=FileSystemLoader(...))`, disponible nativement dans cette session), syntaxe JS extraite validée par `node --check`, aperçu Artifact autonome (mêmes SVG/CSS/JS, données simulées rejouant le cycle file→worker→sortie) validé visuellement par l'utilisateur avant intégration finale. 6 tests neufs (`tests/test_ops_jobs_template.py` : rendu à vide, IDs attendus par le JS présents, échappement anti-injection d'un message d'erreur malveillant via `tojson`, régression sur les anciens `<tbody id="statuses/queue">`, unicode/caractères spéciaux). Suite complète 318 tests, mêmes 9 erreurs `fastapi` + 4 échecs clé `.env` locale déjà documentés (pt 48/51), zéro régression.
    - Non vérifié en conditions réelles (à faire après déploiement, cf. TODO) : flux SSE live sous charge réelle, lisibilité avec plusieurs utilisateurs simultanés, débordement effectif d'une voie à plus de 3 jobs.

54. **Accès mobile et interfaces — documentation (25/09/2026, Phase 5)** : `docs/acces-mobile-et-interfaces.md` écrit à partir de relevés faits sur le VPS, jamais de valeurs supposées. Principe rappelé : les deux interfaces (VSCode sur le PC, client SSH sur l'iPhone) s'attachent à **la même session `tmux`** — jamais deux sessions Claude Code concurrentes sur le même dépôt (ce serait exactement le scénario « écrire aux deux modèles » que RÈGLE N°1 interdit). Faits mesurés : hôte `vps-4294f993`, utilisateur `ubuntu`, nœud Tailscale `radar-vps` = `100.94.157.91` (IP publique `57.128.180.93`), sessions `tmux` `claude` (travail) et `radar`, `radar-ops` en écoute sur `100.94.157.91:8610` **et** loopback seulement (jamais sur l'IP publique), `sshd` sur `0.0.0.0:22` avec `PasswordAuthentication no` (clé uniquement, clés `radar-deploy` / `github-actions-radar-deploy`), `ufw` actif. Le document isole en § 7 ce qui **n'a pas pu être vérifié sans `sudo`** (`/etc/ufw/user.rules` et `user6.rules` en 0600 root, `sshd_config` illisible, iPhone jamais branché) — consigné dans le TODO plutôt que présenté comme acquis. Ajouté à `docs/context-manifest.json` pour que le digest de SessionStart le porte.

55. **Gate de lecture native (`Read`/`Grep`) — RÈGLE N°1 étendue à la lecture (25/09/2026)** : constat chiffré sur cette session — 89 appels API Claude en une session, 98 % des jetons facturés (`cache_read_input_tokens`) étaient la MÊME fenêtre de contexte refacturée à chaque aller-retour d'outil, pas du travail frais (travail frais réel ≈ 242k jetons, même ordre que les 203k du courtier ce jour-là). Décision utilisateur : la lecture (documents ET code exploratoire) est elle-même une tâche à faible valeur ajoutée à sous-traiter, pas seulement l'écriture — ça révise le carve-out du haut de ce fichier.
    - **`scripts/hooks/delegation_gate.py`** étend `required_modes()` : `Read` sur un fichier de plus de `RADAR_READ_GATE_MIN_LINES` lignes (défaut 80) exige un reçu récent en mode `context`, SAUF lecture chirurgicale (`limit` ≤ `RADAR_READ_GATE_SURGICAL_LINES`, défaut 30, ni 0 ni négatif — ça ne borne rien) — c'est la relecture ciblée d'un point signalé `uncertain` par un résumé déjà obtenu, ou l'extrait nécessaire juste avant une `Edit`. `Grep` est gaté SANS seuil de taille (mode `context` ou `search`) : contrairement à `Read`, il peut reconstruire un gros fichier en plusieurs petits appels qu'un seuil par appel ne verrait jamais — argument confirmé par 2 des 3 avis de modèles consultés ci-dessous (« Grep-as-Read »). Chemin relatif résolu contre `CLAUDE_PROJECT_DIR` avant de compter les lignes (piège cwd `~/radar` vs `~/radar-work`, pt 50).
    - **Délégation appliquée à sa propre conception** : premier jet des deux fonctions (`compte_lignes_seuil`, la branche `Read` de `required_modes`) délégué en mode `code` avant écriture par Claude. Revue croisée par 3 modèles gratuits distincts (`gemini-3.5-flash-lite` via mode `context`, `nvidia/nemotron-3-super-120b-a12b:free` et `gemini-3.1-flash-lite` via mode `general`, prompt identique) : 2 sur 3 ont détecté que `limit<=surgical` laissait passer `limit=0`/négatif sans borner la lecture (corrigé : `0 < limit <= surgical`), et 2 sur 3 ont recommandé de gater `Grep` malgré la limite du mode `search` (correspondance de sous-chaîne, une ligne par fichier, pas de regex/multi-match) — argument retenu : un reçu récent satisfait le gate même si `search` ne remplace pas vraiment le `Grep` refusé, la règle n'exige qu'une tentative de délégation, pas un remplacement fonctionnel exact. **Limite constatée en pratique** : le mode `context` cache son résultat par (chemin absolu, empreinte du texte envoyé) SANS tenir compte du modèle ni de la question posée — poser la même question à 3 modèles via `context` renvoie 2 fois le même résultat en cache. Pour une vraie pluralité d'avis sur un contenu déjà digéré une fois, utiliser `--mode general`/`reasoning` (pas de cache, prompt libre) plutôt que `context` (schéma JSON fixe, pensé pour la pré-digestion, pas la question ouverte).
    - Tests : `RequiredModesLectureTests` (13 cas neufs) dans `tests/test_gemini_gate.py`, alias `scripts/hooks/gemini_gate.py` mis à jour (nouvelles fonctions/constantes réexportées). Suite complète : mêmes 9 erreurs `fastapi` + 4 échecs `.env` locale déjà documentés (pt 48/51), zéro régression.
    - **Bloqué en attente d'action utilisateur** : le matcher `PreToolUse` de `.claude/settings.json` doit passer de `"Bash|Write|Edit"` à `"Bash|Write|Edit|Read"` pour que la nouvelle branche `Read` prenne effet (`Grep` est déjà couvert nulle part non plus — aucun matcher existant ne le liste, à ajouter aussi). Le classifieur de sécurité du harnais a refusé la modification directe de ce fichier par Claude (cohérent avec pt 45 : la config de hooks n'est pas une action que Claude s'autorise seule) — modification à faire par l'utilisateur, ou à valider explicitement tool par tool.

56. **Contournement du gate par `git diff` brut + mode `search` trop littéral (26/09)** — récit détaillé → `claude_archive.md` § « Incidents et correctifs — 23/09 → 26/09 ». En bref : `git diff` non relayé désormais gaté ; repli tokenisé dans `scripts/ai/locate.py`.

57. **`radar_ops` — cartographie des requêtes (`/delegation/workflows`, 27/09/2026)** : sous-onglet de Délégation. Sankey agrégé en tokens (requêtes → Claude/Courtier → cache relu, contexte écrit, sortie, modèles, tentatives en échec ; case « masquer le cache relu » pour comparer le travail frais au délégué) + liste des requêtes ; clic → diagramme de séquence en couloirs (Toi / Claude / Outils / Courtier / un couloir par modèle), chaque rangée cliquable → entrée, sortie, métadonnées. Source : les **transcripts Claude Code** (`~/.claude/projects/*/*.jsonl`, seuls à garder texte + usage par appel API), recoupés avec `gemini-receipts.jsonl` et `ai/ai_usage.jsonl` par fenêtre de temps ET mode (fenêtre élargie à 30 min pour un appel lancé en arrière-plan). `scripts/workflow_ingest.py` (côté hôte : le conteneur ne voit pas `~/.claude`) écrit `<ops>/workflows/<session>.json` ; relancé en processus détaché par le hook `Stop` (`telemetry.py`, `refresh_workflow`) pour la session courante, en CLI pour un rattrapage (`--since`, défaut 26/09 22h Paris). Textes masqués par `scripts/ai/redact.py`, sorties d'outils locaux tronquées à 4000 car., le reste à 60000. **Pièges** : un appel API est éclaté en une ligne JSONL par bloc répétant le même `usage` (compter par `requestId`) ; les `tool_result` sont dans des lignes `user` ; une `<task-notification>` prolonge la requête en cours ; le prompt exact envoyé à un modèle délégué n'est pas conservé par le courtier (seule la commande l'est).

58. **Correctifs courtier + part déléguée + workflow par requête (27/09/2026)** : analyse des reçus du 23 au 27/09 — l'essentiel des « erreurs » du 23-24/09 sont des reçus de banc (prompt de 1 caractère), pas de vraies délégations. Vraies causes corrigées : (a) **worktree sans `.env`** (`.worktrees/<branche>`) → toutes les clés absentes → « aucun candidat utilisable » : `catalogue.dotenv_paths()` replie sur le `.env` du dépôt principal (lu par `providers` ET `ai_query`) ; (b) `--model fournisseur:modèle` (le format qu'affiche `--list-candidates`) refusé → accepté, et `--list-candidates` respecte désormais `--model` ; (c) le reçu « aucun candidat » porte les 3 motifs d'écartement les plus fréquents ; (d) **santé par contrat** (`health.record_contract_failure`) : un modèle qui rend du JSON hors contrat ou du code qui ne compile pas dans un mode recule en fin de cascade pour ce mode pendant 24 h (nemotron en `context` : 80 à 490 s perdus par tentative) — un rang, pas un veto ; `record_success` (HTTP) ne l'efface pas. Part déléguée : `/delegation` et `/delegation/workflows` ne comptent plus au numérateur que les délégations abouties (jetons d'échec affichés à part, « perdus en échec »), et `/delegation` exclut aussi le cache relu côté Claude (même convention que la cartographie). Ré-ingestion nécessaire (`scripts/workflow_ingest.py`) pour que les anciennes requêtes séparent `failed_prompt`/`failed_output`. Workflow : skill `workflow` + agent `executant` (cf. en-tête). Mesures qui le motivent : contexte de départ ~62k jetons (dont ~16k pour ce fichier), 200-230k en session longue ; un sous-agent frais démarre à ~40k, un `fork` à ~180k.

### TODO avant compaction

## TODO — vérifications VPS en attente

**RECOS RADAR** :

- Filtre albums possédés : relancer `fetch_collection` ET `ingest_bandcamp` puis `scan_recos force=1` (exclusion Bandcamp ajoutée pt 41, jamais vérifiée en conditions réelles).


**Labels & scoring** :


**Recherche vendeur** (pt 37) :


**Divers** :
- **Workflow par requête (pt 58)** : sur les 5 prochaines requêtes, comparer dans `/delegation/workflows` le cache relu par requête (repère : 2,9 M en moyenne, 137k par appel) et le nombre d'appels du fil principal. Si le gain reste faible, prochain levier : alléger ce fichier (~16k jetons relus à chaque appel, fil principal ET sous-agents) en archivant les pts 43-57.
- **Gate `git diff` + recherche exhaustive (pt 56)** : `Bash` était déjà dans le matcher `PreToolUse` (pt 55) — le nouveau blocage sur `git diff` brut s'applique donc dès que le code est déployé, sans action utilisateur supplémentaire sur `.claude/settings.json`. À vérifier en conditions réelles : qu'un `git diff -- fichier` sans pipe déclenche bien le blocage avec le bon message, et que le repli `search` reste utile (pas trop bruyant) sur des requêtes réelles à l'échelle du dépôt complet, pas seulement `radar_web`.
- **Gate de lecture native `Read`/`Grep` (pt 55) — matcher activé le 26/09/2026** : `.claude/settings.json` porte désormais `Read|Grep` dans le matcher `PreToolUse` (fait par l'utilisateur). Vérifié en conditions réelles dans cette session : un `Read` sans `limit` sur un fichier >80 lignes est bien bloqué avec le message attendu, une délégation `--mode context` débloque, et une relecture chirurgicale (`limit=30`) passe ensuite sans reblocage. Reste à mesurer sur la durée : fréquence de blocage sur une session de code normale, et si le volume `cache_read_input_tokens` par session baisse effectivement (repère pt 55 : ~14,8M sur 89 appels avant le gate).
- **Accès mobile & ACL Tailscale (pt 54, Phase 5)** : sur le VPS, `sudo ufw status numbered` — confirmer que `22/tcp` n'est joignable que depuis `tailscale0` (les règles `ufw` sont root-only, non lisibles en session : cf. `docs/acces-mobile-et-interfaces.md` § 7). Puis valider depuis l'iPhone la chaîne Tailscale + client SSH + `tmux attach -t claude`, dont le comportement de redimensionnement (`tmux set -g window-size largest` si le texte du PC se tronque) et la lisibilité de `http://100.94.157.91:8610/delegation` en portrait.
- **Diagramme jobs `radar_ops` (pt 53)** : après déploiement, vérifier en conditions réelles le flux SSE sous charge (plusieurs utilisateurs simultanés), le débordement d'une voie à plus de 3 jobs (badge `+N`), et la lisibilité sur mobile/écran étroit.
- **Retry avec backoff Gemini (pt 51)** : observer en usage réel un vrai 429/503 pendant une cascade et confirmer que le retry (4s puis 8s) le résorbe avant tout fallback — pas provoqué en session, seul le chemin sans erreur a été vérifié. Si les 429 persistent malgré le retry, envisager un throttle explicite ~3 req/s (RPM=5 côté clé) plutôt qu'un simple retry réactif.
- **`radar_ops` exposé publiquement (27/09/2026)** : après création de l'enregistrement DNS `ops` → `57.128.180.93` et déploiement, vérifier que `https://ops.hubclaudel.fr/` sert la page jobs (certificat Let's Encrypt émis), qu'une requête non authentifiée est renvoyée vers `/login`, et que le flux SSE de la page jobs se met à jour en direct à travers Caddy.
- `radar_ops` (pt 43) : après déploiement, vérifier que le service `radar-ops` répond sur le port 8610 depuis Tailscale, que le débit/ETA des jobs se construit bien en conditions réelles (job long type `scorestore_releases`), que la page Bases ne rame pas malgré le dump Discogs (le comptage de lignes est coupé au-delà de 200 Mo), et que la page Scoring passe de « non estampillé » à « à jour » après un premier `scorestore_releases` post-déploiement.
- **Télémétrie (pt 43, régression corrigée au pt 50)** : après déploiement, confirmer que `~/radar-work/.claude/telemetry.jsonl` et `~/radar/.claude/telemetry.jsonl` ne grossissent plus (l'override `RADAR_TELEMETRY_DIR` doit tout rediriger vers `~/radar/data/ops/telemetry.jsonl`, quel que soit le répertoire de la session) ; confirmer que `/delegation` affiche des sessions récentes des deux répertoires ; envisager de nettoyer les deux fichiers `.claude/telemetry.jsonl` orphelins (activité perdue entre le 24/09 07h24 et le déploiement du correctif — pas rattrapable, pas grave en soi) ; mesurer le surcoût du hook `PostToolUse` (un sous-processus Python par appel d'outil) et le retirer de cet événement s'il se voit.
- `/wantlist` et `/patte` (page d'accueil) : confirmer le temps de chargement à froid après déploiement (correctifs de perf déjà déployés — plus de calcul lourd de graphe hors de l'onglet qui en a besoin).
- `vps-ops` : confirmer que la session VPS applique bien le périmètre élargi du 23/09 (pt 45) — drapeaux `.env` activés sans accord préalable mais documentés, merge toujours conditionné à une instruction explicite par PR, jamais de secret dans un rapport.
- Process d'amélioration des scripts (pt 39) : prochaine étape `/script-observe ytcache` côté VPS puis `/script-loop ytcache`. Token GitHub (`~/.config/gh/radar_pat`) + clé de déploiement en écriture (`gh_deploy_vps_radar_work`) provisionnés le 23/09 — reste à vérifier en conditions réelles l'ouverture d'une PR de boucle sur un vrai correctif.
- PR #184 (pt 40, mergée le 20/09) : token GitHub + `gh` CLI provisionnés sur le VPS le 23/09 (pt 45). Reste à vérifier au premier vrai `/script-loop` que la session ouvre bien la PR et respecte le garde-fou par instance avant de merger.
- Chantier multi-utilisateur : bloqué à l'étape OAuth Discogs/Spotify (5b) — inchangé.
- TODO code résiduel (pt 22, ancien) : multi-selects genre/style, `<label for>` non reliés, libellés FR Réglages, liens `/disco` reco/recherche, UI artistes "approx", suppression track/DJ dans Mes sets.

Détail complet (numéros de PR, mesures VPS précises, diagnostics session par session) → `claude_archive.md` : section « Archive détaillée — 11/09 → 19/09 » pour l'historique antérieur, et section « Incidents et correctifs — 23/09 → 26/09 » pour les récits d'incident de la période récente (pts 46-52 et 56).


### Pièges retirés (corrigés dans le code ou doublons)

8. **Piège Streamlit** : archivé, mort — jamais y reporter d'évolutions.
9. **Piège Bandcamp** (`bcsearch_public_api`) : endpoint non documenté, repli URL de recherche suffit.
11. **Piège dump Discogs** : `data.discogs.com` sert via `?download=` (query), pas chemin direct — corrigé `dump_url()`/`checksum_url()`.
13. **Piège recherche YouTube** (`ytcache.search_video`) : 1er résultat pouvait être hors-sujet. Corrigé : 15 résultats scorés par métadonnées (requête enrichie du label, bonus chaîne=artiste/label).
21. **Vendeurs — en pause depuis pt 33** : scan vendeurs manuel + `RADAR_SELLER_SCAN=1` hebdo sans objet tant que `features.SELLERS_ENABLED` vaut `False`.
23. **Piège `step` HTML5 sans `min`** (`settings.html`) : poids par défaut non multiple du pas → champ invalide → formulaire bloqué en silence. Corrigé : `step="any"` sur les groupes de poids.
25. **Piège lien artiste↔label sans croisement de style** (`Ctx.artist_label_signal`) : partager un label suivi boostait un artiste même hors-style. Corrigé : le signal ne compte que les crédits dont un style est dans `wmap`.
30. **Piège artistes placeholder RECOS** ("Various"/"Various Artists"/"Unknown Artist"/"No Artist", suffixes de désambiguïsation Discogs) : `_is_placeholder_artist()` (set dédié, volontairement distinct d'`ARTIST_STOPWORDS` pour ne pas écarter à tort un artiste réel homonyme, ex. "Release (22)") traite ces valeurs comme absence de crédit — piste écartée des candidats RECOS plutôt que cherchée au titre seul. `_is_continuous_mix()` filtre aussi les megamix à l'écriture/sélection (jamais dans `scoring.real_tracks()`, partagée avec la page sortie).
