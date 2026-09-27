"""Jobs d'ingestion de l'écoute et du profil : YouTube, Spotify, Bandcamp,
collection Discogs, fusion du corpus, profilage des labels.
"""

import re
import time
from collections import Counter
from datetime import datetime

from radar_web.radar import ytcache
from radar_jobs.common import (
    cfg_load, COLLECTION_CACHE_PATH, CONFIG_PATH, CORPUS_PATH, load_json, LOOKUP_CACHE_PATH,
    normalize_label, PROFILE_PATH, RESOLVED_PATH, save_json, SPOTIFY_META_PATH, style_key,
    YOUTUBE_META_PATH,
)
from radar_jobs.sources import (
    bandcamp_albums, discogs_get, discogs_lookup, discogs_search, parse_yt_label,
    parse_yt_title, spotify_items, spotify_playlist_id, spotify_playlist_meta, spotify_token,
    youtube_items, yt_playlist_id,
)
from radar_jobs.tracks import _release_identity_key


# ============================================================= corpus helpers

def corpus_merge(new_rows, source):
    corpus = load_json(CORPUS_PATH, [])
    seen = {(r.get("source"), style_key(r.get("artist", "")), style_key(r.get("title", "")))
            for r in corpus}
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    for r in new_rows:
        sig = (source, style_key(r.get("artist", "")), style_key(r.get("title", "")))
        if sig in seen or not (r.get("artist") or r.get("title")):
            continue
        seen.add(sig)
        corpus.append({**r, "source": source, "added_at": now})
        added += 1
    save_json(CORPUS_PATH, corpus)
    return added, len(corpus)


# ============================================================= JOBS

def _ingest_lookup_loop(job, source, raw, token, cache, deep=True, kind="track"):
    """Boucle commune aux jobs ingest_* : dédoublonnage contre le corpus, lookup
    Discogs titre par titre, flush périodique, merge final. `raw` : liste de
    dicts artist/title (+ label/genre/url optionnels, déjà connus pour certaines
    sources et donc sautant le lookup)."""
    corpus = load_json(CORPUS_PATH, [])
    seen = {(source, style_key(r.get("artist", "")), style_key(r.get("title", ""))) for r in corpus}
    todo = [r for r in raw if (source, style_key(r["artist"]), style_key(r["title"])) not in seen]
    job.st["total"] = len(todo)
    job.msg(f"{len(raw)} titres, {len(todo)} à traiter.")
    acc = []
    for i, r in enumerate(todo):
        if job.stopped():
            break
        label, rid, style, calls = r.get("label"), None, [], 0
        if not label and (r["artist"] or r["title"]):
            hit, calls = discogs_lookup(token, r["artist"], r["title"], cache, kind=kind, deep=deep)
            if hit:
                label, rid, style = hit["label"], hit["release_id"], hit["style"]
        acc.append({"artist": r["artist"], "title": r["title"], "label": label,
                    "release_id": rid, "style": style, "genre": r.get("genre"), "url": r.get("url")})
        job.tick(f"{r['artist']} — {r['title']}" + (f"  → {label}" if label else "  → —"))
        if calls:
            time.sleep(max(0.2, 1.1 * calls - 0.3 * (calls - 1)))
        if (i + 1) % 15 == 0:
            corpus_merge(acc, source)
            acc = []
            save_json(LOOKUP_CACHE_PATH, cache)
    add, tot = corpus_merge(acc, source)
    save_json(LOOKUP_CACHE_PATH, cache)
    job.finish(f"+{job.st['done']} traités. Corpus : {tot}.")


