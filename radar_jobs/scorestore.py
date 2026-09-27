"""Précalcul des scores (`scorestore_releases`, `scorestore_tracks`, rechaînés
par lots) et index des recos (`reco_index`).
"""

import json
import sys
import time
from datetime import datetime, timezone

from radar_jobs.common import cfg_load, normalize_label, RADAR_UID
from radar_jobs.sources import discogs_get
from radar_jobs.tracks import _is_continuous_mix, _is_placeholder_artist, _track_credit_artist


SCORESTORE_PER_LABEL_LIMIT = 500  # requête par label plutôt que globale — même raison que
# le D6 de job_scan_recos (avant sa refonte au Lot 5) : un label prolifique ne doit pas
# écraser les autres avant même le scoring.
SCORESTORE_FETCHES_PER_RUN = 1000  # repli si scoring.scorestore.fetches_per_run absent
# lot par lancement quand les tracklists sont couvertes par le dump local (gratuit,
# zéro appel réseau, cf. discogs_dump.TRACKS_DB_PATH) — bien plus large que
# SCORESTORE_FETCHES_PER_RUN (qui ne borne plus que les VRAIS appels API, le repli
# pour une sortie absente du dump), tout en restant borné pour qu'un run garde une
# durée raisonnable (le job se rechaîne lui-même si le lot était plein, cf. plus bas).
SCORESTORE_LOCAL_BATCH_PER_RUN = 20000
SCORESTORE_RELEASES_BATCH_PER_RUN = 100  # labels traités par lancement — repli si
# scoring.scorestore.releases_batch_per_run absent. Correctif famine scorestore du
# 24/09 (cf. CLAUDE.md) : un utilisateur avec beaucoup de labels suivis (ex. 561)
# monopolisait le worker ~17-20 min d'affilée en scorant tous ses labels d'un coup,
# empêchant scan_recos/publish_recos de tourner à l'heure. Voir job_scorestore_releases
# pour la reprise par rotation (label_cursor persisté dans scorestore_meta).


def _chain_scorestore_tracks():
    """Enfile scorestore_tracks après scorestore_releases, que le scan release
    ait bougé quelque chose ou non : une passe piste par piste laissée en plan
    par un précédent quota API épuisé doit continuer à progresser aux scans
    suivants.

    priority=0 explicite (correctif famine scorestore du 24/09) : sans lui,
    ce chaînage héritait du défaut interactif de jobs.launch() et doublait à
    tort les scorestore_releases des AUTRES utilisateurs déjà en file,
    cassant le round-robin de worker._pick()."""
    from radar_web.radar import jobs as job_queue
    if not any(j["name"] == "scorestore_tracks" and j["uid"] == RADAR_UID for j in job_queue.load_queue()):
        job_queue.launch("scorestore_tracks", {}, uid=RADAR_UID, priority=0)


def _stamp_scorestore_run(con, cfg):
    """Estampille la base de scores avec l'état des entrées AU MOMENT du calcul.

    Sans cette trace, rien ne distingue des scores produits par la méthode
    courante de scores hérités d'une configuration ou d'un graphe antérieurs :
    la page de fraîcheur de `radar_ops` n'aurait aucune base pour répondre.

    Ne fait jamais échouer le job : le précalcul est la mission, l'estampille
    n'en est qu'un effet de bord utile au diagnostic."""
    from radar_web.radar import codeversion, scorestore
    from radar_web.radar import scoring as sc
    try:
        n = con.execute("SELECT COUNT(*) FROM release_scores").fetchone()[0]
        scorestore.set_run_meta(con, {
            "fingerprint": sc.derived_fingerprint(RADAR_UID, cfg),
            "detail": json.dumps(sc.derived_key_detail(RADAR_UID, cfg), sort_keys=True),
            "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "code_sha": codeversion.sha() or "",
            "rows": n,
        })
    except Exception as e:
        print(f"[scorestore] estampille du run impossible : {e}", file=sys.stderr)


def _chain_scorestore_releases():
    """Enfile la suite de job_scorestore_releases quand le tour de labels a
    été tronqué par SCORESTORE_RELEASES_BATCH_PER_RUN (reprise via
    label_cursor persisté dans scorestore_meta), sans attendre le prochain
    déclenchement à cadence fixe de worker._maybe_scorestore_build.

    priority=0 explicite, même raison que _chain_scorestore_tracks ci-dessus."""
    from radar_web.radar import jobs as job_queue
    if not any(j["name"] == "scorestore_releases" and j["uid"] == RADAR_UID for j in job_queue.load_queue()):
        job_queue.launch("scorestore_releases", {}, uid=RADAR_UID, priority=0)


