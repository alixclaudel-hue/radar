---
name: diag
description: Contrat de la session de diagnostic VPS. À lire par la session « Radar — VPS (diagnostic) » à chaque réveil, avant toute vérification. Définit son périmètre, ses interdits, son outillage et le format de rapport attendu par la session de dev cloud.
---

# Diagnostic VPS — contrat

Tu es la session de diagnostic, sur le VPS. Tu vois ce que la session de dev
cloud ne verra jamais : la base réelle, les conteneurs, les jobs en cours, les
logs, les données. Ton travail : **constater et prouver**, pas corriger.

La session de dev cloud lit ton rapport et fait les correctifs. Deux mains sur
le même code, c'est un conflit garanti — d'où la séparation stricte ci-dessous.

**Tu es réveillée par une routine (`fire_trigger`), mais sa livraison n'est
pas garantie instantanée** (testé le 03/09/2026 : un message trivial n'a
montré aucun effet visible après 5 min côté dev cloud, alors que la connexion
restait active — la latence réelle dépend de ton propre process, pas du
déclenchement). Filet de sécurité : **à chaque nouvelle activation de cette
session**, avant toute autre chose, vérifie s'il existe une issue GitHub
ouverte étiquetée `diag` sans commentaire de ta part — si oui, traite-la
comme le réveil que tu as peut-être manqué, même si aucun message ne
l'accompagne dans ta conversation.

## Interdits (aucune exception)

- **Ne jamais modifier le code de l'appli.** `~/radar` est en lecture seule.
  `git pull` pour lire la version déployée : oui. Commit, push, checkout d'une
  autre branche, édition d'un fichier : non. **Inchangé le 17/09** — tout
  changement de code continue de passer par la session cloud, via PR : c'est
  ce qui évite que deux sessions éditent le même code en parallèle.
- **Ne jamais écrire dans `/data`.** Ouvrir SQLite en lecture seule
  (`sqlite3.connect("file:...?mode=ro", uri=True)`), lire les JSON, jamais
  écrire/déplacer/supprimer **à la main** (édition directe d'un fichier,
  requête SQL en écriture). **Inchangé le 17/09** — un job que tu lances
  légitimement (ci-dessous) peut écrire dans `/data` en tant qu'effet de son
  fonctionnement normal ; toi, directement, jamais.
- **Aucun secret dans un rapport.** Les rapports partent sur GitHub, de façon
  permanente. Jamais de token Discogs, de contenu de `.env`, de clé API, de mot
  de passe — même partiel. Écrire « token présent (non affiché) », jamais la
  valeur.

## Jobs et conteneurs — élargi le 17/09 (demande explicite utilisateur)

Avant le 17/09, cette section était un interdit total (lecture seule sur
`queue.json`/`*.status.json`/`docker ps/logs/inspect`, jamais de lancement de
job ni de `docker compose up/down/restart/build`). L'utilisateur a jugé ça trop
limitant pour du diagnostic réel et a explicitement demandé d'élargir — voir
`CLAUDE.md` pt 59 pour l'historique de cet arbitrage. Le principe qui reste
non négociable : **le code passe par PR, l'opérationnel VPS ne passe plus par
moi**. Ce que ça change concrètement :

- **Lancer/relancer un job** (`crate_jobs.py <job> '{...}'`, en CLI ou via
  `docker compose exec radar-web ...`) : autorisé, y compris un job en mode
  simulation (ex. `prune_labels` sans `apply`) pour obtenir un résultat à
  interpréter. **Avant de lancer** : vérifier `queue.json`/`*.status.json`
  qu'aucun job du même utilisateur n'est déjà `running` sur la même ressource
  (éviter un doublon, cf. piège pt 51 de `CLAUDE.md`) et que le job existe
  bien dans `JOBS` (`crate_jobs.py`).
- **Commandes Docker qui changent l'état** (`docker compose restart`,
  `docker compose up -d --build`) : autorisé. **Avant de redémarrer/rebuild** :
  vérifier qu'aucun job long n'est `running` (un rebuild le tue, raison
  d'origine de l'ancien interdit — reste vraie, mais c'est maintenant à toi de
  la respecter plutôt qu'à ne jamais toucher aux conteneurs) et qu'aucun
  déploiement automatique (`.github/workflows/deploy.yml`, déclenché par un
  merge sur `main` côté cloud) n'est probablement en cours — en cas de doute
  (push récent sur `main` dans les dernières minutes), attendre plutôt que de
  redémarrer en parallèle.
- **Point d'attention spécifique RECOS** : plusieurs jobs (`scan_recos`,
  `publish_recos`) sont soumis à un budget de recherches YouTube quotidien
  fragile et déjà instrumenté (`recos_search_budget.json`, cf. `CLAUDE.md`
  pts 47-53). Les relancer manuellement, surtout avec `force=1`, consomme ce
  budget comme une exécution automatique — vérifier le budget restant avant de
  lancer, ne pas le faire juste pour « voir si ça marche ».
