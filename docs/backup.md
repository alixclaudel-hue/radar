# Backups Radar

Sauvegarde chiffrée quotidienne de `/data` (comptes, données de chaque
utilisateur, caches). Voir aussi `docs/architecture.md` étape 6.

## En place sur le VPS

- `scripts/backup.sh` : `tar` de `/data` (hors `data/backups/`) → chiffrement
  **AES-256** (openssl, PBKDF2 200k) en flux → `/data/backups/data-<ts>.tgz.enc`.
- Rétention : 14 fichiers (les plus récents).
- Cron hôte : tous les jours à 04:00 UTC. Log : `data/backups/backup.log`.
- Passphrase : `/home/ubuntu/radar/secrets/backup.pass` (mode 600, hors git).
  **⚠️ En garder une copie dans un gestionnaire de mots de passe** — sans elle,
  les backups sont irrécupérables.

## Restaurer

```bash
cd ~/radar
export BACKUP_PASS='...'          # la passphrase
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BACKUP_PASS \
  -in data/backups/data-AAAA...tgz.enc | tar xz -C ~/radar
sudo docker compose up -d
```

(`tar xz -C ~/radar` réécrit `~/radar/data/`.)

## À faire un jour : copie hors-VPS

Aujourd'hui les backups sont **sur le même disque** que les données → une perte
du VPS = tout perdu. Décommenter la ligne `rclone` de `backup.sh` et configurer
un remote (stockage objet, ou `gh release upload` vers un repo privé).
