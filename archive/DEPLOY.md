# Déploiement Crate Radar (serveur toujours allumé, accès iPhone)

Objectif : l'app tourne sur une petite machine Linux ; on la met à jour depuis le
Mac par `git push` ; l'iPhone y accède de n'importe où. Tout fonctionne
(profilage, graphe, DJ sets via Chromium). Les données vivent sur un volume
persistant, jamais dans git.

## 1. La machine

Recommandé : **VPS 2 vCPU / 4 Go / 40 Go** (Hetzner CX22 ≈ 4,5 €/mois, ou
Fly.io / Scaleway). Un Raspberry Pi 4/5 (4 Go+) marche aussi mais Chromium + les
jobs longs sont plus à l'aise sur x86.

Sur la machine : Ubuntu 22.04+, puis Docker :

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER" && newgrp docker
```

## 2. Le code

Mets `crate_radar.py`, `crate_jobs.py`, `Dockerfile`, `docker-compose.yml`,
`requirements.txt`, `.streamlit/`, `deploy.sh` dans un dépôt git privé
(GitHub/GitLab). Le `.gitignore` fourni exclut déjà **toutes** les données et
`.env`.

```bash
git clone git@github.com:<toi>/crate-radar.git
cd crate-radar
```

## 3. Les secrets

```bash
cp .env.example .env
nano .env      # APP_PASSWORD (obligatoire), DISCOGS_TOKEN, YOUTUBE_API_KEY, BANDCAMP_SUB_*
```

`APP_PASSWORD` : sans lui l'app est ouverte à tous et **affiche ton token
Discogs**. Il est requis dès qu'elle est en ligne.

## 4. Les données (depuis le Mac, une seule fois)

Sur le Mac, dans le dossier du projet :

```bash
mkdir -p data/jobs
cp crate_radar_config.json labels_resolved.json labels_profile.json \
   taste_corpus.json lookup_cache.json producer_graph.json artists_resolved.json \
   collection_cache.json search_history.json reco_feedback.json sellers_seen.json \
   seller_new.json scoring_profiles.json djset_seen.json data/ 2>/dev/null || true

rsync -avz data/ <user>@<serveur>:~/crate-radar/data/
```

(Le token peut rester dans `data/crate_radar_config.json` ; sinon vide-le et
laisse `.env` le fournir.)

## 5. Lancer

```bash
docker compose up -d --build
docker compose logs -f          # vérifier "You can now view your Streamlit app"
```

L'app écoute sur `:8501`.

## 6. HTTPS + accès

Deux options :

- **Tailscale** (privé, simple) : `curl -fsSL https://tailscale.com/install.sh | sh`
  puis `sudo tailscale up`. L'iPhone (app Tailscale, même compte) ouvre
  `http://<nom-machine>:8501`. Pas de certif à gérer.
- **Nom de domaine + Caddy** (public) : `caddy reverse-proxy --from radar.exemple.fr --to :8501`
  (HTTPS auto). Garde quand même `APP_PASSWORD`.

Sur l'iPhone : Safari → l'URL → *Partager → Sur l'écran d'accueil* → icône type app.

## 7. Mettre à jour

Sur le Mac (avec moi) : on édite, puis
```bash
git commit -am "..." && git push
```
Sur le serveur :
```bash
./deploy.sh          # git pull + rebuild + restart
```
Les données ne bougent pas (volume `./data`). Sauvegarde : `rsync` de `./data/`.

## 8. Notes

- Le worker de jobs (`crate_jobs.py`) est lancé en sous-processus par l'app et
  hérite de `CRATE_DATA_DIR=/data` — rien à configurer.
- Si l'image Playwright fournit un Python trop récent pour `streamlit==1.28.0`,
  monter la version dans `requirements.txt` (testé jusqu'à 1.39).
- Scan vendeurs / DJ sets : peuvent se faire bloquer depuis une IP datacenter
  (YouTube surtout). Tailscale + trafic sortant du VPS suffisent en général ;
  sinon, un VPS résidentiel/proxy.