def job_scorestore_releases(job, params):
    """Lot 4 de la refonte scoring (cf. CLAUDE.md point 37) : précalcule le
    score de chaque sortie des labels suivis (Cœur + Aimé) dans
    radar/scorestore.py (fichier SQLite par utilisateur, séparé du
    référentiel Discogs partagé — cf. docstring du module : ces données sont
    par utilisateur, pas remplacées en bloc comme le dump mensuel). Même
    formule que la Recherche/RECOS RADAR (Ctx.album_score, pas une nouvelle
    règle), mais ici SANS filtre de seuil ni limite de nouveautés : toutes
    les sorties des labels suivis sont notées, pas seulement les mieux
    placées ni seulement les jamais-vues — objectif d'infrastructure (Lot 5
    lira ces tables), pas une file de candidats à consommer une fois.

    Calcul purement local (aucun appel réseau) : contrairement à RECOS,
    chaque lancement REFAIT le score de toutes les sorties suivies plutôt que
    de sauter celles déjà vues, pour rester automatiquement à jour après un
    changement de goût sans bookkeeping de fraîcheur séparé.

    Traite les labels suivis PAR LOT DE SCORESTORE_RELEASES_BATCH_PER_RUN
    (correctif famine scorestore du 24/09, cf. CLAUDE.md) : l'hypothèse
    d'origine (« le volume reste assez petit pour que ce soit bon marché »)
    ne tenait plus à l'échelle d'un utilisateur avec des centaines de labels
    suivis — un lancement scorait alors tout d'un coup et monopolisait le
    worker minute après minute. La reprise se fait par rotation sur
    label_keys (triée, donc stable d'un lancement à l'autre), avec un
    curseur persisté dans scorestore_meta (`label_cursor`, via
    scorestore.set_run_meta/read_run_meta, déjà utilisé pour l'estampille de
    fraîcheur) : chaque lot reprend exactement où le précédent s'est arrêté,
    jamais depuis le début.

    Tous formats depuis le 15/09 (`search_local(vinyl_only=False)`, décision
    utilisateur : « exhaustif à la découverte, vinyle à l'achat ») — le
    correctif vinyle du 14/09 (issue #62) avait mis le filtre à la racine de
    `search_local`, ce qui excluait à tort les sorties CD/digital de la
    découverte RECOS elle-même. Le filtre vinyle se fait désormais seulement
    à l'ajout wantlist (`app.py::cart_add`), jamais ici : `release_scores`/
    `track_scores` grossissent en conséquence (~4x, cf. CLAUDE.md).

    Chaîne job_scorestore_tracks (comme job_scan_recos chaîne
    job_publish_recos) SEULEMENT quand le tour de labels est complet — un lot
    tronqué se rechaîne lui-même à la place (_chain_scorestore_releases) pour
    continuer le tour, sans relancer scorestore_tracks à chaque lot."""
    from radar_web.radar import discogs_dump as dd
    from radar_web.radar import scorestore
    from radar_web.radar.scoring import Ctx

    cfg = cfg_load()
    if not dd.available():
        return job.finish(error="Référentiel Discogs local indisponible (dump pas encore importé).")

    lcats = cfg.get("label_categories", {})
    names = [n for cid in ("1", "2") for n in lcats.get(cid, [])]
    label_keys = sorted({normalize_label(n) for n in names if n and n.strip()})

    con = scorestore.open_db(RADAR_UID)
    try:
        scorestore.prune_labels(con, label_keys)
        if not label_keys:
            _chain_scorestore_tracks()
            return job.finish("Aucun label suivi (Cœur ou Aimé) — rien à noter.")

        # Reprise par rotation (cf. docstring) : un lancement ne traite qu'un
        # lot de labels, jamais l'intégralité. Le curseur persisté est CUMULATIF
        # (jamais remis à zéro en stockage, seulement à l'INDEXAGE via `% n_labels`)
        # — un label ajouté/retiré entre deux tours ne fait donc jamais planter la
        # reprise, juste légèrement dériver le point de départ. Un tour est jugé
        # complet dès que ce lot fait franchir un multiple de n_labels au curseur
        # cumulé (`next_cursor // n_labels > cursor // n_labels`) : ça couvre aussi
        # bien le cas courant (lot >= tous les labels suivis, tour complet à chaque
        # lancement, comme avant ce correctif) que le cas d'un total non multiple du
        # lot (le dernier lot d'un tour peut re-couvrir un label déjà vu ce tour-ci,
        # mais n'en saute jamais un).
        sc_cfg = cfg.get("scoring", {}).get("scorestore", {})
        batch_limit = int(params.get("releases_batch_per_run",
                                      sc_cfg.get("releases_batch_per_run",
                                                 SCORESTORE_RELEASES_BATCH_PER_RUN)))
        n_labels = len(label_keys)
        meta = scorestore.read_run_meta(con)
        try:
            cursor = int(meta.get("label_cursor", 0))
        except (TypeError, ValueError):
            cursor = 0
        start = cursor % n_labels
        batch_keys = (label_keys[start:] + label_keys[:start])[:batch_limit]
        next_cursor = cursor + len(batch_keys)
        sweep_complete = next_cursor // n_labels > cursor // n_labels
        truncated = not sweep_complete

        rows, row_ids = [], set()
        for lk in batch_keys:
            # découverte exhaustive tous formats (décision utilisateur 2026-09-15,
            # cf. CLAUDE.md) : le filtre vinyle ne s'applique plus ici, seulement à
            # l'ajout wantlist (app.py::cart_add) — voir docstring de search_local.
            for row in dd.search_local(label_keys=[lk], limit=SCORESTORE_PER_LABEL_LIMIT, vinyl_only=False):
                if row["id"] not in row_ids:
                    row_ids.add(row["id"])
                    rows.append(row)

        ctx = Ctx(uid=RADAR_UID)
        job.st["total"] = len(rows)
        n_scored = 0
        interrompu = False
        for i, row in enumerate(rows, 1):
            if job.stopped():
                interrompu = True
                break
            title = f"{row['artist']} - {row['title']}" if row.get("artist") else (row.get("title") or "")
            styles = row["styles"].split(", ") if row.get("styles") else []
            score, detail = ctx.album_score({
                "label": [row["label"]] if row.get("label") else [], "title": title, "style": styles})
            scorestore.upsert_release_score(con, {
                "release_id": row["id"], "label_key": normalize_label(row.get("label") or ""),
                "label": row.get("label"), "artist": row.get("artist"), "title": row.get("title"),
                "year": row.get("year"), "styles": row.get("styles"), "score": score, "detail": detail})
            if score is not None:
                n_scored += 1
            job.tick(f"{row.get('artist')} — {row.get('title')} : {score if score is not None else '—'}")
            if i % 200 == 0:
                con.commit()
        con.commit()
        # Le curseur avance quel que soit le sort du lot (interrompu ou non) : la
        # DÉCOUVERTE (coûteuse, cf. docstring) a déjà eu lieu pour tout le lot avant
        # ce point. Un curseur figé sur interruption referait scorer deux fois de
        # suite le même lot ; un curseur qui avance revoit au pire un label une fois
        # de trop au tour suivant, jamais deux fois d'affilée par accident.
        scorestore.set_run_meta(con, {"label_cursor": str(next_cursor)})
        # Estampiller un tour incomplet (tronqué OU interrompu) ferait dire « à jour »
        # à radar_ops alors que seule une fraction des labels suivis vient d'être
        # recalculée — le pire des deux mondes, des scores périmés qu'on ne sait
        # plus reconnaître comme tels.
        if not interrompu and sweep_complete:
            _stamp_scorestore_run(con, cfg)
        stats = scorestore.stats(con)
    finally:
        con.close()
    if truncated:
        _chain_scorestore_releases()
    else:
        _chain_scorestore_tracks()
    job.finish(f"{n_scored} sortie(s) notée(s) sur {len(rows)} scannée(s), "
               f"{len(batch_keys)}/{n_labels} label(s) suivi(s) traité(s) ce lancement"
               f" — {stats['n_releases']} au total en base."
               + (" Passe INTERROMPUE : la base n'est pas estampillée, relancer le job."
                  if interrompu else "")
               + (" Lot partiel : reprise automatique au prochain lancement."
                  if truncated and not interrompu else ""))


