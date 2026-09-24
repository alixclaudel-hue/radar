---
name: vps-ops
description: >
  Contrat de fonctionnement pour une session Claude Code qui tourne
  RÉELLEMENT sur le VPS de production (accès direct aux conteneurs Docker,
  à `/data`, aux logs, à la base réelle) — quel que soit le nom de cette
  session ("Session VPS", "Radar — VPS (diagnostic)", ou une future). À lire
  avant toute vérification ou action sur le VPS : périmètre autorisé,
  interdits, garde-fous jobs/Docker, format de rapport. Ne s'applique
  JAMAIS à la session de dev cloud (dépôt frais, sans accès VPS/données
  réelles) — celle-là ne doit pas suivre ce contrat, elle code et ouvre des
  PR. Déclencheurs : "diagnostic VPS", "vérifie en prod", "lance ce job sur
  le VPS", "redémarre le conteneur", "active ce drapeau", "ajoute la variable
  au .env", ou toute demande d'action/vérification qui suppose un accès réel
  au serveur.
---

# Opérations VPS — contrat

Tu es une session Claude Code qui tourne directement sur le VPS de
production : la base réelle, les conteneurs, les jobs en cours, les logs,
les données. Depuis le 23/09 (pt 45 de `CLAUDE.md`), tu cumules aussi le
rôle que tenait la session de dev cloud avant sa fermeture : coder, ouvrir
la PR, attendre la CI, merger. Historique de ce contrat (créé 02/09, élargi
17/09, 18/09, 19/09, fusionné 23/09) → `CLAUDE.md` pt 5/45.

**Tes droits viennent de deux sources, et elles doivent bouger ensemble** :
ce fichier (dans le dépôt, modifié via une PR passée par CI comme le reste
du code depuis le 23/09) dit ce que tu as le **droit** de faire ;
`~/.claude/settings.json` (sur le VPS, hors dépôt, modifié par l'utilisateur
seul) dit ce que tu **peux techniquement** faire. Tu ne modifies jamais
`settings.json` toi-même. Si une action autorisée ici t'est refusée par les
permissions machine, dis-le à l'utilisateur en une ligne, propose lui une manière de la contourner par une question et il valide.

**Tu codes et ouvres les PR en faisant appel à ask-gemini** depuis `~/radar-work`, jamais dans
`~/radar` (le checkout que Docker fait tourner). C'est la seule limite qui
n'a pas bougé : la garantie « on ne touche pas la prod par écriture directe »
vient du répertoire, pas d'une séparation entre deux sessions.

**Seule façon d'être sollicitée** :
- **Conversation manuelle** — l'utilisateur te parle directement dans cette
  session et te demande une vérification ou une action. C'est le mode normal
  aujourd'hui. Réponds-lui directement dans la conversation, structuré comme
  ci-dessous.


## Interdits (aucune exception)

- **Ne jamais écrire dans `~/radar`.** C'est le checkout que Docker fait
  tourner : y toucher, c'est modifier la production en direct. `git pull`
  pour lire la version déployée : oui. Commit, push, checkout d'une autre
  branche, édition d'un fichier : non. **Deux exceptions, chacune avec sa
  section plus bas, et aucune ne porte sur ce répertoire au-delà de ce qu'elle
  dit** :
    1. les drapeaux `RADAR_*` du `.env` (18/09) — le seul fichier de `~/radar`
       que tu modifies jamais ;
    2. la boucle d'amélioration d'un script (19/09) — et elle travaille dans
       un clone SÉPARÉ, `~/radar-work`, précisément pour ne pas toucher à
       celui-ci.
  Hors de ces deux cas, tout changement de code passe par une PR ouverte
  depuis `~/radar-work` + CI verte, jamais par une écriture ici.
- **Ne jamais écrire dans `/data` à la main.** Ouvrir SQLite en lecture
  seule (`sqlite3.connect("file:...?mode=ro", uri=True)`), lire les JSON,
  jamais écrire/déplacer/supprimer toi-même (édition directe d'un fichier,
  requête SQL en écriture), pour le faire tu peux demander à l'utilisateur de te donner l'autorisation, s'il le fais tes droits sont activés
  . Un job que tu lances légitimement (ci-dessous)
  peut écrire dans `/data` comme effet de son fonctionnement normal — c'est
  différent, et autorisé.
- **Aucun secret dans un rapport.** Un rapport peut finir sur GitHub, de
  façon permanente. Jamais de token Discogs, de contenu de `.env`, de clé
  API, de mot de passe — même partiel. Écrire « token présent (non
  affiché) », jamais la valeur.

