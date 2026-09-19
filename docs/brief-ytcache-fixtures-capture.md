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
depuis l'historique réel RECOS (`recos_playlist_history.json`).

**Tâche EXÉCUTÉE le 19/09 (40 cas capturés) — relire ceci avant d'en relancer
une, la prémisse de ce brief était fausse** : rejouer des SUCCÈS historiques ne
discrimine pas. Ces cas passent même avec le scoring de `_best_match`
entièrement court-circuité (mesuré : 25/25 du plancher passaient encore sous
sabordage), parce que YouTube renvoie déjà la bonne vidéo en 1re position pour
une requête « Artiste Titre » bien formée. Ils ne sont donc gardés que comme
PLANCHER (`kind: "floor"` — détectent une casse franche du type « plus aucune
vidéo trouvée »), jamais comme mesure d'amélioration. Deux autres défauts
relevés à l'intégration : 15 des 40 cas portaient un artiste placeholder
(« Various ») d'avant les pts 47/49, écartés depuis par le script de capture ;
et l'identifiant vidéo historique n'est PAS une vérité terrain (une piste a
souvent plusieurs uploads valables — le code actuel préfère désormais la chaîne
officielle « Artiste - Topic », ce qui n'est pas une régression).
**Conclusion : une nouvelle capture depuis l'historique n'apporterait presque
rien.** Ce qui manque, ce sont des cas ADVERSES — cf. « Suite » en fin de
fichier.

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

## Suite — capturer des cas ADVERSES (le vrai manque)

Fait le 19/09 : 40 cas capturés côté VPS, 25 intégrés au dépôt via PR côté
cloud. Le banc mesure donc 3 cas discriminants + 25 de plancher.

**Ce qu'il faut maintenant, et qui n'est PAS dans ce brief** : des cas où le
matching ÉCHOUE ou se trompe — les seuls capables de dire si un changement de
scoring améliore ou dégrade. Où les trouver, dans l'ordre d'intérêt :

1. **Journal de `publish_recos`** : les candidats rejetés avec « meilleur score
   X < 0.5 » alors que la piste est trouvable à la main. Ce sont des faux
   négatifs — l'inverse exact de ce que le plancher couvre.
2. **Retours 👎** sur des pistes de la playlist : faux positifs (mauvaise vidéo
   prise pour la bonne piste), risque accru depuis le pt 67 qui a assoupli le
   seuil global.
3. **Compilations et remix/dub** : les pièges déjà connus (pts 27/67), où
   plusieurs versions coexistent dans les résultats.

Un cas adverse se capture comme les autres (mêmes `search.json`/`videos.json`)
mais se déclare `kind: "adversarial"` avec l'attendu VÉRIFIÉ à la main (identifiant
exact via `expect: "match"`, ou `expect: "no_match"` si aucune vidéo ne convient).
Un tel cas demande donc une vérification humaine — il ne peut pas être généré
automatiquement depuis l'historique, contrairement au plancher.
