# Backups Radar

Sauvegarde chiffrée quotidienne de `/data` (comptes, données utilisateur, caches). Voir aussi `docs/architecture.md` étape 6.

## En place sur le VPS

- `scripts/backup.sh` : `tar` de `/data` (hors `data/backups/` et `data/shared/discogs_dump.sqlite3`, référentiel Discogs reconstructible depuis le dump mensuel, cf. `discogs_dump.py` — pas une donnée utilisateur) → chiffrement **AES-256** (openssl, PBKDF2 200k) en flux → `/data/backups/data-<ts>.tgz.enc`.
- Rétention : 14 fichiers (les plus récents).
- Le log (`backup.log`) inclut désormais un relevé de taille par sous-dossier de `/data`, pour repérer une dérive sans SSH.
- Cron hôte : tous les jours à 04:00 UTC. Log : `data/backups/backup.log`.
- Passphrase : `/home/ubuntu/radar/secrets/backup.pass` (mode 600, hors git). **⚠️ Copie à garder dans un gestionnaire de mots de passe** — sans elle, backups irrécupérables.

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

Backups actuellement **sur le même disque** que les données → perte du VPS = tout perdu. Décommenter la ligne `rclone` de `backup.sh` et configurer un remote (stockage objet, ou `gh release upload` vers repo privé).