def job_ingest_youtube(job, params):
    cfg = cfg_load()
    token = cfg.get("token", "")
    keys = ytcache.youtube_keys(cfg) if not params.get("api_key") else [params["api_key"]]
    urls = params.get("urls") or [u for u in cfg.get("youtube_playlists", "").splitlines() if u.strip()]
    deep = params.get("deep", True)
    pairs = [(u, yt_playlist_id(u)) for u in urls]
    pids = [p for _, p in pairs if p]
    if not pids:
        return job.finish(error="Aucune playlist.")
    job.msg("Lecture des playlists…")
    meta = load_json(YOUTUBE_META_PATH, {})
    raw = []
    for url, pid in [(u, p) for u, p in pairs if p]:
        try:
            d = ytcache.request("/playlists", {"part": "snippet", "id": pid}, keys)
            sn = (d.get("items") or [{}])[0].get("snippet", {})
        except Exception:
            sn = {}
        items = youtube_items(pid, keys)
        for it in items:
            a, t = parse_yt_title(it["title"], it["channel"])
            if a or t:
                raw.append({"artist": a, "title": t, "label": parse_yt_label(it["description"]),
                            "url": None})
        meta[pid] = {"url": url, "title": sn.get("title") or pid,
                     "channel": sn.get("channelTitle") or "",
                     "n_items": len(items),
                     "imported_at": datetime.now().isoformat(timespec="seconds")}
    save_json(YOUTUBE_META_PATH, meta)
    cache = load_json(LOOKUP_CACHE_PATH, {})
    _ingest_lookup_loop(job, "youtube", raw, token, cache, deep=deep)


def job_ingest_spotify(job, params):
    cfg = cfg_load()
    token = cfg.get("token", "")
    cid = params.get("client_id") or cfg.get("spotify_client_id", "")
    csec = params.get("client_secret") or cfg.get("spotify_client_secret", "")
    urls = params.get("urls") or [u for u in cfg.get("spotify_playlists", "").splitlines() if u.strip()]
    deep = params.get("deep", True)
    if not (cid and csec):
        return job.finish(error="Identifiants API Spotify manquants (client ID + secret).")
    pids = [(u, p) for u, p in ((u, spotify_playlist_id(u)) for u in urls) if p]
    if not pids:
        return job.finish(error="Aucune playlist Spotify.")
    job.msg("Connexion à l'API Spotify…")
    try:
        tok = spotify_token(cid, csec)
    except Exception as e:
        return job.finish(error=str(e))
    if not tok:
        return job.finish(error="Spotify : jeton d'accès vide (identifiants ?).")
    meta = load_json(SPOTIFY_META_PATH, {})
    raw = []
    for url, pid in pids:
        try:
            m = spotify_playlist_meta(pid, tok)
        except Exception:
            m = {"title": pid, "channel": "", "n_items": 0}
        try:
            items = spotify_items(pid, tok)
        except Exception as e:
            return job.finish(error=str(e))
        for it in items:
            if it["artist"] or it["title"]:
                raw.append({"artist": it["artist"], "title": it["title"], "label": None, "url": None})
        meta[pid] = {"url": url, "title": m["title"], "channel": m["channel"],
                     "n_items": len(items) or m["n_items"],
                     "imported_at": datetime.now().isoformat(timespec="seconds")}
    save_json(SPOTIFY_META_PATH, meta)
    cache = load_json(LOOKUP_CACHE_PATH, {})
    _ingest_lookup_loop(job, "spotify", raw, token, cache, deep=deep)


def job_ingest_bandcamp(job, params):
    cfg = cfg_load()
    token = cfg.get("token", "")
    u = params.get("user") or cfg.get("bandcamp_sub_user", "")
    pw = params.get("password") or cfg.get("bandcamp_sub_pass", "")
    deep = params.get("deep", True)
    if not (u and pw):
        return job.finish(error="Identifiants Subsonic manquants.")
    job.msg("Lecture de la collection Bandcamp…")
    albums = bandcamp_albums(u, pw)
    cache = load_json(LOOKUP_CACHE_PATH, {})
    _ingest_lookup_loop(job, "bandcamp", albums, token, cache, deep=deep, kind="release")


