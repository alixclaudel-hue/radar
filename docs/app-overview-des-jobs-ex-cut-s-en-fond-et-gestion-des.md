# Radar — jobs de fond et gestion des files d'attente

Vue d'ensemble générée par relecture du code (12/09/2026). Périmètre strictement délimité ci-dessous — voir "Ce qui est dedans / dehors".

## 1. Ce que fait cette partie du projet

Radar a plein de tâches trop longues pour tenir dans une requête web normale : importer le dump Discogs (~1h45), scanner 141 vendeurs, recalculer des scores sur des centaines de milliers de sorties, chercher des vidéos YouTube une par une... Cette partie du projet est le mécanisme qui permet de lancer ces tâches en arrière-plan, de suivre leur avancement en direct dans le navigateur, de les enchaîner automatiquement quand une tâche doit continuer le travail d'une autre, et de ne jamais perdre le fil si le serveur redémarre en plein milieu.

Concrètement : on clique sur un bouton dans l'appli ("Scanner maintenant"), la tâche part dans un processus séparé, une barre de progression s'anime toute seule, et on peut fermer la page — le travail continue sur le serveur.

## 2. Ce qui est dedans / ce qui est dehors (périmètre du document)

**Dedans** (décrit en détail ici) :
- `radar_web/radar/jobs.py` — la file d'attente elle-même (empiler, dépiler, statut, verrous anti-doublon, nettoyage des orphelins).
- `radar_web/worker.py` — la boucle qui dépile la file en continu et qui décide, entre deux jobs, s'il faut enfiler une tâche d'entretien automatique.
- `crate_jobs.py` — la classe `Job` (écriture du fichier de statut, journal, gestion du `.stop`), la fonction `main()` qui sert de point d'entrée du sous-processus, le dictionnaire `JOBS` qui relie un nom de job à sa fonction, et la logique de **chaînage** entre jobs (`_chain_publish_recos`, `_chain_scorestore_tracks`).
- Les routes de `radar_web/app.py` qui lancent/arrêtent/affichent un job (`/jobs/{name}/launch`, `/jobs/{name}/stop`, `/jobs/{name}/status`, `/jobs/{name}/log`) et `VALID_JOBS`.

**Dehors** (mentionné seulement pour situer le fonctionnement, jamais détaillé) :
- Ce que chaque job fait *à l'intérieur* de son métier (parser un dump Discogs, interroger l'API YouTube, calculer un score...) — voir `docs/app-overview-scoring-lot1.md` pour `scoring.py`/`Ctx`, et le code de chaque module (`discogs_dump.py`, `ytcache.py`, `scorestore.py`, `sellers.py`, `catalog_labelgraph.py`) pour le détail métier.
- La reprise **interne à un job précis** après interruption (ex. `discogs_dump_import.state.json`, propre à `job_import_discogs_dump`) — mentionnée en passant au § 6 pour la distinguer de la reprise au niveau de la file, mais pas développée.
- Les pages web qui affichent le résultat des jobs (templates HTMX comme `patte.html`, `reco-radar.html`).
- Le contenu des fichiers de données lus/écrits par chaque job (config, corpus, playlist RECOS...) — seuls les fichiers propres au *mécanisme de jobs* (`queue.json`, `*.status.json`, `*.stop`) sont couverts.

Ce périmètre existe bel et bien tel que décrit ci-dessus : ni plus petit (les trois fichiers `jobs.py`/`worker.py`/`crate_jobs.py` forment un tout cohérent), ni plus grand (aucun autre fichier ne participe à la mécanique de file/statut/chaînage) que ce que ce document couvre.

## 3. Vocabulaire

- **Job** : une tâche de fond identifiée par un nom (`"scan_recos"`, `"import_discogs_dump"`, ...) et associée à une fonction `job_xxx(job, params)` dans `crate_jobs.py`.
- **File d'attente (queue)** : liste unique, partagée entre tous les utilisateurs, stockée dans `/data/jobs/queue.json`.
- **Worker** : le processus unique (`radar_web.worker`, service Docker `radar-worker`) qui dépile la file en boucle et exécute chaque job en sous-processus.
- **Statut** : l'état d'avancement d'un job pour un utilisateur donné, dans `/data/jobs/<uid>/<name>.status.json` — c'est ce que la page web relit pour dessiner la barre de progression.
- **Chaînage** : un job qui, en se terminant, enfile lui-même le job suivant de son pipeline (ex. `scan_recos` → `publish_recos`).
- **uid** : identifiant de l'utilisateur propriétaire du job (`"owner"` par défaut — chantier multi-utilisateur en cours, cf. `docs/architecture.md`).