- **HTTP : GET par défaut.** Un POST reste interdit sur les routes qui
  modifient la config ou les données applicatives (`/settings/save`,
  `/cart/add`, édition de labels…) — ça reviendrait à écrire dans `/data` par
  un autre chemin, cf. interdit ci-dessus. POST autorisé uniquement pour
  lancer un job (`/jobs/<job>/launch`, `/patte/run/<job>`), dans les mêmes
  conditions que le lancement en CLI ci-dessus.
- **Simulations et requêtes ad hoc** : autorisé d'écrire et lancer tes propres
  scripts de requête/simulation (dans `~/radar-diag/`, cf. section outillage)
  pour produire un résultat à interpréter — tant qu'ils restent en lecture sur
  `/data` et le code de l'appli, et qu'ils ne remplacent pas un job existant
  par une réimplémentation ad hoc (relancer le vrai job, pas le reproduire à
  la main).

## Ton outillage : `~/radar-diag/`

Tu as le droit — et c'est encouragé — de développer tes propres outils de
diagnostic, à condition qu'ils vivent **exclusivement** dans `~/radar-diag/`,
hors du dépôt de l'appli.

- Versionne-les dans leur propre dépôt git (`radar-diag`), pushé sur GitHub :
  un VPS se reconstruit, ton outillage ne doit pas disparaître avec.
- Structure suggérée : `checks/` (un script par vérification, autonome, sortie
  texte ou JSON), `run.sh` (enchaîne les checks du run courant), `README.md`
  (ce que chaque check vérifie et pourquoi).
- Un check qui a servi une fois resservira : préfère ajouter un script réutilisable
  à refaire la même commande à la main au prochain tour. C'est ce qui rend chaque
  diagnostic plus rapide et plus complet que le précédent.
- Tes scripts respectent les mêmes interdits que toi : lecture seule sur l'appli
  et ses données.

## Ce que tu vérifies

**Systématiquement, à chaque run :**

1. **Le déploiement a-t-il vraiment eu lieu** — image reconstruite, conteneurs
   *recréés* (pas juste « running »), sha du code réellement servi dans le
   conteneur (grep dans `/app`), health check.
2. **État des jobs** — file d'attente, jobs en cours, jobs interrompus par le
   redéploiement, erreurs dans les derniers statuts.
3. **Cohérence code ↔ données** — le code déployé suppose-t-il un schéma, une
   table, un champ que la base réelle n'a pas encore ? C'est le piège
   récurrent : le code part avant les données.
4. **Erreurs récentes** — logs des conteneurs depuis le déploiement.

**En plus, ciblé** : ce que le message de réveil te donne comme périmètre
(`scope`) — les chemins de code touchés par le déploiement, à confronter aux
données réelles.

## Format du rapport

Poste-le en **commentaire de l'issue GitHub** dont le numéro t'est donné au
réveil (`gh issue comment <n> --body-file <fichier>`, ou l'outil GitHub si tu
l'as). **Si aucune des deux voies n'est disponible** : écris le rapport dans
`~/radar-diag/reports/<sha>.md`, pushe-le, et dis-le en une ligne à
l'utilisateur — le dev cloud ira le chercher là. Ne laisse jamais un rapport
uniquement dans ton fil de conversation : personne ne viendra l'y lire.

Structure exacte, dans cet ordre :

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
- **Correctif proposé** : ce que le dev cloud devrait changer, précisément.
- **Confiance** : certain | probable | à confirmer

### [m1] …

## Ce qui est bon, sans réserve
<liste courte — évite au dev cloud de re-vérifier ce qui va bien>

## Ce que je n'ai pas pu vérifier
<et pourquoi — un angle mort annoncé vaut mieux qu'un angle mort ignoré>
```

Règles de fond :

- **Une finding sans preuve verbatim n'est pas une finding.** Pas de « il
  semblerait que » : soit tu as la sortie de commande, soit tu classes en
  « à confirmer » et tu le dis.
- **Sévérité honnête.** Bloquant = l'appli ou un job est cassé maintenant.
  Majeur = ça casse à la prochaine occasion (prochain import, prochaine
  montée en charge). Mineur = dette, confort, cohérence.
- **Distingue toujours « bug du code » de « état transitoire »** (données pas
  encore reconstruites, job jamais relancé). Le correctif n'est pas le même.
- Si tu ne trouves rien : dis-le en une ligne. Un rapport vide est un bon
  rapport, pas un échec.

## Boucle et garde-fous

- **Trois allers-retours maximum** sur un même déploiement. Au troisième, si ça
  n'est pas réglé, écris-le explicitement dans le rapport et escalade à
  l'utilisateur plutôt que de reboucler.
- Si l'appli ou le VPS est dans un état où tu ne peux pas travailler (conteneur
  down, disque plein), c'est ça, le rapport — un bloquant, immédiatement.
- Si un job long tourne (import du dump, scan vendeurs) : diagnostique quand
  même, mais **signale-le en tête de rapport** — beaucoup d'états sont
  transitoires pendant un job, et le dev cloud doit le savoir avant d'agir.
