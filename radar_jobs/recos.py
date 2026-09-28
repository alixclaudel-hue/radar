"""Playlist RECOS RADAR : `scan_recos` (candidats) et `publish_recos` (YouTube),
avec le budget de recherches quotidien et l'historique des pistes publiées.
"""

import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from radar_web.radar import textmatch, ytcache
from radar_jobs.common import (
    cfg_load, COLLECTION_CACHE_PATH, CORPUS_PATH, JOBS_USER_DIR, load_json, RADAR_UID,
    RECOS_CANDIDATES_PATH, RECOS_HISTORY_PATH, RECOS_PLAYLIST_PATH, RECOS_SEARCH_BUDGET_PATH,
    save_json, style_key,
)
from radar_jobs.sources import discogs_get
from radar_jobs.tracks import _is_continuous_mix, _release_identity_key, _strip_discogs_suffix


# Le quota gratuit YouTube Data API se remet à zéro à MINUIT HEURE DU PACIFIQUE
# (9h à Paris), pas à minuit à Paris : compter les recherches sur l'horloge de
# Paris bloquait le budget pendant ~9h chaque jour alors que le vrai quota était
# déjà reparti (constaté en prod le 16/09). Horloge distincte de celle de la
# purge des pistes écoutées (worker.py), qui elle reste sur Paris.
YT_QUOTA_TZ = ZoneInfo("America/Los_Angeles")
RECOS_MAX_TRACKS = 5  # repli si scoring.recos.max_tracks absent (curseur /settings, 10/09)
RECOS_DAILY_SEARCH_BUDGET = 80
# marge sous le quota gratuit YouTube Data API : évite que scan_recos empile plus
# de candidats que publish_recos ne peut en chercher sur YouTube en une journée
# (retour utilisateur du 09/09 : file à 417 candidats, quota épuisé dès la 1re
# publication). 90 supposait 100 unités par candidat, or une piste qui ne trouve
# rien en coûte 202 (recherche avec label, puis sans, plus /videos) : la file
# entière brûlait le quota du jour et les candidats suivants tombaient en
# « aucune vidéo trouvée » alors que le quota, pas la piste, était en cause.
# 80 n'est atteignable en entier qu'avec plusieurs clés : au pire des cas (80
# échecs à 202 unités) il faudrait ~16 160 unités, soit plus que les 10 000/jour
# d'une clé unique — le plafond effectif reste alors le quota Google.
RECOS_MAX_ATTEMPTS = 3
# Plafonne les sorties interrogées chez Discogs par lancement pour retrouver une
# vidéo déjà attachée à la sortie (cf. `_discogs_release_video`). Ressource
# différente du budget YouTube : Discogs limite au DÉBIT (60 requêtes/minute,
# d'où le sleep de 1,1 s), pas à la journée. Le plafond borne surtout la DURÉE du
# run, qui sinon tiendrait ~90 s rien qu'en attente sur une file pleine.
RECOS_DISCOGS_LOOKUPS_PER_RUN = 40
# Plus de plafond « recherches YouTube par lancement » distinct (ancien
# scoring.recos.searches_per_run, supprimé le 2026-09-28) : le BUDGET QUOTIDIEN
# ci-dessus est la seule limite de recherches d'un run, pour qu'il soit consommé
# en totalité chaque jour (demande utilisateur). Le plafond par lancement
# laissait du budget inutilisé les jours où peu de runs tournaient, et faisait
# doublon approximatif avec le compteur journalier.