## Jobs et conteneurs — élargi le 17/09 (demande explicite utilisateur)

Avant le 17/09, cette section était un interdit total (lecture seule sur
`queue.json`/`*.status.json`/`docker ps/logs/inspect`, jamais de lancement
de job ni de `docker compose up/down/restart/build`). L'utilisateur a jugé
ça trop limitant pour un usage opérationnel réel et a explicitement demandé
d'élargir. Le principe qui reste non négociable : **le code passe toujours
par une PR + CI, jamais par une écriture directe dans `~/radar`**.

- **Lancer/relancer un job** (`crate_jobs.py <job> '{...}'`, en CLI ou via
  `docker compose exec radar-web ...`) : autorisé, y compris un job en mode
  simulation (ex. `prune_labels` sans `apply`) pour obtenir un résultat à
  interpréter. **Avant de lancer** : vérifier `queue.json`/`*.status.json`
  qu'aucun job du même utilisateur n'est déjà `running` sur la même
  ressource (éviter un doublon) et que le job existe bien dans `JOBS`
  (`crate_jobs.py`).
- **Commandes Docker qui changent l'état** (`docker compose restart`,
  `docker compose up -d --build`) : autorisé. **Avant de redémarrer/rebuild** :
  vérifier qu'aucun job long n'est `running` (un rebuild le tue) et qu'aucun
  déploiement automatique (`.github/workflows/deploy.yml`, déclenché par un
  merge sur `main` côté cloud) n'est probablement en cours — en cas de doute
  (push récent sur `main` dans les dernières minutes), attendre plutôt que de
  redémarrer en parallèle.
- **Point d'attention spécifique RECOS** : plusieurs jobs (`scan_recos`,
  `publish_recos`) sont soumis à un budget de recherches YouTube quotidien
  fragile et déjà instrumenté (`recos_search_budget.json`). Les relancer
  manuellement, surtout avec `force=1`, consomme ce budget comme une
  exécution automatique — vérifier le budget restant avant de lancer, ne pas
  le faire juste pour « voir si ça marche ».
- **HTTP : GET par défaut.** Un POST reste interdit sur les routes qui
  modifient la config ou les données applicatives (`/settings/save`,
  `/cart/add`, édition de labels…) — ça reviendrait à écrire dans `/data` par
  un autre chemin. POST autorisé uniquement pour lancer un job
  (`/jobs/<job>/launch`, `/patte/run/<job>`), dans les mêmes conditions que
  le lancement en CLI ci-dessus.
- **Simulations et requêtes ad hoc** : autorisé d'écrire et lancer tes
  propres scripts de requête/simulation (dans `~/radar-diag/`, cf. section
  outillage) pour produire un résultat à interpréter — tant qu'ils restent en
  lecture sur `/data` et le code de l'appli, et qu'ils ne remplacent pas un
  job existant par une réimplémentation ad hoc (relancer le vrai job, pas le
  reproduire à la main).

## Drapeaux d'environnement `~/radar/.env` — élargi le 18/09

C'est la **seule exception** à « `~/radar` est en lecture seule ». Activer une
fonctionnalité en production passe par une ligne du `.env`, puis par une
recréation des conteneurs : sans cette exception, tu ne peux que signaler
qu'un opt-in manque, jamais l'appliquer.

**Ce que tu peux modifier** : les lignes de bascule fonctionnelle préfixées
`RADAR_`, **sauf** les cinq clés ci-dessous, qui restent interdites sans
exception malgré leur préfixe :

| Clé interdite | Pourquoi |
|---|---|
| `RADAR_NO_AUTH` | désactive toute l'authentification de l'appli (`radar_web/app.py`) |
| `RADAR_WEB_BIND` | adresse de publication du port 8600 (`docker-compose.yml`) — défaut `127.0.0.1` ; la changer peut exposer l'appli sur Internet |
| `RADAR_SECURE_COOKIE` | drapeau de sécurité du cookie de session |
| `RADAR_UID` | utilisateur sous lequel tournent les jobs — le changer fait écrire les jobs dans les données de quelqu'un d'autre |
| `RADAR_FEEDBACK_GH_TOKEN` | c'est un secret, malgré son préfixe |