def job_fetch_collection(job, params):
    cfg = cfg_load()
    token = cfg.get("token", "")
    ident = discogs_get(token, "/oauth/identity")
    user = ident.get("username")
    if not user:
        return job.finish(error="Identité Discogs illisible (token ?).")
    lc, wc, lids, ac = {}, {}, {}, {}
    owned_ids, owned_keys = set(), set()
    n_coll = n_want = 0
    for kind, path, rk, tgt in (
        ("collection", f"/users/{user}/collection/folders/0/releases", "releases", lc),
        ("wantlist", f"/users/{user}/wants", "wants", wc),
    ):
        page, pages = 1, 1
        while page <= pages and page <= 200:
            if job.stopped():
                break
            d = discogs_get(token, path, {"page": page, "per_page": 100})
            items = d.get(rk, [])
            for it in items:
                bi = it.get("basic_information", {})
                for lb in bi.get("labels", []):
                    nm = (lb.get("name") or "").strip()
                    if not nm:
                        continue
                    k = normalize_label(nm)
                    tgt[k] = tgt.get(k, 0) + 1
                    if lb.get("id") and k not in lids:
                        lids[k] = {"name": nm, "id": lb.get("id")}
                names = [(ar.get("name") or "").strip() for ar in bi.get("artists", [])]
                for nm in names:
                    if nm and nm.lower() != "various":
                        ac[nm] = ac.get(nm, 0) + 1
                if kind == "collection":
                    # Identité des disques POSSÉDÉS, pour ne plus les proposer en
                    # reco (retour utilisateur 2026-09-17). L'id seul ne suffit
                    # pas : la collection porte UN pressage, la découverte RECOS
                    # peut en remonter un autre — d'où la clé artiste+titre en
                    # plus (`_release_identity_key`). `basic_information.id` est
                    # l'id de la SORTIE, l'objet racine portant aussi un
                    # `instance_id` propre à la copie possédée.
                    rid = bi.get("id") or it.get("id")
                    if str(rid).isdigit():
                        owned_ids.add(int(rid))
                    k = _release_identity_key(", ".join(n for n in names if n), bi.get("title"))
                    if k:
                        owned_keys.add(k)
            if kind == "collection":
                n_coll += len(items)
            else:
                n_want += len(items)
            pages = d.get("pagination", {}).get("pages", 1)
            job.tick(f"{kind} page {page}/{pages}", total=None)
            page += 1
            if page <= pages:
                time.sleep(1.1)
    cache = {"username": user, "fetched_at": datetime.now().isoformat(timespec="seconds"),
             "n_collection": n_coll, "n_wants": n_want, "label_counts": lc,
             "want_label_counts": wc, "label_ids": lids, "artist_counts": ac,
             "owned_release_ids": sorted(owned_ids), "owned_release_keys": sorted(owned_keys)}
    save_json(COLLECTION_CACHE_PATH, cache)
    # merge labels into base + seed resolved
    if params.get("merge_base", True):
        cfg = cfg_load()
        lcats = cfg.setdefault("label_categories", {"1": [], "2": []})
        exist = {normalize_label(x) for cid in ("1", "2") for x in lcats.get(cid, [])}
        newn = [v["name"] for k, v in lids.items() if k not in exist]
        lcats.setdefault("2", []).extend(newn)   # nouveaux labels détectés -> Aimé par défaut
        save_json(CONFIG_PATH, cfg)
        res = load_json(RESOLVED_PATH, {})
        for k, v in lids.items():
            cur = res.get(k)
            if cur and cur.get("status") in ("exact", "confirmed"):
                continue
            res[k] = {"original": v["name"], "discogs_name": v["name"], "discogs_id": v["id"],
                      "status": "confirmed", "reviewed_by": "collection"}
        save_json(RESOLVED_PATH, res)
        job.finish(f"{n_coll} disques, {n_want} wants. +{len(newn)} labels ajoutés à la base.")
    else:
        job.finish(f"{n_coll} disques, {n_want} wants.")