def job_scorestore_tracks(job, params):
    """Passe piste par piste de radar/scorestore.py (Lot 4, suite de
    job_scorestore_releases) : deux étapes indépendantes dans le même job —

    (a) rafraîchit GRATUITEMENT (recalcul local, sans appel réseau) le score
    de toute piste déjà connue, pour rester à jour après un changement de
    goût sans jamais redemander sa tracklist à l'API — une sortie tombée
    sous le seuil depuis garde ses pistes déjà récupérées à jour (cf.
    scorestore.releases_with_tracks) ;

    (b) récupère la tracklist des sorties les mieux notées qui n'ont ENCORE
    aucune piste connue — depuis le 15/09 (diagnostic VPS, cf. CLAUDE.md), en
    priorité via `discogs_dump.tracks_for_release()` : le dump local CONTIENT
    bien les tracklists (seul le schéma d'origine ne les exploitait pas),
    donc AUCUN appel réseau pour toute sortie présente dans le dernier import
    mensuel — plafonné à `SCORESTORE_LOCAL_BATCH_PER_RUN` par lancement (pas
    de coût réseau, juste CPU/écriture, un lot bien plus large que l'ancien
    budget API). L'API Discogs ne sert plus que de repli pour une sortie
    ABSENTE du dump (ajoutée sur Discogs depuis le dernier import), plafonné
    à `scoring.scorestore.fetches_per_run` appels par lancement (même
    principe de budget que RECOS_SEARCHES_PER_RUN — coût réseau/rate-limit
    Discogs, pas juste de la CPU).

    Stocke aussi le détail `{label, artist, style}` de `Ctx.album_score` par
    PISTE (Lot 5, cf. CLAUDE.md point 43 — `job_scan_recos` en a besoin pour
    le feedback RECOS RADAR), pas seulement le score.

    La passe (a) normalise aussi l'artiste des pistes déjà stockées
    (`_is_placeholder_artist`) : les lignes polluées par "Various"/"Unknown
    Artist" avant le correctif du 15/09 se nettoient donc seules, sans rappel
    réseau — coût nul, cf. diagnostic VPS 2026-09-15. La passe (b) ne stocke
    plus les megamix/DJ mix continus (`_is_continuous_mix`) : rien à faire
    dans RECOS RADAR."""
    from radar_web.radar import discogs_dump as dd
    from radar_web.radar import scorestore
    from radar_web.radar.scoring import Ctx, real_tracks

    if not scorestore.available(RADAR_UID):
        return job.finish("Rien à noter (lance d'abord scorestore_releases).")

    cfg = cfg_load()
    token = cfg.get("token", "")
    sc = cfg.get("scoring", {}).get("scorestore", {})
    min_score = float(params.get("min_score", sc.get("min_score", 10)))
    fetches_per_run = int(params.get("fetches_per_run", sc.get("fetches_per_run", SCORESTORE_FETCHES_PER_RUN)))

    con = scorestore.open_db(RADAR_UID)
    try:
        ctx = Ctx(uid=RADAR_UID)

        # (a) rafraîchissement gratuit des pistes déjà connues, quel que soit
        # leur score courant (cf. docstring de releases_with_tracks).
        n_rescored = 0
        for r in scorestore.releases_with_tracks(con):
            styles = r["styles"].split(", ") if r.get("styles") else []
            tracks = scorestore.tracks_for_release(con, r["release_id"])
            updated = []
            for t in tracks:
                art = "" if _is_placeholder_artist(t.get("artist")) else (t.get("artist") or "").strip()
                title = f"{art} - {t['title']}" if art else (t.get("title") or "")
                score, detail = ctx.album_score({"label": [r["label"]] if r.get("label") else [],
                                                  "title": title, "style": styles})
                updated.append({**t, "artist": art, "score": score, "detail": detail})
            scorestore.upsert_track_scores(con, r["release_id"], updated)
            n_rescored += len(updated)
        con.commit()

        tracks_ready = dd.tracks_available()
        if not token and not tracks_ready:
            job.finish(f"{n_rescored} piste(s) rafraîchie(s) — pas de token Discogs ni de "
                       "tracklists locales, récupération de nouvelles tracklists sautée.")
            return

        # (b) tracklist des sorties top-scorées pas encore couvertes — dump local
        # d'abord (gratuit), API en repli seulement (cf. docstring).
        batch_limit = SCORESTORE_LOCAL_BATCH_PER_RUN if tracks_ready else fetches_per_run
        targets = scorestore.top_releases(
            con, min_score=min_score, without_tracks_only=True, limit=batch_limit)
        job.st["total"] = len(targets)
        n_new_releases, n_new_tracks, n_local, n_api = 0, 0, 0, 0
        main_con = dd.connect_readonly() if tracks_ready else None
        tracks_con = dd.connect_tracks_readonly() if tracks_ready else None
        try:
            for row in targets:
                if job.stopped():
                    break
                dump_tracks = (dd.tracks_for_release(row["release_id"], con=tracks_con, main_con=main_con)
                               if tracks_ready else None)
                if dump_tracks is not None:
                    tracks = real_tracks(dump_tracks)
                    n_local += 1
                elif token and n_api < fetches_per_run:
                    d = discogs_get(token, f"/releases/{row['release_id']}")
                    tracks = real_tracks(d.get("tracklist", [])) if d else []
                    n_api += 1
                    time.sleep(1.1)
                else:
                    # hors dump ET (pas de token OU budget API épuisé pour ce lancement) :
                    # laissé de côté, retenté au prochain passage (jamais marqué "vu").
                    job.tick(f"{row.get('artist')} — {row.get('title')} : hors dump local, "
                             f"repli API {'indisponible (pas de token)' if not token else 'plafonné pour ce lancement'}.")
                    continue
                styles = row["styles"].split(", ") if row.get("styles") else []
                scored_tracks = []
                for i, t in enumerate(tracks):
                    ttl = (t.get("title") or "").strip()
                    if not ttl or _is_continuous_mix(ttl):
                        continue
                    art = _track_credit_artist(t, row.get("artist") or "")
                    title = f"{art} - {ttl}" if art else ttl
                    score, detail = ctx.album_score({"label": [row["label"]] if row.get("label") else [],
                                                      "title": title, "style": styles})
                    scored_tracks.append({"track_no": i, "artist": art, "title": ttl,
                                           "score": score, "detail": detail})
                if scored_tracks:
                    scorestore.upsert_track_scores(con, row["release_id"], scored_tracks)
                    n_new_releases += 1
                    n_new_tracks += len(scored_tracks)
                job.tick(f"{row.get('artist')} — {row.get('title')} : +{len(scored_tracks)} piste(s)")
                con.commit()
        finally:
            if main_con is not None:
                main_con.close()
            if tracks_con is not None:
                tracks_con.close()
    finally:
        con.close()
    # Lot plein (dump local) ou budget API épuisé : d'autres sorties sans tracklist
    # attendent probablement encore — se rechaîne soi-même pour tourner en continu
    # plutôt que d'attendre le prochain passage à cadence fixe de
    # worker._maybe_scorestore_build.
    if len(targets) >= batch_limit or n_api >= fetches_per_run:
        _chain_scorestore_tracks()
    job.finish(f"{n_rescored} piste(s) rafraîchie(s) — +{n_new_tracks} nouvelle(s) piste(s) sur "
               f"{n_new_releases} sortie(s) ({n_local} via le dump local, {n_api} via l'API).")


