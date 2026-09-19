# Brief VPS — capture de fixtures réelles pour le banc `ytcache`

Destiné à une session Claude Code qui tourne **sur le VPS** (contrat
`.claude/skills/vps-ops/SKILL.md` — le lire d'abord si pas déjà en mémoire).
Ce brief ne demande **aucune exception** à ce contrat : tout ce qui suit reste
en lecture seule sur `~/radar`, aucune écriture ni commit dans ce dépôt.

## Contexte

Méthode d'entraînement/optimisation de `ytcache.py` (matching YouTube des
pistes RECOS), décidée avec l'utilisateur en session cloud : un banc de
mesure hors-ligne (`scripts/bench_ytcache.py`) rejoue le scoring contre des
réponses YouTube capturées une fois, sans appel réseau ni quota à chaque
rejeu. 3 cas construits à la main existent déjà (`tests/fixtures/ytcache/`).
Objectif de cette tâche : **étoffer ce jeu de cas avec de vraies captures**,
depuis l'historique réel RECOS (`recos_playlist_history.json`) — des pistes
déjà validées un jour, donc un corpus de non-régression.

**La suite (faire évoluer `ytcache.py` lui-même à partir de ce banc) reste
côté session cloud, pas ici** — cf. « Ce que tu NE fais PAS » ci-dessous.

## Prérequis

- `~/radar` à jour (`git pull`, lecture seule — normal).
- Une clé YouTube utilisable : `YOUTUBE_API_KEY` dans `.env`, ou un token
  personnel dans la config de l'utilisateur par défaut. Si aucune des deux
  n'est disponible, arrête-toi et dis-le — ne demande pas à l'utilisateur de
  te donner une clé en clair dans la conversation.
- Vérifier qu'aucun job lourd n'est en cours (`*.status.json`) avant de
  lancer — cette capture ne passe pas par la file de jobs, mais elle
  consomme le même quota Google que `scan_recos`/`publish_recos` en
  production : éviter de cumuler avec un run RECOS en cours.
- `~/radar-diag` cloné et à jour (dépôt séparé, celui de ton outillage/mémoire).

## Étapes

1. **Capturer** (depuis `~/radar`, en lecture — le script n'écrit que dans
   `--out`) :
   ```
   python scripts/capture_ytcache_fixtures.py --out ~/radar-diag/ytcache-fixtures --limit 15
   ```
   - `--out` doit pointer dans `~/radar-diag`, jamais dans `~/radar` — le
     script refuse de lui-même un `--out` à l'intérieur du dépôt applicatif,
     mais vérifie que la commande ci-dessus n'a pas été modifiée.
   - `--limit 15` : ~15×101 = 1515 unités de quota Google (sur 10 000/jour).
     Monte prudemment (`--limit 30` par exemple) si le quota du jour le
     permet, jamais au point de gêner RECOS en production le même jour.
   - Le script ignore automatiquement les cas déjà présents (dépôt ou
     `--out`) — relancer plusieurs fois avec des `--limit` croissants est
     sans risque de doublon.

2. **Valider** que les fixtures capturées rejouent correctement (aucun appel
   réseau ici, juste un contrôle de cohérence) :
   ```
   python scripts/bench_ytcache.py --fixtures-dir ~/radar-diag/ytcache-fixtures -v
   ```
   Un score très bas serait suspect (ex. clé/quota mal configuré côté
   capture) — dans ce cas, documente-le dans le rapport plutôt que de
   recapturer en boucle.

3. **Committer/pousser dans `radar-diag`** (jamais dans `radar`) :
   ```
   cd ~/radar-diag
   git add ytcache-fixtures/
   git commit -m "Capture N fixtures ytcache depuis recos_playlist_history.json"
   git push
   ```
   Aucun secret dans ces fichiers (réponses API publiques : titres, chaînes,
   ids de vidéos) — mais reste dans les interdits habituels du contrat pour
   le reste (pas de token, pas de contenu `.env` ailleurs dans le commit).

4. **Journaliser** dans `~/radar-diag/memory/journal/<AAAA-MM>.md` (convention
   habituelle) : nombre de cas capturés, quota consommé, résultat du bench,
   lien/sha du commit `radar-diag`.

5. **Rapport à l'utilisateur**, format habituel du contrat vps-ops (verdict,
   actions effectuées, ce qui reste à faire) — dire explicitement : *« Prêt à
   être récupéré côté session cloud pour intégration dans `tests/fixtures/
   ytcache/` du dépôt `radar` (PR) »*.

## Ce que tu NE fais PAS (rappel du contrat)

- **Jamais écrire dans `~/radar`** — ni `tests/fixtures/ytcache/`, ni
  `cases.json` du dépôt, ni aucun autre fichier. `--out` pointe toujours
  ailleurs.
- **Jamais commit/push sur le dépôt `radar`** — seulement sur `radar-diag`.
- **Ne pas modifier `ytcache.py`** ni tenter d'améliorer le scoring
  toi-même : cette étape ne fait QUE capturer des fixtures. L'itération sur
  le code (mesurer, proposer un correctif, rejouer le banc, committer) reste
  une tâche de la session cloud, sur une branche dédiée, via PR — jamais
  ici, pour éviter que deux sessions modifient le même code en parallèle
  (raison d'être de la séparation stricte du contrat vps-ops).
- **Ne pas afficher la clé YouTube** utilisée, même partiellement, dans le
  rapport ou le journal.

## Suite (hors périmètre de ce brief, pour information)

Une fois les fixtures poussées sur `radar-diag`, l'utilisateur (ou un
prochain tour de la session cloud) récupère ce dossier, l'intègre à
`tests/fixtures/ytcache/` du dépôt `radar` et fusionne `cases.json`, puis
lance une boucle d'itération sur `ytcache.py` mesurée par ce banc — sur une
branche dédiée, jamais directement sur `main`.