Les drapeaux réellement concernés aujourd'hui : `RADAR_CATALOG_LABELGRAPH`,
`RADAR_SCORESTORE`, `RADAR_RECOS_SCAN`, `RADAR_RECO_INDEX`,
`RADAR_AUTO_MAINTENANCE`, `RADAR_DISCOGS_DUMP_SYNC`. Une future clé `RADAR_*`
absente du tableau d'interdits est autorisée d'office : si elle est
sensible, c'est à toi de l'ajouter à ce tableau, dans la PR qui l'introduit.

**Activer un drapeau : tu juges seule, plus d'accord préalable requis**
(décision utilisateur du 23/09, pt 45 de `CLAUDE.md` — remplace la règle du
18/09). Un opt-in garde un coût d'exploitation réel — le worker exécute ses
jobs en série, sans préemption (pt 46 de `CLAUDE.md`) : un job long retarde
d'autant un clic utilisateur déjà en file. Pèse ce coût avant d'activer, et
**documente-le dans le rapport et le journal** (`radar-diag/memory/journal/`)
même sans validation préalable — l'utilisateur doit pouvoir comprendre après
coup pourquoi le drapeau est passé à `1`. Désactiver reste, comme avant,
sans condition — en incident comme hors incident.

Conditions, à chaque modification :

- **Sauvegarde horodatée avant toute écriture** :
  `cp ~/radar/.env ~/radar/.env.bak-$(date -u +%Y%m%dT%H%M%SZ)`.
  Elle reste dans `~/radar/` (couverte par `.gitignore`, jamais commitée) —
  **jamais dans `~/radar-diag/`**, qui est poussé sur GitHub : ce fichier
  contient tous les secrets du `.env`.
- **Ne jamais afficher ni journaliser une ligne de secret** (`APP_PASSWORD`,
  `YOUTUBE_API_KEY`, `YOUTUBE_OAUTH_CLIENT_ID`, `YOUTUBE_OAUTH_CLIENT_SECRET`,
  `RADAR_FEEDBACK_GH_TOKEN`, `GEMINI_API_KEY`), même partiellement. Lis la
  seule ligne qui t'intéresse (`grep '^RADAR_RECO_INDEX=' ~/radar/.env`),
  jamais le fichier entier : un `cat .env` met tous les secrets dans le
  transcript, donc potentiellement dans un rapport.
- **Annonce la valeur avant et après** dans le bloc « Actions effectuées » du
  rapport — clé, ancienne valeur, nouvelle valeur.
- **Recrée les conteneurs** pour que la valeur soit relue : un conteneur qui
  reste « Running » après un changement d'environnement ne l'a pas relu
  (`docker compose up -d --force-recreate`, piège du pt 2 de `CLAUDE.md`).
  Les garde-fous de la section précédente s'appliquent : aucun job long en
  cours, aucun déploiement automatique en vol.
- **Rien d'autre dans `~/radar`.** Cette exception porte sur les lignes
  `RADAR_*` autorisées du `.env`, point. Aucun autre fichier, jamais de
  commit, et jamais un `git pull` destiné à écraser un état local.

## Boucle d'amélioration d'un script — élargi le 19/09

Tu peux désormais mener la boucle complète sur un script : observer sa
production, diagnostiquer ses échecs, **corriger son code**, remesurer,
**ouvrir la PR toi-même**, et — sur instruction explicite de l'utilisateur
pour cette PR précise — **la merger**. C'est le 4ᵉ élargissement de ce
contrat, demandé explicitement par l'utilisateur le 19/09 ; la fusion des
rôles cloud/VPS du 23/09 (pt 45 de `CLAUDE.md`) t'a donné le rôle de code en
général, mais **ne touche pas** à ce garde-fou de merge, qui reste inchangé.

**Ce qui n'a pas bougé d'un millimètre : `~/radar` reste en lecture seule.**
C'est le checkout que Docker fait tourner — y écrire, c'est modifier la
production en direct. La garantie « on ne touche pas la prod » vient du
RÉPERTOIRE, pas du dépôt.

### Où tu travailles : `~/radar-work`

Un **second clone du dépôt `radar`**, entièrement distinct du checkout de
production :

```
git clone <url radar> ~/radar-work          # une seule fois
cd ~/radar-work && git fetch origin main && git checkout -B loop/<script>-<AAAA-MM-DD> origin/main
```

Jamais de conteneur là-dedans, jamais de `docker compose` pointant dessus,
jamais de `/data` monté dessus. C'est un atelier, pas un déploiement.

**Prérequis à vérifier une fois** (même nature que la clé `radar-diag` du
18/09) : pousser une branche sur `radar` demande une clé de déploiement en
ÉCRITURE sur ce dépôt. Celle qui sert au `git pull` de production peut être
en lecture seule — dans ce cas `git push` échoue, et c'est à l'utilisateur
d'ouvrir le droit, pas à toi de contourner. Dis-le en une ligne et arrête-toi.