def _recos_history_load():
    """{video_id: {"video_id","artist","title",...}} depuis recos_history.json —
    fichier permanent (jamais purgé, même quand une piste sort de la playlist par
    FIFO ou suppression manuelle) qui sert de mémoire de TOUT ce qui a déjà été
    ajouté un jour, pas seulement ce qui y est encore (retour utilisateur
    2026-09-10). Anciennes entrées (avant ce correctif) : simples chaînes
    video_id sans artiste/titre, gardées telles quelles (dédoublonnage par
    vidéo seulement pour elles, pas par identité de piste)."""
    out = {}
    for h in load_json(RECOS_HISTORY_PATH, []):
        if isinstance(h, str):
            out[h] = {"video_id": h, "artist": None, "title": None}
        elif isinstance(h, dict) and h.get("video_id"):
            out[h["video_id"]] = h
    return out


def _recos_history_track_keys(history):
    """{(style_key(artist), style_key(title))} — identité de piste, pour repérer
    une piste déjà publiée par le passé même si un futur match YouTube tombe sur
    un video_id différent (ré-upload, autre chaîne) que le dédoublonnage par
    video_id seul laisserait passer."""
    return {(style_key(h["artist"]), style_key(h["title"]))
            for h in history.values() if h.get("artist") and h.get("title")}


def _recos_searches_used_today():
    """Recherches YouTube RECOS déjà consommées aujourd'hui, sur l'horloge de
    remise à zéro du quota Google (minuit heure du Pacifique, 9h à Paris —
    cf. YT_QUOTA_TZ). Sans ce compteur PERSISTANT, RECOS_DAILY_SEARCH_BUDGET ne bornait
    que la longueur de recos_candidates.json (job_scan_recos), jamais le
    nombre réel de recherches/jour — la boucle horaire du worker
    (RADAR_RECOS_SCAN=1) pouvait épuiser le quota YouTube dès la matinée
    (diagnostic VPS 2026-09-15). Lu ici (job_publish_recos) plutôt que dans le
    worker pour valoir aussi sur un lancement manuel (bouton ▶)."""
    today = datetime.now(YT_QUOTA_TZ).date().isoformat()
    d = load_json(RECOS_SEARCH_BUDGET_PATH, {})
    return int(d.get("count", 0)) if d.get("date") == today else 0


def _recos_searches_record(n):
    """Ajoute `n` recherches au compteur du jour (reparti de 0 si le fichier
    date d'un jour différent, toujours en heure du Pacifique)."""
    if not n:
        return
    today = datetime.now(YT_QUOTA_TZ).date().isoformat()
    d = load_json(RECOS_SEARCH_BUDGET_PATH, {})
    count = int(d.get("count", 0)) if d.get("date") == today else 0
    save_json(RECOS_SEARCH_BUDGET_PATH, {"date": today, "count": count + n})


def _owned_releases():
    """(ids, clés d'identité) des sorties déjà possédées : collection Discogs
    (collection_cache.json, job_fetch_collection) + achats Bandcamp (corpus,
    job_ingest_bandcamp — retour utilisateur issue #62, 20/09 : ne pas
    recommander non plus une piste déjà dans la collection Bandcamp). Vide
    tant que la synchro correspondante n'a jamais tourné : le filtre est
    alors sans effet, jamais bloquant."""
    coll = load_json(COLLECTION_CACHE_PATH, {})
    ids = {int(x) for x in coll.get("owned_release_ids", []) if str(x).isdigit()}
    keys = set(coll.get("owned_release_keys", []))
    for r in load_json(CORPUS_PATH, []):
        if r.get("source") != "bandcamp":
            continue
        if str(r.get("release_id") or "").isdigit():
            ids.add(int(r["release_id"]))
        k = _release_identity_key(r.get("artist"), r.get("title"))
        if k:
            keys.add(k)
    return ids, keys


