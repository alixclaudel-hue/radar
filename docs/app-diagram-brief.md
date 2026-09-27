# Diagram Brief: Radar - App Overview

**Layout**: top-to-bottom
**Flow summary**: Shows how a browser request or an ingest job enters Radar, which layer does the work, and where the results land - the precomputed data stores that both the web app and the read-only ops service read from.

---

## Elements

Create a group labeled "Entrees" that contains: Navigateur, APIs et sources externes.

Create a group labeled "Point d'entree" that contains: Caddy, radar-web (app.py).

Create a group labeled "Logique" that contains: File de jobs, radar-worker, crate_jobs.py + radar_jobs, scoring.py, radar_ops.

Create a group labeled "Donnees / Sorties" that contains: Mirroir Discogs + graphe de labels, Donnees par utilisateur.

Create a box labeled "Navigateur". Note: l'utilisateur (proprietaire ou invite), clic ou requete HTTP.

Create a box labeled "APIs et sources externes". Note: Discogs (API + dump mensuel), YouTube Data API, Spotify Web API, Bandcamp API - chacune avec ses propres quotas.

Create a box labeled "Caddy". Note: reverse-proxy HTTPS, route vers radar-web et radar_ops.

Create a box labeled "radar-web (app.py)". Note: FastAPI, ~100 routes ; middleware _guard fait session cookie/CSRF puis fixe le uid courant (contextvars).

Create a box labeled "File de jobs". Note: radar/jobs.py, data/jobs/queue.json ; priorite 1 = clic utilisateur devant 0 = fond.

Create a box labeled "radar-worker". Note: radar_web/worker.py, conteneur separe, execution SERIELLE uniquement (partage le quota Discogs par IP).

Create a box labeled "crate_jobs.py + radar_jobs". Note: sous-processus OS ; regroupe dump.py, ingest.py, djsets.py, curation.py, graph.py, scorestore.py, search.py, recos.py - un seul module importe par job.

Create a box labeled "scoring.py". Note: le "cerveau" - classe Ctx, DAG de dependances, album_score/ascore/reco_rows ; precompute plutot que calcul a la volee.

Create a box labeled "radar_ops". Note: service Docker separe (port 8610), lit le meme volume /data en LECTURE SEULE, jamais de donnee metier ecrite.

Create a box labeled "Mirroir Discogs + graphe de labels". Note: discogs_dump.sqlite3 (+ tracks), catalog_labelgraph.sqlite3 - data/shared, partage entre utilisateurs.

Create a box labeled "Donnees par utilisateur". Note: scorestore.sqlite3, crate_radar_config.json, taste_corpus.json, producer_graph.json, recos_playlist.json - data/users/<uid>.

Draw an arrow from Navigateur to Caddy. Label it "requete HTTPS".

Draw an arrow from Caddy to radar-web (app.py). Label it "routage".

Draw an arrow from Caddy to radar_ops. Label it "routage (Tailscale-only)".

Draw an arrow from radar-web (app.py) to scoring.py. Label it "album_score / reco_rows".

Draw an arrow from scoring.py to Mirroir Discogs + graphe de labels. Label it "lit catalogue + graphe".

Draw an arrow from scoring.py to Donnees par utilisateur. Label it "lit scores precalcules + config + corpus".

Draw an arrow from scoring.py to radar-web (app.py). Label it "score / classement".

Draw an arrow from radar-web (app.py) to Navigateur. Label it "page HTML/HTMX rendue".

Draw an arrow from radar-web (app.py) to File de jobs. Label it "POST /jobs/<name>/launch".

Draw an arrow from File de jobs to radar-worker. Label it "job en attente".

Draw an arrow from radar-worker to crate_jobs.py + radar_jobs. Label it "subprocess python crate_jobs.py <job>".

Draw an arrow from APIs et sources externes to crate_jobs.py + radar_jobs. Label it "reponses API (ingest/import)".

Draw an arrow from crate_jobs.py + radar_jobs to Mirroir Discogs + graphe de labels. Label it "import_discogs_dump / build_catalog_labelgraph".

Draw an arrow from crate_jobs.py + radar_jobs to Donnees par utilisateur. Label it "ingest / scorestore / recos - ecrit".

Draw an arrow from radar_ops to Mirroir Discogs + graphe de labels. Label it "lecture diagnostic".

Draw an arrow from radar_ops to Donnees par utilisateur. Label it "lecture diagnostic".

Mark radar-web (app.py) as entry point.

Mark APIs et sources externes as external.

Mark Mirroir Discogs + graphe de labels as database.

Mark Donnees par utilisateur as database.

Mark radar-worker as async.

Add a note to crate_jobs.py + radar_jobs: "+16 modules radar/*.py non dessines individuellement (paths, websession, accounts, store, discogs, textmatch, ytcache, bandcamp, volumo, sellers, recoindex, learn, opslog, vocab, features, codeversion) et ~25 templates HTMX".

---

## Notes
- Le tableau "Key files" (section 8 de la doc) liste 14 fichiers cles ; regroupes ici sous 5 boites de logique pour rester lisible (regle des 8-10 elements les plus significatifs pour un projet a plus de 20 fichiers cles).
- La table "State / data flow" (section 6) est representee par les deux boites de donnees plutot que ligne par ligne - le detail (uid, session, feature flags) reste dans le document source.
- `radar_ops` n'ecrit jamais de donnee metier ; ses fleches vers les boites de donnees sont volontairement a sens unique (lecture seule).
