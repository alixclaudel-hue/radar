"""Extraction des DJ sets (`ingest_djsets`) : yt-dlp + Playwright.
"""

import os
import re
import time
from datetime import datetime

from radar_web.radar import ytcache
from radar_jobs.common import (
    cfg_load, CORPUS_PATH, DJSET_INPUT_PATH, DJSET_SEEN_PATH, HERE, load_json,
    LOOKUP_CACHE_PATH, normalize_label, save_json, style_key,
)
from radar_jobs.sources import discogs_lookup


_DJSET_HINT_RE = re.compile(
    r"\b(dj[- ]?set|\bset\b|\bmix\b|mixtape|b2b|boiler ?room|essential mix|"
    r"live (?:set|@|at)|podcast|\bradio\b|session|warm[- ]?up|closing set|opening set|"
    r"in the mix|guest mix|resident)\b", re.I)


def _yt_descriptions(ids, keys):
    """{video_id: description} — cache partagé (30 j) + cascade de clés."""
    out, miss = {}, []
    for v in ids:
        c = ytcache.cache_get("video:" + v, 30 * 86400)
        if c is not None:
            out[v] = c
        else:
            miss.append(v)
    if not keys:
        return out
    for i in range(0, len(miss), 50):
        chunk = miss[i:i + 50]
        try:
            d = ytcache.request("/videos", {"part": "snippet", "id": ",".join(chunk)}, keys)
        except Exception:
            continue
        for it in d.get("items", []):
            desc = it.get("snippet", {}).get("description", "")
            out[it["id"]] = desc
            ytcache.cache_put("video:" + it["id"], desc)
    return out


#  Une recherche texte brute ("nom du DJ") classe les résultats par pertinence
#  générale YouTube : un set iconique mais moins vu, ou repris par un tiers
#  (Boiler Room, Cercle, une radio, un festival) plutôt que posté sur la
#  chaîne de l'artiste, se retrouve souvent hors des toutes premières pages
#  de CETTE requête précise et n'est donc jamais récupéré, quel que soit le
#  filtre appliqué ensuite. Chaque variante ci-dessous interroge l'index de
#  recherche sous un angle différent (le nom seul ne suffit pas) et couvre
#  une tranche différente de résultats — cumulées, elles trouvent largement
#  plus de sets qu'une seule requête, y compris ceux hébergés ailleurs que
#  sur la chaîne propre de l'artiste.
_SET_QUERY_HINTS = ("", "boiler room", "essential mix", "dj set", "live set",
                    "full set", "b2b", "radio show")