def job_scan_recos(job, params):
    """Candidats pour la playlist RECOS RADAR — refondu au Lot 5 de la refonte
    scoring (cf. CLAUDE.md point 43) : lit directement radar/scorestore.py
    (`track_scores` JOIN `release_scores`) au lieu de rescanner le référentiel
    Discogs et de renoter les sorties lui-même. Ce travail est déjà fait en
    continu, en amont, par job_scorestore_releases/job_scorestore_tracks
    (Lot 4) — ce job n'est plus qu'une SÉLECTION dans ce qui est déjà calculé.

    Changement de fond (demande utilisateur) : chaque piste candidate porte
    désormais SON PROPRE score (`Ctx.album_score` calculé par artiste+titre
    RÉEL de la piste, cf. job_scorestore_tracks) — avant ce lot, toutes les
    pistes d'une même sortie héritaient du même score de RELEASE (calculé une
    fois sur le titre de la sortie), moins précis pour une compilation ou un
    featuring où les pistes n'ont pas toutes le même artiste. Conséquence :
    plus aucun appel réseau ici (la tracklist est déjà récupérée par
    job_scorestore_tracks) — ni token Discogs ni référentiel dump requis pour
    CE job précis, juste que scorestore ait déjà tourné au moins une fois.

    `recos_seen.json` n'est plus utilisé (fichier laissé tel quel sur disque,
    inoffensif) : lire scorestore est local et bon marché, chaque scan peut
    donc relire tout `track_scores` sans distinguer "sortie déjà vue" — le
    dédoublonnage par IDENTITÉ DE PISTE (candidats + playlist + historique,
    inchangé, cf. `_recos_history_track_keys`) suffit à ne jamais ajouter deux
    fois la même piste.

    `force` : vide `recos_candidates.json` avant de reconstruire la file
    depuis `track_scores`, utile si des candidats en attente portent un score
    devenu obsolète après un changement de pondération (`track_scores`, lui,
    reste à jour tout seul via la passe de rafraîchissement gratuite de
    job_scorestore_tracks). Ne touche ni recos_playlist.json (déjà publié) ni
    recos_history.json (vidéos déjà proposées, jamais réajoutées).

    Écarte aussi les pistes des albums DÉJÀ POSSÉDÉS (collection Discogs ET
    achats Bandcamp, cf. `_owned_releases` — retour utilisateur 2026-09-17 :
    toutes les pistes de « Asakusa Light » proposées alors que l'album est en
    collection ; élargi à Bandcamp le 20/09), par release_id ET par identité
    artiste+titre (un autre pressage du même disque porte un id différent).

    Écarte (diagnostic VPS 2026-09-15, décision utilisateur) les pistes sans
    artiste exploitable (`TRIM(ts.artist) <> ''`, cf. `_track_credit_artist`) —
    pas de recherche YouTube au titre seul, `_best_match` ne sait pas trancher
    sans artiste — et les megamix/DJ mix continus (`_is_continuous_mix`), déjà
    présents en base avant que job_scorestore_tracks ne les filtre à
    l'écriture."""
    from radar_web.radar import scorestore

    cfg = cfg_load()
    rc = cfg.get("scoring", {}).get("recos", {})
    min_score = float(params.get("min_score", rc.get("min_score", 60)))
    max_new = int(params.get("max_new_releases", rc.get("max_new_releases", 20)))
    force = bool(params.get("force"))

    if not scorestore.available(RADAR_UID):
        return job.finish(error="Aucun score précalculé disponible — lance d'abord "
                                 "scorestore_releases (ou active RADAR_SCORESTORE=1 sur le worker).")

    con = scorestore.connect_readonly(RADAR_UID)
    try:
        rows = con.execute(
            "SELECT ts.artist, ts.title, ts.score, ts.detail_json, "
            "ts.release_id, rs.title, rs.label, rs.year, rs.artist "
            "FROM track_scores ts JOIN release_scores rs ON rs.release_id = ts.release_id "
            "WHERE ts.score >= ? AND TRIM(ts.artist) <> '' "
            "ORDER BY ts.score DESC", (min_score,)).fetchall()
    finally:
        con.close()
    if not rows:
        return job.finish(f"Aucune piste précalculée au-dessus de {min_score:g} pour l'instant "
                           f"— laisse scorestore_tracks récupérer la tracklist des sorties les "
                           f"mieux notées (job séparé, cf. CLAUDE.md point 42).")

    candidates = [] if force else load_json(RECOS_CANDIDATES_PATH, [])
    if len(candidates) >= RECOS_DAILY_SEARCH_BUDGET:
        last = _publish_recos_last_message()
        cause = f" Dernière publication : {last}" if last else ""
        return job.finish(f"File déjà pleine ({len(candidates)} en attente, quota YouTube ~"
                           f"{RECOS_DAILY_SEARCH_BUDGET} recherches/jour) — scan sauté, "
                           f"laisse la publication rattraper le retard.{cause}")
    # inclut aussi TOUT ce qui a déjà été publié un jour (recos_history.json,
    # jamais purgé), pas seulement la playlist ou la file d'attente courantes :
    # une piste retirée depuis (FIFO ou suppression manuelle) ne doit pas
    # revenir simplement parce qu'elle n'y est plus à l'instant T — retour
    # utilisateur 2026-09-10. La playlist courante reste incluse en plus (une
    # piste tout juste publiée, donc pas encore "vue" par le prochain scan).
    playlist = load_json(RECOS_PLAYLIST_PATH, [])
    known_tracks = ({(style_key(c.get("artist")), style_key(c.get("title")))
                     for c in candidates + playlist}
                    | _recos_history_track_keys(_recos_history_load()))
    owned_ids, owned_keys = _owned_releases()
    now = datetime.now().isoformat(timespec="seconds")
    job.st["total"] = min(len(rows), max_new)
    n_added, n_owned = 0, 0
    for (artist, title, score, detail_json, release_id, release_title, label, year,
         release_artist) in rows:
        if job.stopped() or n_added >= max_new or len(candidates) >= RECOS_DAILY_SEARCH_BUDGET:
            break
        title = (title or "").strip()
        k = (style_key(artist), style_key(title))
        if not title or k in known_tracks or _is_continuous_mix(title):
            continue
        if (release_id in owned_ids
                or _release_identity_key(release_artist, release_title) in owned_keys):
            n_owned += 1
            continue
        known_tracks.add(k)
        detail = json.loads(detail_json) if detail_json else {}
        candidates.append({
            "artist": artist, "title": title, "release_id": release_id,
            "release_title": release_title or "", "label": label, "year": year,
            "album_score": score, "added_at": now,
            "d_label": detail.get("label"), "d_artist": detail.get("artist"),
            "d_style": detail.get("style"),
        })
        n_added += 1
        job.tick(f"{artist} — {title} ({score})")
    save_json(RECOS_CANDIDATES_PATH, candidates)
    owned_note = f" {n_owned} piste(s) écartée(s) (album déjà en collection Discogs/Bandcamp)." if n_owned else ""
    job.finish(f"+{n_added} piste(s) candidate(s) sur {len(rows)} précalculée(s) — "
               f"file : {len(candidates)}.{owned_note}")