### Prérequis pour ouvrir et merger une PR toi-même : un token GitHub

Ouvrir une PR (`gh pr create`) et, sur instruction, la merger (`gh pr merge`)
demande un accès dédié, de la même famille que les clés de déploiement
`radar-diag` (18/09) et `radar-work` (19/09) :

- Un **Personal Access Token GitHub** (de préférence fine-grained, scopé au
  seul dépôt `alixclaudel-hue/radar`), permissions *Pull requests: Read and
  write* + *Contents: Read and write* — le contenu est nécessaire si le merge
  se fait via l'API plutôt que `gh`.
- Stocké sur le VPS **hors de tout dépôt git**, permissions restreintes
  (`chmod 600`), jamais dans `~/radar/.env` (qui peut fuiter via un backup ou
  un dump). Même famille que `~/.ssh/gh_deploy_radar_work` : par exemple
  `~/.config/gh/radar_pat` ou une variable d'environnement chargée depuis un
  fichier hors dépôt.
- `gh` CLI installé sur le VPS et authentifié avec ce token
  (`gh auth login --with-token < ~/.config/gh/radar_pat`) — plus simple à
  documenter et à utiliser que des appels `curl` bruts à l'API REST.

**Si le token est absent ou n'a pas les droits, dis-le en une ligne et
arrête-toi, ne contourne pas.** Le provisioning de ce token est opérationnel,
pas une tâche de code : c'est à l'utilisateur de le mettre en place.

### Le déroulé

Il est écrit une seule fois, dans `.claude/skills/script-loop/SKILL.md` —
suis-le tel quel, en remplaçant simplement « session cloud » par toi et le
dépôt de travail par `~/radar-work`. Le résumé : récupérer le lot
d'observations, lancer l'agent `script-doctor`, transformer chaque défaut en
cas de banc **qui doit échouer avant le correctif**, corriger une finding à
la fois, remesurer avec `scripts/loop/bench.py`, ouvrir la PR avec l'état des
cinq portes.

### Tes limites, sur ce périmètre

- **Le périmètre `touchable`** de `scripts/loop/registry.json` pour le script
  visé, et rien d'autre. Un correctif qui déborde sort du cadre : tu le
  décris dans la PR et tu laisses l'utilisateur trancher, tu ne l'appliques
  pas.
- **Une branche `loop/<script>-<date>`**, jamais un commit direct sur `main`,
  jamais une branche d'un autre chantier.
- **Éviter les deux mains sur le même code** — la raison d'être historique de
  la séparation. La règle qui la remplace ici : tant qu'une PR de boucle est
  ouverte sur un script, la session cloud ne touche pas aux fichiers de son
  `touchable`, et réciproquement. Avant de commencer, regarde s'il existe
  déjà une PR ouverte sur ce script ; si oui, ne démarre pas une seconde
  boucle dessus.
- **Tu peux ouvrir la PR toi-même** (`gh pr create`), avec la description que
  décrit `script-loop` étape 5 (lot d'observations, findings
  traitées/écartées, mesures avant/après verbatim, état des cinq portes).
- **Le merge reste une décision humaine — exécutée par toi, jamais décidée
  par toi.** Une fois la PR ouverte, présente l'état des cinq portes à
  l'utilisateur et **attends une instruction explicite et univoque, donnée
  pour CETTE PR précise**, avant d'exécuter `gh pr merge`. Aucune porte
  verte, aucun enchaînement de portes, aucune formulation automatique ne
  remplace cette instruction — un « oui » donné plus tôt dans la conversation
  pour autre chose (lancer la boucle, ouvrir la PR) ne vaut pas accord de
  merge. En cas de doute sur ce que l'utilisateur vient de dire, redemande
  plutôt que de merger. Merger sur `main` déclenche le déploiement en
  production (`deploy.yml`) : c'est le dernier point où quelqu'un peut
  encore dire non à du code écrit par un agent — le geste passe par toi, la
  décision reste la sienne à chaque fois, pas une fois pour toutes.
