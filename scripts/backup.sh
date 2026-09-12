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

# tout /data SAUF les backups et le référentiel Discogs (shared/, reconstructible
# depuis le dump mensuel Discogs, cf. discogs_dump.py — pas une donnée utilisateur,
# inutile de le réencrypter en entier chaque jour) -> tar -> chiffrement (flux,
# rien en clair sur disque)
#
# Piège : tar archive avec des chemins RELATIFS à -C ("data/...") — un --exclude
# en chemin absolu ne matche jamais rien. Vécu le 12/09 : backups/ et
# shared/discogs_dump_raw/ (~12G) inclus dans l'archive -> 13G corrompu
# (tar lit le .tgz.enc en cours d'écriture -> "file changed as we read it").
base="$(basename "$DATA_DIR")"
sudo tar czf - \
  --exclude="$base/backups" \
  --exclude="$base/shared/discogs_dump.sqlite3*" \
  --exclude="$base/shared/discogs_dump_raw" \
  -C "$(dirname "$DATA_DIR")" "$base" \
  | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 -pass env:BACKUP_PASS -out "$out"
chown "$(id -u):$(id -g)" "$out" 2>/dev/null || true
echo "$(date -u +%FT%TZ)  ok  $(basename "$out")  $(du -h "$out" | cut -f1)"

# repères de taille par sous-dossier (diagnostic dérive, sans passer par SSH)
du -sh "$DATA_DIR"/*/ 2>/dev/null | sort -rh

# rétention : ne garde que les KEEP plus récents
ls -1t "$OUT_DIR"/data-*.tgz.enc 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

# --- copie hors-VPS (optionnel) : décommente + configure un remote rclone ---
# rclone copy "$out" "monremote:radar-backups/" && echo "  copié hors-VPS"
