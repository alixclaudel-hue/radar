#!/usr/bin/env bash
# One-shot : lance un Chromium visible accessible en VNC pour se connecter à
# Discogs une bonne fois. À exécuter sur le VPS. VNC exposé UNIQUEMENT sur
# l'IP Tailscale (réseau privé), sans mot de passe, éphémère.
#
#   bash radar_web/tools/vnc_login.sh
#
# Puis, depuis le Mac : Finder → Aller → Se connecter au serveur →
#   vnc://100.94.157.91:5900
# Connexion Discogs, puis fermer la fenêtre / Ctrl-C ici.
set -e

TS_IP="${TS_IP:-100.94.157.91}"
DATA_DIR="${DATA_DIR:-$HOME/radar/data}"
IMAGE="mcr.microsoft.com/playwright/python:v1.48.0-jammy"

echo ">>> VNC sur ${TS_IP}:5900 (Tailscale uniquement, sans mot de passe)"
sudo docker run --rm -it \
  -v "${DATA_DIR}:/data" \
  -v "$(cd "$(dirname "$0")" && pwd)/discogs_login.py:/login.py:ro" \
  -p "${TS_IP}:5900:5900" \
  -e DISCOGS_PROFILE=/data/discogs_profile \
  "${IMAGE}" \
  bash -lc '
    set -e
    apt-get update -qq && apt-get install -y -qq x11vnc xvfb fluxbox >/dev/null
    export DISPLAY=:0
    Xvfb :0 -screen 0 1280x900x24 >/dev/null 2>&1 &
    sleep 1
    fluxbox >/dev/null 2>&1 &
    x11vnc -forever -nopw -rfbport 5900 -shared -quiet >/dev/null 2>&1 &
    sleep 1
    python /login.py
  '
echo ">>> Terminé. Profil dans ${DATA_DIR}/discogs_profile"
