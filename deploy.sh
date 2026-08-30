#!/usr/bin/env bash
# Mise à jour du serveur : à lancer sur la machine hôte.
set -euo pipefail
cd "$(dirname "$0")"

if [ -d .git ]; then git pull --ff-only; else echo "(pas de dépôt git — code supposé déjà à jour)"; fi
docker compose build
docker compose up -d
docker compose ps
echo "--- logs (Ctrl+C pour quitter) ---"
docker compose logs -f --tail=30