def _yt_video_ids(source, max_n, min_seconds=2100, keys=(), require_hint=True):
    """[(video_id, source_label)] pour une source. Filtres : durée ≥ min_seconds ;
    pour une recherche texte, le nom doit figurer dans le titre ; si `require_hint`,
    un mot-clé de set (set / mix / radio / boiler room / podcast / session…) doit
    apparaître dans le titre OU la description.

    Pour une recherche texte (pas un @handle/URL de chaîne, déjà ciblé par
    nature), interroge plusieurs variantes de requête (cf. `_SET_QUERY_HINTS`)
    et fusionne les résultats avant de filtrer — une seule requête au nom nu
    passe à côté des sets tiers mal classés pour cette requête précise."""
    import random
    import yt_dlp
    s = source.strip()
    is_text = not (s.startswith("@") or "youtube.com/" in s or s.startswith("http"))
    if s.startswith("@"):
        targets = [f"https://www.youtube.com/{s}/videos"]
    elif "youtube.com/" in s:
        targets = [s if "/videos" in s or "list=" in s else s.rstrip("/") + "/videos"]
    elif s.startswith("http"):
        targets = [s]
    else:
        per_query = max(max_n * 2, 25)      # marge pour filtrer, par variante
        targets = [f"ytsearch{per_query}:{(s + ' ' + hint).strip()}" for hint in _SET_QUERY_HINTS]

    # on sépare le NOM (à exiger dans le titre) des mots-indices de recherche
    _hints = {"set", "sets", "dj", "mix", "mixe", "mixes", "radio", "boiler", "room",
              "essential", "live", "podcast", "b2b", "closing", "opening", "warmup",
              "warm", "up", "at", "the", "show", "session", "sessions"}
    name_toks = [t for t in re.split(r"[^\w]+", s.lower()) if len(t) > 1 and t not in _hints]
    name_norm = " ".join(name_toks)

    opts = {"extract_flat": True, "quiet": True, "no_warnings": True, "skip_download": True}
    cookies = os.path.join(HERE, "www.youtube.com_cookies.txt")
    if os.path.exists(cookies):
        opts["cookiefile"] = cookies
    roots = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        for i, target in enumerate(targets):
            try:
                roots.append(ydl.extract_info(target, download=False))
            except Exception:
                continue
            if i < len(targets) - 1:
                time.sleep(random.uniform(1, 2))    # plusieurs requêtes d'affilée : ne pas cogner

    # 1) candidats : durée OK + (recherche texte) nom dans le titre
    cand, seen, stack = [], set(), list(roots)
    while stack and len(cand) < max_n * 3 * len(targets):
        node = stack.pop(0)
        if not node:
            continue
        if node.get("entries"):
            stack = list(node["entries"]) + stack
            continue
        vid = node.get("id")
        if not vid or len(vid) != 11 or vid in seen:
            continue
        seen.add(vid)
        dur = node.get("duration")
        if dur is not None and dur < min_seconds:
            continue
        title = node.get("title") or ""
        if is_text and title and name_toks:
            tl = title.lower()
            ok = (name_norm and name_norm in normalize_label(title)) or (
                sum(t in tl for t in name_toks) / len(name_toks) >= 0.6)
            if not ok:
                continue
        cand.append((vid, title))

    # 2) filtre "c'est bien un set" : mot-clé dans le titre OU la description
    #    (pour une recherche texte seulement ; une chaîne/@handle est déjà de confiance)
    if require_hint and is_text and cand:
        descs = _yt_descriptions([v for v, _ in cand], keys)
        cand = [(v, t) for v, t in cand
                if _DJSET_HINT_RE.search(f"{t}\n{descs.get(v, '')}")]

    return [(v, s, t) for v, t in cand[:max_n]]


def _scrape_music_panel(page, video_id):
    """Cartes du panneau « Musique » d'une vidéo -> [(title, artist, album)]."""
    page.goto(f"https://www.youtube.com/watch?v={video_id}", timeout=30000)
    page.wait_for_timeout(2500)
    for t in ("text=Tout accepter", "text=Accept all", "button:has-text('Accept all')"):
        try:
            page.click(t, timeout=1500)
        except Exception:
            pass
    for sel in ("tp-yt-paper-button#expand", "#expand", "ytd-text-inline-expander #expand"):
        try:
            page.click(sel, timeout=2000)
            break
        except Exception:
            pass
    page.wait_for_timeout(1500)
    out = []
    for card in page.query_selector_all("yt-video-attribute-view-model"):
        def _txt(sel):
            el = card.query_selector(sel)
            return el.inner_text().strip() if el else None
        title = _txt("h1.ytVideoAttributeViewModelTitle")
        artist = _txt("h4.ytVideoAttributeViewModelSubtitle")
        album = _txt("span.ytVideoAttributeViewModelSecondarySubtitle")
        link_el = card.query_selector("a.ytVideoAttributeViewModelContentContainer")
        href = link_el.get_attribute("href") if link_el else None
        url = f"https://www.youtube.com{href}" if href and href.startswith("/") else href
        if title or artist:
            out.append((title, artist, album, url))
    # dédoublonnage interne (le panneau apparaît parfois 2× dans le DOM)
    seen, uniq = set(), []
    for row in out:
        if row not in seen:
            seen.add(row)
            uniq.append(row)
    return uniq


