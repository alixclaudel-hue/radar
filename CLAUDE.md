# Radar — instructions projet

Outil perso de crate-digging vinyle basé sur Discogs : ingère ton écoute (YouTube,
Spotify, Bandcamp, DJ sets), profile tes labels/artistes, et note des sorties Discogs
selon ton goût. Voir `docs/architecture.md` pour la cible multi-utilisateur (chantier en
cours).

## Deux applis, une seule active

- **`radar_web/`** — FastAPI + HTMX, port 8600. **C'est l'interface active.**
- **`crate_radar.py`** — Streamlit, port 8501. **Gelée, référence historique.**
  Ne PAS la maintenir en parallèle : seuls la couche de données JSON et `crate_jobs.py`
  sont communs. (Retrait prévu, cf. `docs/architecture.md` étape 7.)

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

## Conventions

- **Avant chaque push** : `python3 -m py_compile` sur les fichiers touchés, puis lancer
  uvicorn en local (`CRATE_DATA_DIR` pointé sur un `data/` local) et `curl` les routes
  modifiées — vérifier 200/303, pas de traceback. C'est le filet principal tant que la CI
  n'est pas en place.
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