def job_merge_corpus(job, params):
    """Fusionne les labels du corpus dans la base + nettoyage."""
    cfg = cfg_load()
    corpus = load_json(CORPUS_PATH, [])
    resolved = load_json(RESOLVED_PATH, {})
    lcats = cfg.setdefault("label_categories", {"1": [], "2": []})
    base_keys = {normalize_label(x) for cid in ("1", "2") for x in lcats.get(cid, [])}

    junk_sub = ["(bmi)", "(ascap)", "(sesac)", "(prs)", "(gema)", "(sacem)", "distrokid",
                "cd baby", "cdbaby", "tunecore", "the orchard", "believe digital", "ingrooves",
                "routenote", "amuse", "ditto music", "label engine"]

    def clean(name):
        s = re.sub(r"^\s*discogs\s*:\s*", "", (name or "").strip(), flags=re.I)
        m = re.search(r"(?:licen[sc]e\s+(?:to|from)|distributed by|marketed by)\s+(.+)$", s, re.I)
        if m:
            s = m.group(1).strip()
        s = s.rstrip(" :;.-")
        if "http" in s.lower() or "_" in s or len(s) < 2:
            return ""
        return s

    def is_junk(n):
        low = n.lower()
        return (not low or "not on label" in low or low in ("none", "n/a", "unknown")
                or any(x in low for x in junk_sub))

    counts, disp = Counter(), {}
    dropped = []
    for r in corpus:
        lab = clean(r.get("label"))
        if not lab:
            if r.get("label"):
                dropped.append(r["label"])
            continue
        k = normalize_label(lab)
        counts[k] += 1
        disp.setdefault(k, lab)
    good = [k for k in disp if not is_junk(disp[k])]
    new = sorted(disp[k] for k in good if k not in base_keys)
    job.st["total"] = len(new)

    n_before = len(base_keys)
    lcats.setdefault("2", []).extend(new)   # nouveaux labels détectés -> Aimé par défaut
    save_json(CONFIG_PATH, cfg)
    seeded = 0
    for k in good:
        cur = resolved.get(k)
        if cur and cur.get("status") in ("exact", "confirmed"):
            continue
        resolved[k] = {"original": disp[k], "discogs_name": disp[k],
                       "discogs_id": (cur or {}).get("discogs_id"),
                       "status": "confirmed", "reviewed_by": "corpus"}
        seeded += 1
    save_json(RESOLVED_PATH, resolved)
    job.st["done"] = len(new)
    job.finish(f"+{len(new)} labels ajoutés (base {n_before} → {n_before + len(new)}), "
               f"{seeded} résolus, {len(set(dropped))} écartés.")


def _reco_label_priority():
    coll = load_json(COLLECTION_CACHE_PATH, {})
    corpus = load_json(CORPUS_PATH, [])
    score = Counter()
    disp = {}
    for k, n in coll.get("label_counts", {}).items():
        score[k] += n
        disp.setdefault(k, coll.get("label_ids", {}).get(k, {}).get("name") or k)
    for k, n in coll.get("want_label_counts", {}).items():
        score[k] += 0.6 * n
        disp.setdefault(k, coll.get("label_ids", {}).get(k, {}).get("name") or k)
    for r in corpus:
        if r.get("label"):
            k = normalize_label(r["label"])
            score[k] += 0.5
            disp.setdefault(k, r["label"])
    return score, disp


def job_profile_labels(job, params):
    cfg = cfg_load()
    token = cfg.get("token", "")
    limit = int(params.get("limit", 150))
    profile = load_json(PROFILE_PATH, {})
    resolved = load_json(RESOLVED_PATH, {})
    score, disp = _reco_label_priority()
    # 1) labels prioritaires (collection / wantlist / corpus), les plus fréquents d'abord
    ordered = [k for k, _ in score.most_common()]
    seen = set(ordered)
    # 2) puis tout le reste de la base de labels (non couvert par le signal ci-dessus)
    lcats = cfg.get("label_categories", {})
    for name in [n for cid in ("1", "2") for n in lcats.get(cid, [])]:
        k = normalize_label(name)
        if k and k not in seen:
            seen.add(k)
            ordered.append(k)
            disp.setdefault(k, name)
    todo = [k for k in ordered if k not in profile][:limit]
    job.st["total"] = len(todo)

    def canon(k):
        e = resolved.get(k)
        if e and e.get("status") in ("exact", "approx", "confirmed") and e.get("discogs_name"):
            return e["discogs_name"]
        return disp.get(k, k)

    for i, k in enumerate(todo):
        if job.stopped():
            break
        d = discogs_search(token, label=canon(k), per_page=100, page=1,
                           sort="want", sort_order="desc")
        res = d.get("results", [])
        sc, gc = Counter(), Counter()
        for x in res:
            sc.update(x.get("style") or [])
            gc.update(x.get("genre") or [])
        profile[k] = {"original": disp.get(k, k), "sampled": len(res),
                      "style_counts": dict(sc), "genre_counts": dict(gc),
                      "total_items": d.get("pagination", {}).get("items", len(res)),
                      "profiled_at": datetime.now().isoformat(timespec="seconds")}
        job.tick(f"{canon(k)} — {len(res)} sorties")
        if (i + 1) % 15 == 0:
            save_json(PROFILE_PATH, profile)
        time.sleep(1.1)
    save_json(PROFILE_PATH, profile)
    job.finish(f"+{job.st['done']} labels profilés. Total : {len(profile)}.")