def job_ingest_djsets(job, params):
    """Extrait les tracks des sets/podcasts YouTube de DJs choisis (panneau « Musique »
    via Playwright) et les verse dans le corpus (source=djset, une ligne par couple
    track×DJ)."""
    try:
        import yt_dlp  # noqa
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        return job.finish(error=f"Dépendance manquante ({e.name}). Installe : "
                                "pip3 install yt-dlp playwright && playwright install chromium")

    cfg = cfg_load()
    token = cfg.get("token", "")
    inp = load_json(DJSET_INPUT_PATH, {})
    sources = [s.strip() for s in inp.get("sources", []) if s.strip()]
    max_per = int(inp.get("max_per_source", 25))
    min_seconds = int(inp.get("min_minutes", 35)) * 60
    require_hint = bool(inp.get("require_hint", True))
    yt_keys = ytcache.youtube_keys(cfg)
    deep = bool(inp.get("deep", True))
    if not sources:
        return job.finish(error="Aucune source (DJ / émission / chaîne).")

    job.msg("YouTube · recherche des sets…")
    vids = []
    for src in sources:
        try:
            got = _yt_video_ids(src, max_per, min_seconds, yt_keys, require_hint)
            vids += got
            job.msg(f"YouTube · {src} : {len(got)} set(s) identifié(s)")
        except Exception as e:
            job.msg(f"{src} : erreur listing ({e})")
        if job.stopped():
            break

    seen_vids = set(load_json(DJSET_SEEN_PATH, []))
    todo = [(v, dj, vt) for v, dj, vt in vids if v not in seen_vids]
    job.st["total"] = len(todo)
    job.msg(f"YouTube · {len(vids)} set(s) identifié(s), {len(todo)} à scanner")
    if not vids:
        return job.finish(error="Aucune vidéo trouvée pour ces sources. Essaie un @handle, "
                                "une URL de chaîne, ou ajoute « set »/« boiler room » au nom.")
    if not todo:
        return job.finish(f"Les {len(vids)} vidéo(s) trouvées ont déjà été traitées "
                          "(voir « Réinitialiser l'historique » pour tout re-scanner).")

    corpus = load_json(CORPUS_PATH, [])
    cache = load_json(LOOKUP_CACHE_PATH, {})
    dj_seen = {(r.get("source"), style_key(r.get("artist", "")), style_key(r.get("title", "")),
               style_key(r.get("dj", ""))) for r in corpus if r.get("source") == "djset"}
    added, now = 0, datetime.now().isoformat(timespec="seconds")

    import random
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        pg = browser.new_page()
        for i, (vid, dj, vtitle) in enumerate(todo):
            if job.stopped():
                break
            try:
                tracks = _scrape_music_panel(pg, vid)
            except Exception as e:
                tracks = []
                job.msg(f"{vid} : {e}")
            for title, artist, album, track_url in tracks:
                a = (artist or "").strip()
                t = (title or "").strip()
                if not (a or t):
                    continue
                sig = ("djset", style_key(a), style_key(t), style_key(dj))
                if sig in dj_seen:
                    continue
                dj_seen.add(sig)
                label, rid, style, vinyl_id = None, None, [], None
                hit, _ = discogs_lookup(token, a, t, cache, deep=deep)
                if hit:
                    label, rid, style = hit["label"], hit["release_id"], hit["style"]
                    vinyl_id = hit.get("vinyl_release_id")
                corpus.append({"artist": a, "title": t, "label": label, "release_id": rid,
                               "vinyl_release_id": vinyl_id, "style": style, "genre": None,
                               "url": None, "source": "djset", "dj": dj, "video": vid,
                               "set_title": vtitle, "track_url": track_url, "added_at": now})
                added += 1
            seen_vids.add(vid)
            job.tick(f"YouTube · {dj} · set {job.st['done'] + 1}/{len(todo)} · "
                     f"{len(tracks)} track(s) · +{added} au corpus")
            if (i + 1) % 8 == 0:
                save_json(CORPUS_PATH, corpus)
                save_json(LOOKUP_CACHE_PATH, cache)
                save_json(DJSET_SEEN_PATH, sorted(seen_vids))
            time.sleep(random.uniform(4, 8))
        browser.close()

    save_json(CORPUS_PATH, corpus)
    save_json(LOOKUP_CACHE_PATH, cache)
    save_json(DJSET_SEEN_PATH, sorted(seen_vids))
    job.finish(f"{job.st['done']} vidéo(s) traitées → +{added} entrées djset au corpus "
               f"(total corpus {len(corpus)}).")
