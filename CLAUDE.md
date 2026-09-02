# Radar — instructions projet

Outil perso de crate-digging vinyle basé sur Discogs : ingère ton écoute (YouTube,
Spotify, Bandcamp, DJ sets), profile tes labels/artistes, et note des sorties Discogs
selon ton goût. Voir `docs/architecture.md` pour la cible multi-utilisateur (chantier en
cours).

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
