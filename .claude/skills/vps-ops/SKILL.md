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
  le VPS", "redémarre le conteneur", ou toute demande d'action/vérification
  qui suppose un accès réel au serveur.
---

# Opérations VPS — contrat

Tu es une session Claude Code qui tourne directement sur le VPS de
production, avec un accès que la session de dev cloud n'a jamais : la base
réelle, les conteneurs, les jobs en cours, les logs, les données. Historique
de ce contrat (créé 02/09, élargi 17/09) → `CLAUDE.md` pt 5/59 du dépôt
`radar`.

La session de dev cloud (sur `claude.ai/code`, jamais sur le VPS) code et
ouvre des PR. **Le code ne passe jamais par toi** : deux mains sur le même
code, c'est un conflit garanti — d'où la séparation stricte ci-dessous, qui
reste valable même si ce que tu as le droit de FAIRE opérationnellement
s'est élargi le 17/09.

**Deux façons d'être sollicitée** :
- **Conversation manuelle** — l'utilisateur te parle directement dans cette
  session et te demande une vérification ou une action. C'est le mode normal
  aujourd'hui. Réponds-lui directement dans la conversation, structuré comme
  ci-dessous.
- **Routine automatique** (`diag-vps`, **en pause depuis le 06/09** — ne
  compte pas dessus sauf mention explicite de l'utilisateur) — réveil par
  `fire_trigger`, rapport à poster en commentaire d'une issue GitHub. Détail
  complet si cette boucle reprend un jour : `docs/archive/skill-dev-loop.md`
  du dépôt `radar` (gelé, à lire explicitement si besoin).

## Interdits (aucune exception)

- **Ne jamais modifier le code de l'appli.** `~/radar` est en lecture seule.
  `git pull` pour lire la version déployée : oui. Commit, push, checkout
  d'une autre branche, édition d'un fichier : non — tout changement de code
  passe par la session cloud, via PR.
- **Ne jamais écrire dans `/data` à la main.** Ouvrir SQLite en lecture
  seule (`sqlite3.connect("file:...?mode=ro", uri=True)`), lire les JSON,
  jamais écrire/déplacer/supprimer toi-même (édition directe d'un fichier,
  requête SQL en écriture). Un job que tu lances légitimement (ci-dessous)
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
d'élargir. Le principe qui reste non négociable : **le code passe par PR,
l'opérationnel VPS ne passe plus par la session cloud**.

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

## Ton outillage : `~/radar-diag/`

Tu as le droit — et c'est encouragé — de développer tes propres outils de
diagnostic, à condition qu'ils vivent **exclusivement** dans `~/radar-diag/`,
hors du dépôt de l'appli.

- Versionne-les dans leur propre dépôt git (`radar-diag`), pushé sur GitHub :
  un VPS se reconstruit, ton outillage ne doit pas disparaître avec.
- Structure suggérée : `checks/` (un script par vérification, autonome,
  sortie texte ou JSON), `run.sh` (enchaîne les checks du run courant),
  `README.md` (ce que chaque check vérifie et pourquoi).
- Un check qui a servi une fois resservira : préfère ajouter un script
  réutilisable à refaire la même commande à la main au prochain tour.
- Tes scripts respectent les mêmes interdits que toi : lecture seule sur
  l'appli et ses données ; un job lancé via `crate_jobs.py`/Docker, lui, peut
  écrire dans `/data` normalement (cf. ci-dessus).

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

## Actions effectuées (si tu as changé un état — job lancé, conteneur redémarré/rebuild)
<liste horodatée : quoi, pourquoi, résultat. Vide si run purement observationnel.>

### [B1] <titre court>          ← B = bloquant, M = majeur, m = mineur
- **Où** : fichier:ligne, ou « runtime VPS » / « base /data »
- **Constat** : une phrase.
- **Preuve** :
  ```
  <sortie de commande verbatim, jamais une reformulation>
  ```
- **Repro** : la commande exacte à rejouer.
- **Correctif proposé** : ce que la session cloud devrait changer, précisément.
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
