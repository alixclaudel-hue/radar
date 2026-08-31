#!/usr/bin/env bash
# Bootstrap serveur (Ubuntu). À lancer DEPUIS le dossier du repo cloné :
#   cd ~/radar && bash bootstrap.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== Docker =="
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" || true
  echo ">> déconnecte/reconnecte-toi (ou 'newgrp docker') puis relance ce script."
  exit 0
fi

echo "== Tailscale =="
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
sudo tailscale status >/dev/null 2>&1 || {
  echo ">> lance :  sudo tailscale up    (ouvre l'URL affichée pour lier la machine)"
}

echo "== Pare-feu (SSH + Tailscale seulement) =="
if command -v ufw >/dev/null; then
  sudo ufw allow 22/tcp >/dev/null
  sudo ufw allow in on tailscale0 >/dev/null || true
  sudo ufw --force enable >/dev/null
fi

echo "== .env =="
[ -f .env ] || { cp .env.example .env; echo ">> édite .env (APP_PASSWORD + tokens) puis relance."; exit 0; }
grep -q 'APP_PASSWORD=.\+' .env && ! grep -q 'CHANGE' .env || {
  echo ">> mets un vrai APP_PASSWORD dans .env puis relance."; exit 0; }

echo "== Données =="
mkdir -p data/jobs
if [ ! -f data/crate_radar_config.json ]; then
  echo "   récupération du seed depuis la branche data-seed…"
  git fetch -q origin data-seed && git show origin/data-seed:seed/crate_radar_config.json >/dev/null 2>&1 && {
    for f in $(git ls-tree -r --name-only origin/data-seed -- seed | sed 's#^seed/##'); do
      mkdir -p "data/$(dirname "$f")"
      git show "origin/data-seed:seed/$f" > "data/$f"
    done
    echo "   seed installé ($(ls data/*.json | wc -l) fichiers). Les secrets viennent de .env."
  } || echo ">> pas de branche data-seed — copie tes JSON dans ./data/ à la main."
fi

echo "== Build + run (Streamlit sur :8501) =="
docker compose up -d --build
docker compose ps
echo
echo "OK. Accès :  http://$(tailscale ip -4 2>/dev/null | head -1):8501"
echo "Mises à jour ultérieures :  git pull && docker compose up -d --build"
