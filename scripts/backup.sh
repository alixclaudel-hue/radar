#!/usr/bin/env bash
# Backup chiffré des données Radar (Option A, étape 6).
# Lancé par cron sur l'HÔTE du VPS (pas dans un conteneur). openssl suffit,
# rien à installer.
#
#   0 4 * * *  BACKUP_PASS="$(cat /home/ubuntu/radar/secrets/backup.pass)" \
#              /home/ubuntu/radar/scripts/backup.sh >> /home/ubuntu/radar/data/backups/backup.log 2>&1
#
# Restauration :
#   openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BACKUP_PASS \
#     -in data-AAAA....tgz.enc | tar xz -C ~/radar        # écrase /data
set -euo pipefail

DATA_DIR="${DATA_DIR:-/home/ubuntu/radar/data}"
OUT_DIR="$DATA_DIR/backups"
KEEP="${KEEP:-14}"
: "${BACKUP_PASS:?BACKUP_PASS manquante}"

mkdir -p "$OUT_DIR"
ts=$(date -u +%Y%m%dT%H%M%SZ)
out="$OUT_DIR/data-$ts.tgz.enc"

# tout /data SAUF les backups -> tar -> chiffrement (flux, rien en clair sur disque)
sudo tar czf - --exclude="$OUT_DIR" -C "$(dirname "$DATA_DIR")" "$(basename "$DATA_DIR")" \
  | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 -pass env:BACKUP_PASS -out "$out"
chown "$(id -u):$(id -g)" "$out" 2>/dev/null || true
echo "$(date -u +%FT%TZ)  ok  $(basename "$out")  $(du -h "$out" | cut -f1)"

# rétention : ne garde que les KEEP plus récents
ls -1t "$OUT_DIR"/data-*.tgz.enc 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

# --- copie hors-VPS (optionnel) : décommente + configure un remote rclone ---
# rclone copy "$out" "monremote:radar-backups/" && echo "  copié hors-VPS"