def _publish_recos_last_message():
    """Dernier message connu de publish_recos (ex. « Quota YouTube épuisé ») — affiché
    dans le message « file pleine » de scan_recos pour éviter à l'utilisateur d'avoir
    à ouvrir un second journal pour comprendre pourquoi la file ne se vide pas
    (retour utilisateur 2026-09-10 : « scan sauté » sans explication de la cause)."""
    s = load_json(os.path.join(JOBS_USER_DIR, "publish_recos.status.json"), {})
    return (s.get("message") or "").strip()


def _discogs_release_video(token, release_id, artist, title, keys):
    """Identifiant de la vidéo YouTube que Discogs attache DÉJÀ à cette sortie pour
    cette piste, ou "" si aucune ne correspond (ou si la sortie n'en a aucune).

    Même appariement que le bouton play de la tracklist de /search
    (`textmatch.best_video_uri`) : une sortie Discogs porte une liste de vidéos,
    rarement une par piste, souvent sans ordre ni position — seul le recoupement
    du titre permet de dire laquelle est la piste demandée.

    Deux raisons de préférer ce chemin à une recherche YouTube quand il aboutit :
    c'est la vidéo que Discogs associe à la sortie, pas le meilleur résultat d'une
    recherche textuelle ; et il ne consomme aucune unité du quota de RECHERCHE
    (~100 par recherche, 80 recherches/jour au mieux, cf. RECOS_DAILY_SEARCH_BUDGET)
    — seule la vérification de lisibilité coûte 1 unité sur `/videos`, métrique
    distincte qui reste servie même quand le quota de recherche du jour est épuisé
    (point 52 de CLAUDE.md).

    La vérification n'est pas optionnelle : un lien Discogs peut pointer vers une
    vidéo supprimée, privée ou bloquée, et la playlist ne doit contenir que du
    lisible. `QuotaExhausted`/`RateLimited`/`requests.RequestException` remontent
    à l'appelant, qui les traite comme pour une recherche."""
    d = discogs_get(token, f"/releases/{release_id}")
    time.sleep(1.1)                       # 60 requêtes/min côté Discogs
    # min_overlap=0 : ces vidéos sont déjà rattachées à LA bonne sortie, l'artiste
    # n'a donc rien à apporter au score global (diagnostic VPS 2026-09-18, cf.
    # textmatch.best_video_uri) — seule la couverture du titre de la piste compte.
    uri = textmatch.best_video_uri(d.get("videos") or [], artist, title, min_overlap=0)
    vid = ytcache.youtube_id(uri)
    return vid if vid and ytcache.playable_video(vid, keys) else ""


