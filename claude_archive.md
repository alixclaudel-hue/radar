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