def job_reco_index(job, params):
    """Reconstruit le cache disque de `Ctx.reco_index` (radar_web/radar/recoindex.py)
    — couche 3 du chantier M1, brief VPS du 18/09.

    Le calcul complet met 183,5 s à froid sur les données réelles (465 284 labels
    classés) et il était payé par la PREMIÈRE recherche suivant chaque redémarrage
    du conteneur. Ce job le sort du chemin HTTP : le worker l'enfile quand la
    signature des données change, les requêtes ne font plus que relire le fichier.

    Ne recalcule rien si le cache est déjà à jour, sauf `force=1` — le worker
    l'enfile sur une simple cadence, pas sur un événement précis.

    Le cache reste un raccourci : `Ctx._compute_reco_index` retombe sur le calcul
    en ligne s'il est absent ou périmé. Ce job peut donc échouer ou ne jamais
    tourner sans rien casser, seulement ralentir."""
    from radar_web.radar import recoindex
    from radar_web.radar.scoring import Ctx
    ctx = Ctx(uid=RADAR_UID)
    if not params.get("force") and ctx.reco_index_is_fresh():
        meta = recoindex.read_meta(RADAR_UID) or {}
        return job.finish(f"Cache déjà à jour ({meta.get('n', 0)} labels, "
                          f"construit le {meta.get('built_at', '?')}).")
    job.tick("calcul de l'index de reco (aucun appel réseau)")
    meta = ctx.save_reco_index()
    job.finish(f"{meta['n']} label(s) en cache — la recherche n'a plus à recalculer "
               f"l'index tant que le goût, la collection, le corpus ou le référentiel "
               f"ne changent pas.")
