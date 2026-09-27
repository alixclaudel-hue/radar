"""Dump Discogs local : `import_discogs_dump`, `build_catalog_labelgraph`.
"""

import os
import sqlite3
from datetime import datetime


def job_import_discogs_dump(job, params):
    """Télécharge le dernier dump mensuel officiel Discogs (Releases, Labels,
    Artists) et reconstruit le référentiel local SQLite (radar/discogs_dump.py)
    — TOUS formats pour les sorties depuis l'élargissement du 11/09 (vinyle,
    CD, fichier audio, etc. ; `is_vinyl` marque celles qui sont au format
    vinyle 12"/LP, pour un filtrage optionnel côté consommateur — cf. module
    docstring de discogs_dump.py), avec genre/style/label/artiste + crédits
    par sortie, et l'identité canonique (id Discogs) de chaque label et
    artiste. Ne concerne QUE le catalogue (données stables) ; le marketplace
    (vendeurs, prix, inventaire) n'existe pas dans ces dumps et reste
    toujours en API live.

    Les trois dumps vont dans la MÊME base reconstruite (radar/discogs_dump.py
    open_new_db/finalize_new_db) : une seule bascule atomique à la fin, jamais
    de base à moitié à jour (releases neuves, labels/artists périmés ou
    inversement). Un dump mensuel est un instantané complet, jamais un delta :
    "actualiser" retélécharge et reconstruit l'index en entier (peut prendre
    longtemps selon la bande passante et la taille des fichiers — plusieurs Go
    au total, l'essentiel pour Releases ; sensiblement plus volumineux depuis
    l'élargissement tous formats — le téléchargement/parsing XML ne change
    pas [déjà tout le flux était lu pour trier vinyle/non-vinyle], mais le
    nombre de lignes réellement insérées en base augmente).

    Tracklists (diagnostic VPS 2026-09-15) : parsées dans la MÊME passe XML que
    releases et écrites dans une base SÉPARÉE (`discogs_dump.TRACKS_DB_PATH`,
    sa propre bascule atomique) — `job_scorestore_tracks` les lit en priorité,
    zéro appel API, et ne retombe sur l'API que pour une sortie absente du
    dump (ajoutée sur Discogs après ce dernier import mensuel).

    Reprenable : chaque déploiement redéploie le conteneur du worker (tout merge sur
    main, pas seulement ceux qui touchent au dump), ce qui tue ce job en plein milieu
    d'un import de ~1h45 s'il tombe pendant. `discogs_dump_import.state.json`
    (dd.load_import_state/save_import_state) retient l'étape atteinte et, pour
    Releases (le gros morceau), le nombre de sorties déjà committées : au prochain
    lancement, l'import reprend à cet endroit plutôt que de repartir de zéro.
    Labels/Artists (quelques minutes chacun) repartent de zéro sur eux-mêmes si
    interrompus, sans reperdre Releases."""
    from radar_web.radar import discogs_dump as dd

    force = bool(params.get("force"))
    job.msg("Recherche du dernier dump Discogs disponible…")
    try:
        latest = dd.find_latest_dump_date()
    except Exception as e:                       # noqa: BLE001
        return job.finish(error=f"Impossible de lister les dumps Discogs : {e}")

    meta = dd.get_meta()
    state = dd.load_import_state()
    # une reprise porte forcément sur le MÊME dump (un dump mensuel est un instantané
    # complet, jamais un delta) — un checkpoint d'un autre dump_date est caduc, ignoré.
    resuming = state.get("dump_date") == latest and state.get("stage") in (
        "releases", "labels", "artists", "finalize")
    if meta.get("dump_date") == latest and not force and not resuming:
        return job.finish(f"Déjà à jour (dump du {latest}).")
    if not resuming:
        state = {"dump_date": latest, "stage": "releases",
                  "releases_done": 0, "releases_vinyl": 0, "n_labels": 0, "n_artists": 0}
        dd.save_import_state(state)
    else:
        job.msg(f"Reprise d'un import interrompu du dump {latest} (redémarré à l'étape « {state['stage']} », "
                f"{state['releases_done']:,} sortie(s) déjà traitée(s))…".replace(",", " "))

    kinds = ("releases", "labels", "artists")
    gz_paths = {k: os.path.join(dd.RAW_DIR, f"discogs_{latest}_{k}.xml.gz") for k in kinds}

    for kind in kinds:
        job.msg(f"Téléchargement du dump {kind} ({latest})…")

        def dl_progress(done, total, kind=kind):
            job.sub(done=done, total=total, label=f"téléchargement {kind}")
            if job.stopped():
                raise InterruptedError("stop demandé pendant le téléchargement")

        try:
            dd.download_dump(latest, gz_paths[kind], progress_cb=dl_progress, kind=kind)
        except InterruptedError:
            return job.finish("Arrêté pendant le téléchargement — fichier(s) partiel(s) conservé(s), reprise au prochain lancement.")
        except Exception as e:                    # noqa: BLE001
            return job.finish(error=f"Téléchargement du dump {kind} échoué : {e}")

    # CHECKSUM.txt couvre les 4 fichiers du mois (releases/labels/artists/masters) — on ne
    # télécharge pas masters, mais rien ne justifiait de ne vérifier QUE releases parmi les
    # 3 qu'on importe (labels/artists corrompus silencieusement sinon, diagnostic mineur Lot 4).
    for kind in kinds:
        job.msg(f"Vérification de l'intégrité du dump {kind}…")
        ok = dd.verify_checksum(latest, gz_paths[kind])
        if ok is False:
            try:
                os.remove(gz_paths[kind])
            except OSError:
                pass
            return job.finish(error=f"Somme de contrôle invalide pour {kind} — fichier corrompu, relance le job.")
        # ok is None : CHECKSUM.txt indisponible (ou ce fichier n'y figure pas), on ne bloque pas l'import dessus.

    est_total = meta.get("n_total") or 20_000_000
    con = dd.open_new_db(resume=resuming)
    # Tracklists (diagnostic VPS 2026-09-15, cf. docstring de TRACKS_DB_PATH) : base
    # séparée, remplie dans la MÊME passe XML que releases — jamais un second parsing
    # du flux. `resuming` couvre les deux bases ensemble (même dump_date, même notion
    # de reprise) : un import interrompu reprend les deux .new en l'état.
    tracks_con = dd.open_new_tracks_db(resume=resuming)
    try:
        if state["stage"] == "releases":
            job.msg(f"Import du dump {latest} — sorties…")

            def releases_progress(done):
                job.sub(done=done, total=max(est_total, done), label="import des sorties")

            def releases_checkpoint(done, vinyl):
                # `seen`/`n_vinyl` ne remontent ici qu'après un commit (cf. import_releases) :
                # toujours sûr à écrire comme point de reprise.
                state["releases_done"], state["releases_vinyl"] = done, vinyl
                dd.save_import_state(state)

            n_total, n_vinyl = dd.import_releases(
                con, gz_paths["releases"], progress_cb=releases_progress,
                resume_from=state["releases_done"], resume_vinyl=state["releases_vinyl"],
                checkpoint_cb=releases_checkpoint, tracks_con=tracks_con)
            state["stage"] = "labels"
            dd.save_import_state(state)
        else:
            n_total, n_vinyl = state["releases_done"], state["releases_vinyl"]

        if state["stage"] == "labels":
            job.msg(f"Import du dump {latest} — labels…")

            def labels_progress(done):
                job.sub(done=done, total=max(done, 1), label="import des labels")
            # pas de reprise fine ici (fichier bien plus petit que releases, quelques
            # minutes tout au plus) : un labels interrompu repart de zéro sur ce seul
            # fichier, sans reperdre releases. INSERT OR REPLACE (clé = id) : rejouer le
            # parsing en entier ne duplique rien.
            n_labels = dd.import_labels(con, gz_paths["labels"], progress_cb=labels_progress)
            state["stage"], state["n_labels"] = "artists", n_labels
            dd.save_import_state(state)
        else:
            n_labels = state["n_labels"]

        if state["stage"] == "artists":
            job.msg(f"Import du dump {latest} — artistes…")
            # artist_aliases est un INSERT simple (pas REPLACE, pas de clé naturelle) :
            # si une tentative précédente sur CETTE étape a été interrompue en cours de
            # route, ses lignes partielles doivent être purgées avant de tout rejouer,
            # sinon les alias déjà committés se dupliqueraient. Sans effet sur un premier
            # passage (table encore vide).
            con.execute("DELETE FROM artist_aliases")

            def artists_progress(done):
                job.sub(done=done, total=max(done, 1), label="import des artistes")
            n_artists = dd.import_artists(con, gz_paths["artists"], progress_cb=artists_progress)
            state["stage"], state["n_artists"] = "finalize", n_artists
            dd.save_import_state(state)
        else:
            n_artists = state["n_artists"]

        job.msg("Construction des index…")
        dd.finalize_new_db(con)
        dd.finalize_new_tracks_db(tracks_con)
    except Exception as e:                        # noqa: BLE001
        con.close()                                # sans effet si finalize_new_db a déjà fermé con
        tracks_con.close()                         # idem pour tracks_con et finalize_new_tracks_db
        return job.finish(error=f"Import échoué : {e}")

    dd.clear_import_state()

    for kind in kinds:
        try:
            os.remove(gz_paths[kind])             # pas besoin de garder les .gz une fois importés
        except OSError:
            pass

    dd.save_meta({"dump_date": latest, "imported_at": datetime.now().isoformat(timespec="seconds"),
                  "n_total": n_total, "n_vinyl": n_vinyl, "n_labels": n_labels, "n_artists": n_artists})

    n_tracks_con = sqlite3.connect(dd.TRACKS_DB_PATH)
    try:
        n_tracks = n_tracks_con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    finally:
        n_tracks_con.close()

    # Un nouveau dump doit rafraîchir toute la chaîne dérivée sans intervention :
    # la résolution canonique (bien meilleure avec les namevariations du nouveau
    # dump artistes) puis le profilage des labels.
    # priority=0 explicite : ce chaînage automatique n'est pas un clic
    # utilisateur — sans ça il hérite du défaut interactif de jobs.launch()
    # et double à tort les jobs de fond d'autres utilisateurs déjà en file
    # (cf. CLAUDE.md, correctif famine scorestore du 24/09).
    from radar_web.radar import jobs as job_queue
    job_queue.launch("canonicalize", {"scope": "corpus"}, uid="owner", priority=0)
    job_queue.launch("profile_labels", {"limit": 150}, uid="owner", priority=0)

    job.finish(f"Dump {latest} importé : {n_total} sortie(s) tous formats "
               f"(dont {n_vinyl} vinyle 12\"/LP), {n_labels} label(s), {n_artists} artiste(s), "
               f"{n_tracks} piste(s) (tracklists locales). Canonisation + profilage enfilés.")