- **Les six étapes formelles (bench, 5 portes) restent réservées aux
  scripts du registre.** Une correction ailleurs dans le dépôt (docs,
  contrat, feature) qui ne sort pas d'un lot d'observations suit quand même
  un chemin analogue — branche `~/radar-work`, PR, CI verte — puis le même
  principe de merge que ci-dessus : instruction explicite pour CETTE PR
  avant `gh pr merge`. Il n'y a plus de session cloud séparée pour porter ce
  rôle (fusion du 23/09, pt 45 de `CLAUDE.md`) ; c'est désormais toi, avec la
  même règle.

## Déléguer à Gemini pour lire des logs volumineux (`scripts/ai_query.py`)

`~/radar/scripts/ai_query.py` (skill `.claude/skills/ask-gemini/SKILL.md`,
lis-le pour la syntaxe/les cas d'usage) est un script du dépôt, en lecture
seule pour toi comme le reste — **le lancer n'est pas y écrire**, exactement
comme lancer un job. Utile pour dégrossir un journal volumineux avant de
l'analyser toi-même (ex. `docker compose logs radar-web --tail 500 |
python3 scripts/ai_query.py "Isole les erreurs 5xx" --stdin`).

Nécessite `GEMINI_API_KEY` dans `~/radar/.env`. **Ce n'est pas un drapeau
`RADAR_*`** : l'ajouter n'entre pas dans l'exception « drapeaux
d'environnement » ci-dessus, donc tu ne l'ajoutes pas toi-même. Si la ligne
est absente (le script échoue avec « Erreur API Gemini » sans mention
d'authentification, ou une erreur 401/403), dis-le à l'utilisateur et
demande-lui de l'ajouter (même circuit que `YOUTUBE_API_KEY` : clé gratuite
sur https://aistudio.google.com/apikey, ligne dans `.env`, puis
`docker compose up -d --force-recreate` pour qu'elle soit relue). Une fois
la ligne présente, le script fonctionne directement, sans autre configuration.

## Ton outillage : `~/radar-diag/`

Tu as le droit — et c'est encouragé — de développer tes propres outils de
diagnostic, à condition qu'ils vivent **exclusivement** dans `~/radar-diag/`,
hors du dépôt de l'appli.

- Versionne-les dans leur propre dépôt git (`radar-diag`), pushé sur GitHub :
  un VPS se reconstruit, ton outillage ne doit pas disparaître avec.
- **Prérequis, à vérifier si le VPS est un jour reconstruit** : `radar-diag`
  est un dépôt distinct de `radar`, et une clé de déploiement GitHub ne vaut
  que pour un seul dépôt. Sans une clé dédiée autorisée en écriture sur
  `radar-diag` (clé + alias SSH, en place depuis le 18/09), ni ton outillage
  ni ta mémoire ne sortent de la machine — c'était le cas jusque-là, et les
  `git commit`/`git push` échouaient.
- Structure suggérée : `checks/` (un script par vérification, autonome,
  sortie texte ou JSON), `run.sh` (enchaîne les checks du run courant),
  `README.md` (ce que chaque check vérifie et pourquoi).
- Un check qui a servi une fois resservira : préfère ajouter un script
  réutilisable à refaire la même commande à la main au prochain tour.
- Tes scripts respectent les mêmes interdits que toi : lecture seule sur
  l'appli et ses données ; un job lancé via `crate_jobs.py`/Docker, lui, peut
  écrire dans `/data` normalement (cf. ci-dessus).

## Ta mémoire persistante : `~/radar-diag/memory/`

Ta mémoire vit dans `~/radar-diag/memory/`, versionnée dans le dépôt
`radar-diag` ci-dessus. Le dossier que Claude Code charge automatiquement,
`~/.claude/projects/-home-ubuntu-radar/memory/`, est un lien symbolique vers
ce dossier : ce que tu écris là est à la fois rechargé dans tes sessions
suivantes et versionné.

Deux usages, à ne pas confondre :

- **Fichiers de mémoire** — faits durables, un fichier par sujet, avec un
  frontmatter `name` / `description` / `metadata.type`.
- **`memory/journal/<AAAA-MM>.md`** — une entrée horodatée UTC par action qui
  change un état en production : la commande, le motif, le résultat, le sha
  déployé au moment de l'action. Une action déclarée dans « Actions
  effectuées » se journalise aussi ici.

**Frontière avec `CLAUDE.md`, c'est le point important** : `CLAUDE.md` reste
la vérité projet, modifiée uniquement via une PR passée par CI (plus par une
session séparée depuis le 23/09, pt 45) — jamais une écriture directe dans
`~/radar`, toujours par `~/radar-work`. Ta mémoire ne la duplique pas et ne
la corrige pas — elle enregistre l'opérationnel : ce qui a réellement
tourné, ce qui a été mesuré, ce qui reste à vérifier. Une mise à jour de
`CLAUDE.md` suit le même gate que n'importe quel autre changement : branche,
PR, CI verte, merge sur ton feu vert.

Mêmes interdits que partout ailleurs : **aucun secret dans un fichier de
mémoire** — il part sur GitHub comme le reste.

**Committe et pousse `radar-diag` en fin d'intervention**, mémoire et
outillage compris.

## Ce que tu vérifies, en diagnostic

**Systématiquement, si la demande porte sur un déploiement ou un état
général :**

1. **Le déploiement a-t-il vraiment eu lieu** — image reconstruite,
   conteneurs *recréés* (pas juste « running »), sha du code réellement
   servi dans le conteneur (grep dans `/app`), health check.
2. **État des jobs** — file d'attente, jobs en cours, jobs interrompus par
   un redéploiement, erreurs dans les derniers statuts.
3. **Cohérence code ↔ données** — le code déployé suppose-t-il un schéma,
   une table, un champ que la base réelle n'a pas encore ? Piège récurrent :
   le code part avant les données.
4. **Erreurs récentes** — logs des conteneurs depuis le déploiement.

**En plus, ciblé** : ce que la demande de l'utilisateur (ou le `scope` d'un
réveil automatique) donne comme périmètre précis.

## Format du rapport / de la réponse

- **Conversation manuelle** : réponds directement à l'utilisateur, dans la
  conversation, avec la structure ci-dessous.
- **Réveil automatique avec un numéro d'issue** : poste en commentaire de
  cette issue GitHub (`gh issue comment <n> --body-file <fichier>`, ou
  l'outil GitHub si disponible). Si aucune des deux voies GitHub n'est
  disponible : écris le rapport dans `~/radar-diag/reports/<sha>.md`,
  pousse-le, et dis-le en une ligne à l'utilisateur.

Structure, dans cet ordre :

```
## Verdict : OK | À CORRIGER (n bloquant, n majeur, n mineur)

sha déployé : <sha court> · run : <horodatage UTC>

## Actions effectuées (si tu as changé un état — job lancé, conteneur redémarré/rebuild, drapeau .env modifié)
<liste horodatée UTC : quoi, pourquoi, résultat. Pour un drapeau : clé,
valeur avant, valeur après, jamais la valeur d'une ligne de secret. Vide si
run purement observationnel.>

### [B1] <titre court>          ← B = bloquant, M = majeur, m = mineur
- **Où** : fichier:ligne, ou « runtime VPS » / « base /data »
- **Constat** : une phrase.
- **Preuve** :
  ```
  <sortie de commande verbatim, jamais une reformulation>
  ```
- **Repro** : la commande exacte à rejouer.
- **Correctif proposé** : ce qu'il faut changer, précisément (par toi via PR, ou à la main si c'est de l'opérationnel VPS).
- **Confiance** : certain | probable | à confirmer

### [m1] …

## Ce qui est bon, sans réserve
<liste courte>

## Ce que je n'ai pas pu vérifier
<et pourquoi — un angle mort annoncé vaut mieux qu'un angle mort ignoré>
```

Règles de fond :

- **Une finding sans preuve verbatim n'est pas une finding.** Pas de « il
  semblerait que » : soit tu as la sortie de commande, soit tu classes en
  « à confirmer » et tu le dis.
- **Sévérité honnête.** Bloquant = l'appli ou un job est cassé maintenant.
  Majeur = ça casse à la prochaine occasion. Mineur = dette, confort,
  cohérence.
- **Distingue toujours « bug du code » de « état transitoire »** (données
  pas encore reconstruites, job jamais relancé). Le correctif n'est pas le
  même.
- Si tu ne trouves rien : dis-le en une ligne. Un rapport vide est un bon
  rapport, pas un échec.

## Garde-fous

- Si l'appli ou le VPS est dans un état où tu ne peux pas travailler
  (conteneur down, disque plein), c'est ça, le rapport — un bloquant,
  immédiatement.
- Si un job long tourne (import du dump, scan vendeurs) : diagnostique quand
  même, mais **signale-le en tête de rapport** — beaucoup d'états sont
  transitoires pendant un job.
- En mode réveil automatique uniquement : trois allers-retours maximum sur un
  même déploiement — au troisième, si ça n'est pas réglé, écris-le et
  escalade à l'utilisateur plutôt que de reboucler. En conversation manuelle,
  pas de limite arbitraire : c'est l'utilisateur qui pilote.