## 4. Fichiers de ce périmètre

```
radar_web/
  worker.py                  ->  boucle unique : dépile la file, lance crate_jobs.py en
                                  sous-processus, et — quand la file est vide — décide
                                  d'enfiler les tâches d'entretien automatique (opt-in
                                  par variable d'environnement)
  radar/jobs.py               ->  la file elle-même : launch()/stop()/status()/running(),
                                  lecture/écriture de queue.json, reap_orphans()
  app.py (extrait)            ->  routes web qui pilotent les jobs :
                                    VALID_JOBS (liste blanche des jobs lançables
                                      depuis le navigateur)
                                    POST /jobs/{name}/launch  -> jobs.launch()
                                    POST /jobs/{name}/stop    -> jobs.stop()
                                    GET  /jobs/{name}/status  -> fragment HTMX
                                      (barre de progression, auto-poll toutes
                                      les 2-10s tant que le job tourne)
                                    GET  /jobs/{name}/log     -> télécharge le
                                      journal complet en .txt
crate_jobs.py                 ->  point d'entrée du sous-processus (python crate_jobs.py
                                  <job> <params_json>) :
                                    classe Job          : écrit le .status.json, gère
                                                          le fichier .stop, le journal
                                    dict JOBS           : nom -> fonction job_xxx
                                    main()               : dispatch + capture d'erreur
                                    fonctions job_xxx()  : une par tâche (métier HORS
                                                          périmètre, cf. § 2)
                                    _chain_publish_recos(),
                                    _chain_scorestore_tracks() : enfilent le job suivant
                                                          du pipeline
```

Fichiers de données propres au mécanisme (pas au métier de chaque job) :

```
/data/jobs/
  queue.json                 ->  la file d'attente globale (tous utilisateurs)
  <uid>/
    <name>.status.json        ->  statut courant d'un job pour un utilisateur
    <name>.stop                ->  présence du fichier = demande d'arrêt
```

## 5. Comment un job est lancé et exécuté — vue d'ensemble

```
Navigateur (bouton "Scanner"/"Importer"/...)
        |
        v
POST /jobs/{name}/launch   (radar_web/app.py)
        |  name doit être dans VALID_JOBS
        v
jobs.launch(name, params, uid)      (radar_web/radar/jobs.py)
        |  refuse un doublon (déjà "queued"/"running" pour cet uid+name)
        |  empile {id, uid, name, params, state:"queued"} dans queue.json
        v
                    ... le navigateur repart avec /jobs/{name}/status
                    (fragment HTMX qui se réinterroge lui-même toutes
                    les 2 à 10 s tant que le job tourne)

============================ pendant ce temps, en continu ============================

radar_web/worker.py :: main()   (processus séparé, service "radar-worker")
        |
        v
    boucle "while True":
        - relit queue.json
        - choisit un job "queued" (round-robin : privilégie un autre
          uid que le dernier servi, pour ne pas affamer les autres
          utilisateurs si l'un d'eux a un gros backlog)
        - marque ce job "running" dans queue.json
        - lance en sous-processus :
              python crate_jobs.py <name> <params_json>
              avec RADAR_UID=<uid>, CRATE_DATA_DIR=<paths.DATA>
        - attend la fin (timeout 6h) ou l'échec
        - retire le job de queue.json
        - si la file est vide : vérifie les tâches d'entretien
          automatique (scan vendeurs hebdo, import dump mensuel,
          graphe labels, scorestore, RECOS...) et en enfile si besoin
        - dort 2 s, recommence
```

```
crate_jobs.py (sous-processus, une exécution = un job)
        |
        v
main() : vérifie que <name> est dans JOBS, crée un objet Job(name),
         appelle JOBS[name](job, params)
        |
        v
job_xxx(job, params)  -- écrit sa progression via job.tick()/job.msg()/job.sub()
                          -- vérifie job.stopped() régulièrement pour s'arrêter proprement
                          -- (le VRAI travail de chaque job est HORS périmètre, § 2)
        |
        v
job.finish(message ou error)  -- écrit le statut final dans <name>.status.json
        |
        v
[optionnel] le job appelle _chain_publish_recos() ou _chain_scorestore_tracks()
            avant de finir : enfile directement le job suivant du pipeline
            (sans attendre le prochain passage du worker)
```

