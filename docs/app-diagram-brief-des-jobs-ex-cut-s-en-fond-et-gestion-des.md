# Diagram Brief: Jobs de fond et gestion des files d'attente (Radar)

**Layout**: top-to-bottom
**Flow summary**: Un clic dans le navigateur empile un job dans une file JSON partagée ; un worker unique la dépile en boucle et lance chaque job en sous-processus, qui écrit son statut ; le navigateur relit ce statut en boucle (polling HTMX), et certains jobs enchaînent automatiquement le suivant de leur pipeline.

---

## Elements

Create a group labeled "Entrée (navigateur)" that contains: Bouton lancer/arrêter un job.

Create a group labeled "File d'attente (radar/jobs.py)" that contains: queue.json, launch() / stop(), status() / reap_orphans().

Create a group labeled "Worker (worker.py)" that contains: Boucle principale, Tâches d'entretien automatique.

Create a group labeled "Exécution (crate_jobs.py)" that contains: Job (classe), JOBS (dict) + job_xxx(), Chaînage (_chain_publish_recos / _chain_scorestore_tracks).

Create a group labeled "Sortie / état" that contains: *.status.json + journal, Fragment HTMX (barre de progression).

Create a box labeled "Bouton lancer/arrêter un job". Note: pages Réglages / Mes labels & artistes / Reco Radar, POST /jobs/{name}/launch ou /stop.

Create a box labeled "queue.json". Note: fichier unique, partagé par tous les utilisateurs, une entrée = {id, uid, name, params, state}.

Create a box labeled "launch() / stop()". Note: refuse les doublons (queued/running <150s) ; stop() retire de la file si "queued", dépose un .stop si "running".

Create a box labeled "status() / reap_orphans()". Note: fusionne position en file + *.status.json ; reap_orphans nettoie les orphelins "running" au démarrage du worker.

Create a box labeled "Boucle principale". Note: processus unique et séquentiel, un seul job à la fois, round-robin entre utilisateurs, dort 2s entre deux tours.

Create a box labeled "Tâches d'entretien automatique". Note: opt-in par variable RADAR_XXX, enfilées seulement quand la file est vide (scan vendeurs, import dump, graphe labels, scorestore...).

Create a box labeled "Job (classe)". Note: écrit *.status.json, gère le fichier .stop, journal plafonné à 1000 lignes.

Create a box labeled "JOBS (dict) + job_xxx()". Note: main() dispatche vers la fonction métier du job ; le travail réel de job_xxx() est hors périmètre.

Create a box labeled "Chaînage (_chain_publish_recos / _chain_scorestore_tracks)". Note: appelé juste avant job.finish(), enfile le job suivant même si le résultat est vide.

Create a box labeled "*.status.json + journal". Note: par utilisateur, contient done/total, message, log, error, started_at/finished_at.

Create a box labeled "Fragment HTMX (barre de progression)". Note: GET /jobs/{name}/status, auto-poll 2 à 10s tant que le job tourne.

Create a box labeled "Logique métier de chaque job (scoring.py, discogs_dump.py, ytcache.py, scorestore.py...)". Note: hors périmètre de ce diagramme.

Create a box labeled "Templates HTMX (patte.html, reco-radar.html...)". Note: hors périmètre, affichent le fragment de statut.

Draw an arrow from Bouton lancer/arrêter un job to launch() / stop(). Label it "POST /jobs/{name}/launch ou /stop".

Draw an arrow from launch() / stop() to queue.json. Label it "empile ou retire {uid, name, params, state}".

Draw an arrow from Boucle principale to queue.json. Label it "relit, choisit un job queued (round-robin)".

Draw an arrow from Boucle principale to JOBS (dict) + job_xxx(). Label it "sous-processus: python crate_jobs.py <name> <params>".

Draw an arrow from Tâches d'entretien automatique to queue.json. Label it "enfile si cadence atteinte et pas déjà en file".

Draw an arrow from JOBS (dict) + job_xxx() to Job (classe). Label it "tick() / msg() / sub() / stopped()".

Draw an arrow from JOBS (dict) + job_xxx() to Logique métier de chaque job (scoring.py, discogs_dump.py, ytcache.py, scorestore.py...). Label it "délègue le travail réel".

Draw an arrow from Job (classe) to *.status.json + journal. Label it "écrit statut + journal".

Draw an arrow from JOBS (dict) + job_xxx() to Chaînage (_chain_publish_recos / _chain_scorestore_tracks). Label it "avant job.finish()".

Draw an arrow from Chaînage (_chain_publish_recos / _chain_scorestore_tracks) to queue.json. Label it "enfile le job suivant du pipeline".

Draw an arrow from *.status.json + journal to Fragment HTMX (barre de progression). Label it "GET /jobs/{name}/status".

Draw an arrow from Fragment HTMX (barre de progression) to Templates HTMX (patte.html, reco-radar.html...). Label it "affiché dans".

Draw an arrow from Fragment HTMX (barre de progression) to Bouton lancer/arrêter un job. Label it "auto-poll 2-10s tant que en cours".

Draw an arrow from status() / reap_orphans() to queue.json. Label it "lit position / nettoie les orphelins au démarrage".

Mark Bouton lancer/arrêter un job as entry point.

Mark queue.json as database.

Mark *.status.json + journal as database.

Mark Logique métier de chaque job (scoring.py, discogs_dump.py, ytcache.py, scorestore.py...) as external.

Mark Templates HTMX (patte.html, reco-radar.html...) as external.

Mark Boucle principale as async.

---

## Notes

- Périmètre : uniquement le mécanisme de file/statut/worker/chaînage (`radar_web/radar/jobs.py`, `radar_web/worker.py`, `crate_jobs.py`, routes `/jobs/*` de `radar_web/app.py`). Tout ce que fait chaque job *à l'intérieur* de son métier (parser un dump, appeler l'API YouTube, calculer un score) est traité comme une seule boîte externe, jamais détaillée — voir `docs/app-overview-scoring-lot1.md` pour ce détail.
- Les deux pipelines chaînés (`scan_recos → publish_recos` et `scorestore_releases → scorestore_tracks`) partagent le même patron visuel : la boîte "Chaînage" est un mécanisme générique, pas un pipeline dessiné deux fois — le texte de la flèche vers queue.json nomme les deux exemples.
- `build_catalog_labelgraph`, `scorestore_releases` et `scorestore_tracks` ne sont accessibles que via "Tâches d'entretien automatique", jamais via le bouton du navigateur (absents de `VALID_JOBS`) — si le rendu le permet, cette nuance peut être ajoutée en note sur la flèche correspondante plutôt qu'en élément séparé, pour rester sous 20 éléments.
- La reprise après crash (`reap_orphans()`) et la reprise interne à un job (ex. `discogs_dump_import.state.json`) sont deux mécanismes distincts décrits dans le document source (§6) mais non représentés séparément ici pour ne pas surcharger le diagramme — seule `reap_orphans()` (niveau file) apparaît.
