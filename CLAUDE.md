# Radar — instructions projet

Outil perso de crate-digging vinyle basé sur Discogs : ingère ton écoute (YouTube,
Spotify, Bandcamp, DJ sets), profile tes labels/artistes, et note des sorties Discogs
selon ton goût. Voir `docs/architecture.md` pour la cible multi-utilisateur (chantier en
cours).

**Reprise de contexte : lire `docs/etat.md`** — où en est le chantier, ce qui reste à
faire (code + opérationnel), et les pièges déjà appris.

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
`Range` si interrompu), vérifie la somme de contrôle (`CHECKSUM.txt`, best-effort — absence
n'empêche pas l'import), parse en flux (`ET.iterparse`, `root.clear()` **et** `elem.clear()`
après chaque `<release>` — memory-leak sinon sur un fichier de cette taille), reconstruit
l'index en entier (pas de delta, un dump mensuel est toujours un instantané complet) dans
`discogs_dump.sqlite3.new`, jamais dans le fichier servi par l'appli : les index ne sont créés
qu'une fois la table remplie (`ANALYZE` ensuite), puis bascule atomique (`os.replace`) à la
toute fin. L'appli lit l'ancienne base valide jusqu'à la dernière seconde — pas de fenêtre où
`available()` mentirait pendant les ~1h45 que dure un import. `journal_mode=WAL` posé à la
création (persistant dans le fichier). Bouton manuel dans Réglages ; veille automatique
mensuelle via `RADAR_DISCOGS_DUMP_SYNC=1` (à poser sur le `.env` du service `radar-worker` sur
le VPS, comme `RADAR_SELLER_SCAN=1`).

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
  remplie pendant le parsing des sorties (`<artists>` + `<extraartists>` filtrés). Rend
  possible un vrai graphe de co-crédits par jointure SQL (`JOIN release_artists ON
  release_id` en s'excluant soi-même) au lieu d'un appel API par graine — **pas encore
  branché** : `job_build_graph`/`canonicalize` continuent d'interroger l'API pour l'instant.
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
en mode `global` (graphe producteur complet, alimente le ranking labels/artistes de Mon
univers, mensuel). Ces trois tâches n'ont plus de bouton dans l'interface — un utilisateur
n'a plus rien à cliquer. Cadence mémorisée dans `jobs/auto_maintenance.json` (survit aux redémarrages
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

## Jobs

`crate_jobs.py` — tâches longues (profilage, résolution, graphe, ingestion, veille),
lancées en sous-processus par l'appli. Statuts dans `/data/jobs/*.status.json`. Ne PAS
rebuild le conteneur pendant qu'un job tourne (ça le tue). Jobs reprenables via
`*.state.json`.