Le navigateur, lui, ne parle **jamais** directement à `crate_jobs.py` : il ne fait que lire/écrire des fichiers JSON via les routes `/jobs/*`, et c'est le worker, en tâche de fond, qui fait vraiment tourner le code métier.

## 6. La file d'attente en détail (`radar_web/radar/jobs.py`)

- **Une seule file globale**, `queue.json`, partagée par tous les utilisateurs — pas une file par utilisateur. Chaque entrée porte son propre `uid`, ce qui permet au worker de faire du round-robin.
- **`launch(name, params, uid)`** refuse de doubler un job déjà en file ou en cours pour ce couple `(uid, name)` — pas de double-lancement accidentel si l'utilisateur clique deux fois. Elle refuse aussi si le statut persisté indique encore "running" et date de moins de 150 secondes (garde-fou pendant la fenêtre où le worker vient de démarrer le sous-processus mais n'a pas encore mis à jour la file).
- **`status(name, uid)`** fusionne deux sources pour construire ce que les templates affichent : la position dans la file si le job est encore "queued" (`{"queued": True, "position": N}`), sinon le contenu du fichier `.status.json` écrit par `crate_jobs.Job`, avec le flag `running` recalculé depuis la file (pas depuis le fichier de statut seul — un fichier de statut peut mentir si le processus a été tué brutalement).
- **`stop(name, uid)`** : si le job est encore seulement "queued", il est retiré directement de la file (jamais lancé). S'il est déjà "running", un fichier `<name>.stop` est déposé — c'est au job lui-même de le voir (`job.stopped()`) et de s'arrêter proprement à son prochain point de contrôle ; rien ne tue le sous-processus de force.
- **`reap_orphans()`**, appelée une fois au démarrage du worker : nettoie les jobs restés marqués "running" dans la file. Comme le worker est unique et strictement séquentiel, un job encore "running" au démarrage ne peut être qu'un orphelin d'un worker précédent tué en plein travail (redéploiement, OOM) — jamais un vrai job concurrent. Sans ce nettoyage, `launch()` refuserait silencieusement tout nouveau lancement de ce job indéfiniment (l'appli paraîtrait bloquée).

### Deux niveaux de "reprise" à ne pas confondre

- **Reprise au niveau de la file** (ce périmètre) : si le worker (ou son conteneur) redémarre, `reap_orphans()` débloque la file au prochain démarrage ; l'utilisateur doit alors recliquer sur "lancer" — la file ne relance rien toute seule après un crash.
- **Reprise interne à un job** (métier, hors périmètre mais mentionnée pour clarté) : certains jobs longs, comme `job_import_discogs_dump`, gèrent eux-mêmes leur propre checkpoint (ex. `discogs_dump_import.state.json`, qui retient l'étape atteinte et le nombre de sorties déjà committées) pour reprendre au bon endroit plutôt que de tout refaire si le worker les interrompt en cours de route. Ce mécanisme est propre à chaque job et n'a rien à voir avec `queue.json`.

## 7. Le worker (`radar_web/worker.py`)

Processus unique et strictement séquentiel : **un seul job à la fois, jamais deux en parallèle**, même avec plusieurs utilisateurs. C'est un choix assumé : Discogs limite le débit d'appels par IP, et une seule IP sert tout le monde ; l'exécution en série protège ce quota partagé plutôt que de le faire éclater entre jobs concurrents.

Chaque tour de boucle :
1. relit `queue.json`,
2. choisit le prochain job "queued" en **round-robin** (préfère un utilisateur différent du dernier servi, pour qu'un gros backlog d'un utilisateur n'affame pas les autres),
3. s'il n'y a rien à faire, vérifie une série de tâches d'entretien automatique (voir § 8) avant de dormir 2 secondes.

Chaque job est exécuté par `subprocess.run([python, crate_jobs.py, name, params_json], env={..., "RADAR_UID": uid, "CRATE_DATA_DIR": paths.DATA}, timeout=6h)`. Le worker capture toute exception du sous-processus (log seulement, il continue sa boucle) — un job qui plante ne bloque jamais les suivants.

### Tâches d'entretien automatique (déclenchées seulement quand la file est vide)

Toutes optionnelles (variable d'environnement `RADAR_XXX=1`), et toutes vérifient d'abord qu'aucune instance du même job n'est déjà en file avant d'en enfiler une :

| Fonction | Variable d'activation | Cadence | Enfile |
|---|---|---|---|
| `_maybe_weekly_scan` | `RADAR_SELLER_SCAN` | 7 j (si le vendeur le plus récent scanné date d'il y a plus de 7 j) | `scan_catalog` (owner) |
| `_maybe_monthly_dump_sync` | `RADAR_DISCOGS_DUMP_SYNC` | vérifie 1×/jour, agit seulement si un nouveau dump mensuel est publié | `import_discogs_dump` (owner) |
| `_maybe_catalog_labelgraph_build` | `RADAR_CATALOG_LABELGRAPH` | vérifie 1×/heure, agit seulement si le dump partagé a changé depuis le dernier graphe | `build_catalog_labelgraph` (owner) |
| `_maybe_scorestore_build` | `RADAR_SCORESTORE` | toutes les 15 min | `scorestore_releases`, pour **chaque utilisateur enregistré** (`paths.all_uids()`), pas seulement owner |
| `_maybe_auto_maintenance` | `RADAR_AUTO_MAINTENANCE` | par job : `canonicalize` 7j, `profile_labels` 7j, `build_graph` 30j | le job concerné (owner) |
| `_maybe_recos_scan` | `RADAR_RECOS_SCAN` | **désactivée en dur** (`return` immédiat en tête de fonction, en pause depuis le 10/09) | — |

Point notable partagé par `_maybe_catalog_labelgraph_build` et `_maybe_auto_maintenance` : la cadence est calculée à partir du **`finished_at` réellement persisté** dans le statut du job (une exécution qui va au bout sans erreur), jamais depuis l'instant où le job a été *lancé*. Un déploiement qui tue un job en plein milieu ne doit jamais compter comme "fait" — sinon un job à cadence mensuelle (`build_graph`) pourrait être oublié jusqu'à 30 jours avant d'être retenté.

## 8. Statut et journal d'un job (`crate_jobs.Job`)

Chaque exécution instancie `Job(name, total=...)`, qui :
- supprime un éventuel fichier `.stop` laissé par une exécution précédente,
- initialise `<name>.status.json` avec `{running:True, done:0, total, last:"", message:"", error:None, log:[], started_at, finished_at:None}`,
- expose ensuite au code métier :
  - `tick(last, inc=1, total=None)` : incrémente `done`, met à jour `last` et ajoute une ligne au journal (`log`, plafonné aux 1000 dernières lignes),
  - `msg(m)` : message principal affiché au-dessus de la barre de progression,
  - `sub(done, total, label)` : sous-progression optionnelle (ex. articles du vendeur en cours de scan),
  - `stopped()` : vrai si un `.stop` a été déposé — le code métier doit le vérifier lui-même à intervalles réguliers pour s'arrêter proprement,
  - `finish(message, error)` : écrit l'état final, nettoie le `.stop` s'il en restait un.

`main()` (dans `crate_jobs.py`) capture toute exception non gérée par le job lui-même, appelle `job.finish(error=...)`, puis relève l'exception (le worker la logge et passe au job suivant).

Le fichier `.status.json` est ensuite ce que la route `GET /jobs/{name}/status` (dans `app.py`) relit pour construire le fragment HTML HTMX : barre de progression (`done/total`), sous-barre si `sub_total` est renseigné, estimation du temps restant (extrapolée depuis le débit observé, ou un indice de durée typique codé en dur pour les jobs connus comme longs — `JOB_DURATION_HINTS`), et un bouton "arrêter" qui poste sur `/jobs/{name}/stop`. Le fragment se réinterroge lui-même toutes les 2 secondes (10 au-delà de 30 secondes d'exécution) tant que le job est en file ou en cours — pur polling, pas de WebSocket.

## 9. Chaînage entre jobs

Deux pipelines à deux étages existent aujourd'hui, tous deux implémentés par le même patron : le premier job, juste avant `job.finish()`, appelle une petite fonction `_chain_xxx()` qui enfile le second **si il n'est pas déjà en file** — que le premier ait trouvé du travail ou non, pour qu'une file laissée en plan par un quota épuisé continue à se vider au passage suivant.

```
Pipeline RECOS RADAR :

  job_scan_recos                          job_publish_recos
  (sélectionne des candidats     ---->     (cherche chaque candidat sur
   depuis scorestore.track_scores,          YouTube, ajoute à la playlist
   AUCUN appel réseau depuis le              interne recos_playlist.json,
   Lot 5 — lit un précalcul déjà              budget de recherches par
   fait par le pipeline scorestore)           lancement)
        |                                         |
        `-- _chain_publish_recos() --------------'
            (avant job.finish(), que le scan ait
             trouvé 0 ou N candidats)


Pipeline précalcul des scores (scorestore) :

  job_scorestore_releases                 job_scorestore_tracks
  (note CHAQUE sortie des labels  ---->    (rafraîchit gratuitement les
   suivis, aucun appel réseau,              pistes déjà connues, puis va
   toujours la totalité, pas de              chercher via l'API Discogs
   file de nouveauté)                        la tracklist réelle des
        |                                     sorties top-scorées qui
        `-- _chain_scorestore_tracks() ------ n'en ont encore aucune,
            (avant job.finish())              plafonné par lancement)
                                                     |
                                    se rechaîne lui-même (_chain_scorestore_tracks())
                                    si le plafond de la passe (b) a été atteint —
                                    tourne en continu tant qu'il reste du travail,
                                    sans attendre le prochain passage à cadence fixe
                                    du worker (§ 8, _maybe_scorestore_build)
```

Point important : `build_catalog_labelgraph`, `scorestore_releases` et `scorestore_tracks` **ne sont pas dans `VALID_JOBS`** (§ 10) — ils ne sont jamais lançables depuis un bouton de l'appli web. Seul le worker les enfile, via les tâches d'entretien du § 8, et via le chaînage `scorestore_releases -> scorestore_tracks` décrit ci-dessus. `job_scan_veille`, en comparaison, ne chaîne rien : c'est un job à un seul étage.

## 10. Où l'appli web pilote tout ça (`radar_web/app.py`)

```python
VALID_JOBS = {"fetch_collection", "ingest_youtube", "ingest_spotify", "ingest_bandcamp",
              "merge_corpus", "scan_veille", "scan_sellers", "build_graph", "profile_labels",
              "ingest_djsets", "resolve_artists", "canonicalize", "enrich", "scan_catalog",
              "import_discogs_dump", "scan_recos", "publish_recos"}
```

- `POST /jobs/{name}/launch` : refuse tout `name` hors de `VALID_JOBS` (liste blanche — un nom de job arbitraire dans l'URL ne peut rien lancer), applique d'éventuels paramètres par défaut (`JOB_PARAMS`, ex. `{"deep": True}` pour les ingestions), lit le champ `force` du formulaire (bouton "forcer" de certains jobs, ex. `import_discogs_dump`), puis appelle `jobs.launch(...)`. Retourne directement le fragment de statut (`job_status_frag`).
- `POST /jobs/{name}/stop` : même liste blanche, appelle `jobs.stop(...)`.
- `GET /jobs/{name}/status` : construit le fragment HTMX décrit au § 8 (barre de progression, ETA, bouton stop, auto-poll).
- `GET /jobs/{name}/log` : sert le journal complet (`log`) du statut en `.txt` téléchargeable — utile pour du diagnostic depuis un mobile qui n'a pas accès au fichier `.status.json` sur le VPS.
- Un job peut aussi être enfilé directement par du code applicatif plutôt que par une action explicite de l'utilisateur : par exemple `_queue_enrich()` (ligne ~368) enfile `enrich` chaque fois que des labels/artistes sont mis en attente d'enrichissement, sans passer par une route `/jobs/*/launch`.

## 11. Décisions de conception à connaître (pièges/non-évidences)

- **Une seule file pour tous les utilisateurs, un seul worker séquentiel** : ce n'est pas une limitation provisoire mais un choix pour protéger le quota Discogs partagé (une seule IP sert tout le monde). Ne pas paralléliser sans revoir cette contrainte.
- **`build_catalog_labelgraph`, `scorestore_releases`, `scorestore_tracks` sont absents de `VALID_JOBS`** : ils ne sont accessibles qu'au worker (tâches d'entretien opt-in), jamais via un bouton web. Un développeur qui voudrait exposer un bouton "recalculer les scores" doit explicitement les ajouter à `VALID_JOBS`.
- **`_maybe_recos_scan` est désactivée par un `return` en tête de fonction**, pas par la variable d'environnement `RADAR_RECOS_SCAN` (hors d'atteinte depuis une session cloud sans accès VPS) — CLAUDE.md interdit de la réactiver sans demande explicite.
- **Le chaînage se déclenche même sur un résultat vide** (0 candidat trouvé, 0 sortie sans tracklist) : c'est volontaire, pour qu'une file partiellement traitée (ex. quota YouTube épuisé en cours de route) continue à se vider au chaînage suivant plutôt que d'attendre le prochain passage à cadence fixe du worker.
- **Cadence des tâches d'entretien calculée depuis `finished_at` persisté, jamais depuis l'heure de lancement** : un job tué en plein milieu par un redéploiement ne doit jamais compter comme "fait" pour sa cadence.
- **`reap_orphans()` ne tourne qu'au démarrage du worker**, en confiance dans le fait que le worker est unique et séquentiel — un job "running" trouvé dans la file au démarrage est nécessairement un orphelin, jamais un run concurrent légitime.
- **La reprise après crash n'est pas automatique au niveau de la file** : après un redémarrage du worker, un job resté "running" est nettoyé (marqué en erreur/interrompu) mais pas relancé tout seul — il faut recliquer. Seule la reprise *interne* de certains jobs (ex. `import_discogs_dump`, via son propre fichier d'état) permet de reprendre au bon endroit plutôt que de tout refaire.

## 12. Table des fichiers clés

| Fichier | Rôle dans ce périmètre |
|---|---|
| `radar_web/radar/jobs.py` | File d'attente : `launch`, `stop`, `status`, `running`, `reap_orphans` |
| `radar_web/worker.py` | Boucle unique qui dépile la file et lance les tâches d'entretien automatique |
| `crate_jobs.py` | Classe `Job` (statut/journal/stop), dict `JOBS`, `main()`, fonctions de chaînage `_chain_*` |
| `radar_web/app.py` | Routes `/jobs/{name}/launch`, `/stop`, `/status`, `/log`, liste blanche `VALID_JOBS` |
| `radar_web/radar/paths.py` | `JOBS_DIR`, `JOBS_SCRIPT`, `DATA`, `all_uids()` — emplacements lus par `jobs.py`/`worker.py` |
| `/data/jobs/queue.json` | La file d'attente elle-même (donnée, pas code) |
| `/data/jobs/<uid>/<name>.status.json` | Statut courant d'un job pour un utilisateur (donnée) |
| `/data/jobs/<uid>/<name>.stop` | Marqueur de demande d'arrêt (donnée) |

## 13. Pour aller chercher une fonctionnalité précise

- **Ajouter un nouveau job lançable depuis l'appli** : écrire `job_xxx(job, params)` dans `crate_jobs.py`, l'ajouter au dict `JOBS`, puis l'ajouter à `VALID_JOBS` dans `radar_web/app.py` (sinon la route `/jobs/{name}/launch` le refusera silencieusement).
- **Changer la fréquence d'une tâche d'entretien automatique** : `radar_web/worker.py`, constantes `AUTO_MAINT_EVERY`, `SELLER_SCAN_EVERY`, `DUMP_SYNC_CHECK_EVERY`, `CATALOG_LABELGRAPH_CHECK_EVERY`, `SCORESTORE_CHECK_EVERY`.
- **Comprendre pourquoi un job ne se relance pas** : vérifier `jobs.launch()` (doublon en file, ou statut "running" de moins de 150s) puis `jobs.status()`/`reap_orphans()` si un déploiement vient d'avoir lieu.
- **Ajouter un troisième étage à un pipeline chaîné** : suivre le patron `_chain_publish_recos()`/`_chain_scorestore_tracks()` — une fonction appelée juste avant `job.finish()` qui vérifie l'absence de doublon en file avant d'enfiler la suite.
- **Diagnostiquer un job en cours sur le VPS sans accès SSH** : bouton de téléchargement du journal, route `GET /jobs/{name}/log` (sert `.status.json["log"]` en `.txt`).