def job_publish_recos(job, params):
    """Recherche YouTube + ajout à la playlist RECOS RADAR (Fonctionnalité 1, lot 2) —
    consomme la file produite par job_scan_recos (recos_candidates.json). La playlist
    est interne à Radar (recos_playlist.json), pas une vraie playlist YouTube : rien à
    créer/gérer côté compte Google, la page /reco-radar lit ce fichier et lit les
    vidéos via l'API IFrame Player. Seul appel réseau ici : la recherche vidéo (lecture
    seule) via ytcache (cache partagé + cascade de clés, Option A étape 5a).

    Plafonnée à scoring.recos.max_tracks pistes (curseur /settings, repli
    RECOS_MAX_TRACKS) : une fois pleine, plus aucun ajout tant qu'une piste n'a
    pas été retirée — ni éviction automatique par ancienneté (FIFO, abandonnée
    au retour utilisateur 2026-09-14), ni suppression liée à une détection
    d'écoute réelle (l'ancien lot 3, scraping Playwright de l'historique
    YouTube, avait déjà été abandonné, cf. CLAUDE.md). Le renouvellement passe
    désormais par un clic explicite sur une piste (marquage "écoutée") purgé à
    minuit heure de Paris par le worker (cf. app.py::reco_radar_mark_played,
    worker.py::_maybe_recos_midnight_purge).

    Le BUDGET QUOTIDIEN RECOS_DAILY_SEARCH_BUDGET est la SEULE limite : un run
    cherche tant qu'il reste du budget du jour, tant que la playlist n'est pas
    pleine et tant qu'il reste des candidats (demande utilisateur 2026-09-28 :
    « le compteur doit être utilisé en totalité tous les jours »). Compté sur le
    nombre de RECHERCHES YouTube réellement tentées, pas sur les ajouts réussis,
    pour que la conso quota reste bornée même si la plupart des candidats
    échouent à matcher. Une vidéo trouvée chez Discogs ne fait AUCUNE recherche
    YouTube : elle ne touche donc pas ce compteur (cf. `_discogs_release_video`).

    Appliqué ICI (pas dans le worker) via
    `_recos_searches_used_today`/`_recos_searches_record` : persistant, il vaut
    aussi bien pour la boucle horaire (RADAR_RECOS_SCAN=1,
    cf. worker._maybe_recos_scan) que pour un lancement manuel (bouton ▶).

    Playlist PLEINE : on sort avant toute recherche — le budget du jour n'est
    pas gaspillé et aucune piste existante n'est retirée ni remplacée (demande
    utilisateur 2026-09-28)."""
    candidates = load_json(RECOS_CANDIDATES_PATH, [])
    if not candidates:
        return job.finish("Aucun candidat en attente.")

    budget_used = _recos_searches_used_today()
    playlist = load_json(RECOS_PLAYLIST_PATH, [])
    cfg = cfg_load()
    token = cfg.get("token", "")
    keys = ytcache.youtube_keys(cfg)
    rc = cfg.get("scoring", {}).get("recos", {})
    max_tracks = int(rc.get("max_tracks", RECOS_MAX_TRACKS))

    # Playlist PLEINE : sortie AVANT toute recherche (demande utilisateur
    # 2026-09-28). Aucune recherche n'est gaspillée pour rien, et surtout la
    # playlist existante n'est ni vidée ni remplacée (pas de FIFO/éviction : le
    # renouvellement passe uniquement par le marquage « écoutée » + purge minuit,
    # cf. worker._maybe_recos_midnight_purge).
    if len(playlist) >= max_tracks:
        return job.finish(
            f"Playlist pleine ({len(playlist)}/{max_tracks}) — aucune recherche "
            f"lancée ; plus d'ajout tant qu'une piste n'est pas marquée écoutée "
            f"(clic sur une ligne, purge à minuit heure de Paris).")

    if budget_used >= RECOS_DAILY_SEARCH_BUDGET and not token:
        # Sans token Discogs, le budget épuisé ne laisse rien à faire : les vidéos
        # attachées aux sorties (`_discogs_release_video`) sont le seul chemin qui
        # ne dépend pas du quota de RECHERCHE, et il demande ce token. Avec token,
        # le run continue : la boucle ci-dessous n'applique le budget qu'au repli
        # par recherche (coût assumé : jusqu'à RECOS_DISCOGS_LOOKUPS_PER_RUN appels
        # Discogs, cadencés à 1,1 s, par lancement).
        return job.finish(f"Budget quotidien de recherches YouTube atteint ({budget_used}/"
                           f"{RECOS_DAILY_SEARCH_BUDGET}) — reprendra à la remise à zéro du quota YouTube (9h à Paris).")

    # historique permanent (jamais purgé) : {video_id: entrée} + identité de piste
    # (artiste/titre) déjà publiée par le passé, même si elle n'est plus dans la
    # playlist (FIFO ou suppression manuelle) — retour utilisateur 2026-09-10.
    history = _recos_history_load()
    history_keys = _recos_history_track_keys(history)
    job.st["total"] = len(candidates)
    remaining, added, searched, quota_hit, rate_hit = [], 0, 0, False, False
    daily_hit = False
    discogs_lookups, from_discogs = 0, 0
    now = datetime.now().isoformat(timespec="seconds")
    try:
        for c in candidates:
            if job.stopped():
                remaining.append(c)
                continue
            if len(playlist) >= max_tracks:
                remaining.append(c)
                continue
            ck = (style_key(c.get("artist")), style_key(c.get("title")))
            if ck in history_keys:
                # identité déjà connue : aucune recherche réseau, ne compte pas dans le budget.
                job.tick(f"{c['artist']} — {c['title']} : déjà publiée par le passé")
                continue
            art_q = _strip_discogs_suffix(c.get("artist"))
            label_q = _strip_discogs_suffix(c.get("label") or "")
            vid, why, via_discogs = "", "", False
            try:
                # (1) Vidéo déjà attachée à la sortie chez Discogs — essayée AVANT la
                # recherche YouTube (demande utilisateur 2026-09-18) : plus fiable
                # qu'un résultat de recherche textuelle, et gratuite en quota de
                # recherche. Les garde-fous de quota ci-dessous ne s'appliquent donc
                # pas à ce chemin : une piste trouvée ici se publie même une fois le
                # budget du jour épuisé.
                if token and c.get("release_id") and discogs_lookups < RECOS_DISCOGS_LOOKUPS_PER_RUN:
                    discogs_lookups += 1
                    vid = _discogs_release_video(token, c["release_id"], art_q,
                                                 c.get("title"), keys)
                    via_discogs = bool(vid)

                # (2) Repli : recherche YouTube, sous budget.
                if not vid:
                    if (quota_hit or rate_hit
                            or budget_used + searched >= RECOS_DAILY_SEARCH_BUDGET):
                        if budget_used + searched >= RECOS_DAILY_SEARCH_BUDGET:
                            daily_hit = True
                        remaining.append(c)
                        continue
                    # force=True dès la 1re relance (attempts >= 1) : sinon la nouvelle
                    # tentative ne fait que relire le même échec en cache (NEG_TTL 6h,
                    # cf. ytcache.search_video_diag) au lieu de vraiment réinterroger
                    # l'API — les RECOS_MAX_ATTEMPTS tentatives ne cherchaient donc
                    # jamais rien de nouveau (retour utilisateur 2026-09-10).
                    vid, why = ytcache.search_video_diag(
                        f"{art_q} {c['title']}", keys,
                        artist=art_q, title=c.get("title"), label=label_q,
                        force=bool(c.get("attempts")))
                    searched += 1
            except ytcache.QuotaExhausted:
                # Ne décompte PAS `searched` (diagnostic VPS 16/09, BUG 2) : un
                # rejet 429/403 ne consomme aucune unité de quota Google — le
                # compter ici gonflait le budget quotidien d'une fausse unité à
                # chaque tick horaire tant que le quota restait épuisé.
                job.msg("Quota YouTube (recherche) épuisé — reprendra au prochain scan.")
                quota_hit = True
                remaining.append(c)
                continue
            except ytcache.RateLimited:
                # Transitoire (limite de débit par seconde, PAS le quota du jour,
                # diagnostic VPS 16/09 : un appel direct a réussi 20s plus tard sur
                # la même clé) : ne coûte pas d'unité de quota Google, donc ne
                # décompte pas `searched`. `rate_hit` arrête le run PROPREMENT
                # dès la 1re occurrence (BUG 3, diagnostic VPS 16/09) : sans lui,
                # chaque candidat suivant rejouait ses propres 4 tentatives +
                # 1+2+4s de backoff (ytcache._get) contre une clé déjà connue
                # limitée dans ce run, jusqu'à ~70s perdues sur 10 candidats,
                # sans jamais transformer ça en QuotaExhausted ni perdre la
                # journée.
                job.tick(f"{c['artist']} — {c['title']} : YouTube limite le débit "
                         f"— nouvelle tentative au prochain lancement.")
                rate_hit = True
                remaining.append(c)
                continue
            except requests.RequestException as e:
                # Aléa réseau (timeout, coupure) : le candidat n'a pas été
                # réellement tenté, on le remet en file sans compter de
                # recherche ni abandonner tout le run pour un aléa transitoire
                # (BUG 1, diagnostic VPS 16/09 — auparavant non rattrapé du
                # tout, remontait jusqu'à main() et perdait aussi l'état des
                # candidats déjà traités dans ce run, cf. `finally` ci-dessous).
                job.tick(f"{c['artist']} — {c['title']} : erreur réseau ({e}) "
                         f"— nouvelle tentative au prochain lancement.")
                remaining.append(c)
                continue
            if not vid:
                # BUG trouvé le 10/09 (retour utilisateur : "aucune vidéo trouvée"
                # systématique malgré le correctif scoring de 9f1eb04) : ce `continue`
                # ne remettait PAS `c` dans `remaining` — le candidat était détruit dès
                # le 1er échec, alors que sa sortie reste marquée "vue" dans
                # recos_seen.json (job_scan_recos) et ne sera donc jamais re-proposée.
                # Résultat : chaque échec de recherche perdait la piste pour toujours,
                # jusqu'à vider la file sans jamais rien publier. On retente désormais
                # RECOS_MAX_ATTEMPTS fois (le cache négatif NEG_TTL de ytcache expire
                # sous 6h, laissant une chance à une meilleure indexation YouTube ou un
                # changement de scoring) avant d'abandonner explicitement.
                c["attempts"] = c.get("attempts", 0) + 1
                if c["attempts"] < RECOS_MAX_ATTEMPTS:
                    job.tick(f"{c['artist']} — {c['title']} : aucune vidéo trouvée ({why}) "
                             f"— nouvelle tentative au prochain lancement "
                             f"({c['attempts']}/{RECOS_MAX_ATTEMPTS}).")
                    remaining.append(c)
                else:
                    job.tick(f"{c['artist']} — {c['title']} : aucune vidéo trouvée ({why}) "
                             f"— abandonnée après {RECOS_MAX_ATTEMPTS} tentatives.")
                continue
            if vid in history:
                job.tick(f"{c['artist']} — {c['title']} : déjà ajoutée un jour")
                continue
            playlist.append({**c, "video_id": vid, "published_at": now})
            history[vid] = {"video_id": vid, "artist": c.get("artist"), "title": c.get("title")}
            history_keys.add(ck)
            added += 1
            from_discogs += int(via_discogs)
            job.tick(f"{c['artist']} — {c['title']} : ajoutée "
                     f"({'vidéo Discogs' if via_discogs else 'recherche YouTube'})")
            save_json(RECOS_HISTORY_PATH, list(history.values()))
            save_json(RECOS_PLAYLIST_PATH, playlist)
    finally:
        # `finally`, pas juste en fin de fonction (BUG 1, diagnostic VPS 16/09) :
        # une exception non rattrapée ci-dessus (ex. RuntimeError d'une erreur
        # API non classée, cf. ytcache.request) sortait auparavant de la
        # fonction SANS jamais exécuter ces 4 lignes — les candidats déjà
        # `attempts`-incrémentés dans ce run et les recherches déjà parties
        # (jusqu'au budget quotidien, ~8000 unités de quota Google au pire)
        # étaient perdus du fichier de budget, qui existe précisément pour
        # protéger le quota. `remaining` ne contient pas le candidat en cours au moment
        # de l'exception (il n'a pas pu être classé) : perte limitée à 1
        # candidat sur un aléa réellement anormal, pas au run entier.
        save_json(RECOS_HISTORY_PATH, list(history.values()))
        save_json(RECOS_PLAYLIST_PATH, playlist)
        save_json(RECOS_CANDIDATES_PATH, remaining)
        _recos_searches_record(searched)
    note = ""
    if daily_hit:
        note += (f" Budget quotidien atteint ({budget_used + searched}/"
                 f"{RECOS_DAILY_SEARCH_BUDGET}) — reprendra à la remise à zéro du quota YouTube (9h à Paris).")
    if rate_hit:
        note += " YouTube limite le débit — reprendra au prochain lancement."
    if len(playlist) >= max_tracks and remaining:
        note += (f" Playlist pleine ({max_tracks}) — plus d'ajout tant qu'aucune "
                 f"piste n'est marquée écoutée (clic sur une ligne, purge à minuit).")
    origin = (f" {from_discogs} via une vidéo déjà attachée à la sortie chez Discogs "
              f"(0 recherche YouTube), {added - from_discogs} par recherche." if added else "")
    job.finish(f"+{added} piste(s) ajoutée(s) — playlist : {len(playlist)}/{max_tracks}, "
               f"{len(remaining)} en attente.{origin}{note}")