def job_build_catalog_labelgraph(job, params):
    """Reconstruit le graphe label<->label GLOBAL (radar/catalog_labelgraph.py)
    à partir du référentiel Discogs partagé (discogs_dump.sqlite3), sur tout
    le catalogue importé, indépendamment du goût ou du corpus d'un
    utilisateur quelconque — un seul graphe, partagé par tout le monde. Deux
    origines de lien (jamais mélangées, cf. docstring du module) : labels
    partageant un artiste crédité ("artist"), et hiérarchie label
    enfant/parent déclarée par Discogs lui-même ("parent", labels.xml). Ne
    pas confondre avec `job_build_graph` (mode "taste"), qui construit un
    graphe PAR UTILISATEUR à partir de ses graines Cœur/Aimés + corpus
    écouté.

    Lot 2 de la refonte scoring (cf. CLAUDE.md point 38) : infrastructure
    pure, rien n'en consomme encore le résultat pour l'instant (prévu aux
    lots suivants). Déclenché par `radar_web/worker.py`
    (`_maybe_catalog_labelgraph_build`) quand le dump partagé a changé depuis
    la dernière construction — jamais rien d'autre, ce job ne tourne pas à
    cadence fixe."""
    from radar_web.radar import catalog_labelgraph as clg
    from radar_web.radar import discogs_dump as dd

    if not dd.available():
        return job.finish(error="Référentiel Discogs local absent — lance d'abord import_discogs_dump.")

    dump_meta = dd.get_meta()
    force = bool(params.get("force"))
    if not force and not clg.needs_rebuild(dump_meta):
        return job.finish(f"Déjà à jour (dump du {dump_meta.get('dump_date')}).")

    job.msg("Construction du graphe labels global (référentiel Discogs partagé)…")

    def progress(done):
        job.sub(done=done, total=max(done, 1), label="artistes traités")

    try:
        stats = clg.build(progress_cb=progress, stop_cb=job.stopped)
    except InterruptedError:
        return job.finish("Arrêté — graphe existant conservé, reprendra de zéro au prochain lancement.")
    except RuntimeError as e:
        return job.finish(error=str(e))
    except Exception as e:                        # noqa: BLE001
        return job.finish(error=f"Construction du graphe labels échouée : {e}")

    clg.save_meta({"dump_date": dump_meta.get("dump_date"),
                   "built_at": datetime.now().isoformat(timespec="seconds"), **stats})
    job.finish(f"Graphe labels global reconstruit ({dump_meta.get('dump_date')}) : "
               f"{stats['n_labels']} label(s), {stats['n_edges']} lien(s) — "
               f"{stats['n_artist_edges']} par artiste partagé "
               f"({stats['n_artists_used']} artiste(s) utilisé(s), "
               f"{stats['n_artists_skipped_prolific']} écarté(s) — trop de labels distincts), "
               f"{stats['n_parent_edges']} par hiérarchie label parent/enfant.")
