"""Jobs de recherche et de marché : `search_base`, `scan_sellers`, `scan_veille`,
`seller_inventory`, `scan_catalog`.
"""

import os
import time
from datetime import datetime

from radar_jobs.common import (
    cfg_load, load_json, normalize_label, RESOLVED_PATH, save_json, SEARCH_INPUT_PATH,
    SEARCH_RESULTS_PATH, SELLERS_NEW_PATH, SELLERS_SEEN_PATH, VEILLE_NEW_PATH,
    VEILLE_SEEN_PATH,
)
from radar_jobs.sources import discogs_get, discogs_search


def _search_label_releases(token, label_name, genre, style, fmt, year, max_pages):
    out, page = [], 1
    while page <= max_pages:
        d = discogs_search(token, label=label_name, genre=genre, style=style, format=fmt,
                           year=year, per_page=100, page=page, sort="year", sort_order="desc")
        out.extend(d.get("results", []))
        if page >= d.get("pagination", {}).get("pages", 1):
            break
        page += 1
        time.sleep(1.1)
    return out


def job_search_base(job, params):
    """Scan « sans label » : une requête par label du périmètre, filtrée genre/style/
    année. Périmètre + filtres lus dans jobs/search_base.input.json. Résultat écrit
    dans search_results.json."""
    cfg = cfg_load()
    token = cfg.get("token", "")
    inp = load_json(SEARCH_INPUT_PATH, {})
    scope = inp.get("scope") or []
    genre, style, fmt = inp.get("genre", ""), inp.get("style", ""), inp.get("fmt", "")
    yr = inp.get("year", "")
    pages = int(inp.get("pages", 3))
    yf = int(inp["year_from"]) if str(inp.get("year_from", "")).strip().isdigit() else None
    yt = int(inp["year_to"]) if str(inp.get("year_to", "")).strip().isdigit() else None
    resolved = load_json(RESOLVED_PATH, {})

    def canon(name):
        e = resolved.get(normalize_label(name))
        if e and e.get("status") in ("exact", "approx", "confirmed") and e.get("discogs_name"):
            return e["discogs_name"]
        return name

    job.st["total"] = len(scope)
    acc, errors = [], []
    for lab in scope:
        if job.stopped():
            break
        c = canon(lab)
        try:
            rels = _search_label_releases(token, c, genre, style, fmt, yr, pages)
            if not rels:
                d2 = discogs_search(token, q=c, genre=genre, style=style, format=fmt,
                                    year=yr, per_page=100, sort="year", sort_order="desc")
                tgt = normalize_label(c)
                rels = [r for r in d2.get("results", [])
                        if any(normalize_label(x) == tgt for x in r.get("label", []))]
            for r in rels:
                r["_base_label"] = lab
            acc.extend(rels)
        except Exception as e:
            errors.append(f"{lab}: {e}")
        job.tick(f"{lab} → {len(acc)} cumulées")
        time.sleep(1.1)

    seen, out = set(), []
    for r in acc:
        rid = r.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        y = int(r.get("year") or 0)
        if y and yf and y < yf:
            continue
        if y and yt and y > yt:
            continue
        out.append(r)
    out.sort(key=lambda r: int(r.get("year") or 0), reverse=True)
    save_json(SEARCH_RESULTS_PATH, out)
    job.finish(f"{len(scope)} labels scannés → {len(out)} sortie(s)."
               + (f" {len(errors)} erreur(s)." if errors else ""))


def job_scan_sellers(job, params):
    """Scanne l'inventaire « For Sale » de chaque vendeur suivi, repère les annonces
    apparues depuis le dernier passage (diff sur l'id d'annonce) et les empile dans
    seller_new.json. Le 1er scan d'un vendeur sert de référence (aucune nouveauté)."""
    cfg = cfg_load()
    token = cfg.get("token", "")
    sellers = [s.strip() for s in cfg.get("sellers", []) if s and s.strip()]
    if not token:
        return job.finish(error="Pas de token Discogs.")
    if not sellers:
        return job.finish(error="Aucun vendeur suivi.")
    max_pages = int(params.get("max_pages", 5))
    seen = load_json(SELLERS_SEEN_PATH, {})
    queue = load_json(SELLERS_NEW_PATH, [])
    known = {e.get("listing_id") for e in queue}
    job.st["total"] = len(sellers)
    now = datetime.now().isoformat(timespec="seconds")
    total_new = 0
    for s in sellers:
        if job.stopped():
            break
        entry = seen.get(s) or {}
        seen_ids = set(entry.get("ids", []))
        bootstrapped = bool(entry.get("bootstrapped"))
        listings = []
        for page in range(1, max_pages + 1):
            d = discogs_get(token, f"/users/{s}/inventory",
                            {"status": "For Sale", "per_page": 100, "page": page,
                             "sort": "listed", "sort_order": "desc"})
            got = d.get("listings", [])
            listings += got
            if page >= d.get("pagination", {}).get("pages", 1) or not got:
                break
            time.sleep(1.1)
        cur_ids = [x.get("id") for x in listings if x.get("id")]
        fresh = [x for x in listings if x.get("id")
                 and x["id"] not in seen_ids and x["id"] not in known]
        if bootstrapped:
            for x in fresh:
                rel = x.get("release", {}) or {}
                desc = rel.get("description") or ""
                artist, _, title = desc.partition(" - ")
                lab = rel.get("label")
                if isinstance(lab, list):
                    lab = lab[0] if lab else None
                queue.append({
                    "listing_id": x.get("id"), "seller": s, "release_id": rel.get("id"),
                    "artist": artist.strip(), "title": (title or desc).strip(), "label": lab,
                    "style": rel.get("style") or [], "genre": rel.get("genre") or [],
                    "year": rel.get("year"), "thumb": rel.get("thumbnail"),
                    "format": rel.get("format"),
                    "price": (x.get("price") or {}).get("value"),
                    "currency": (x.get("price") or {}).get("currency"),
                    "condition": x.get("condition"),
                    "uri": x.get("uri") or (f"https://www.discogs.com/sell/item/{x.get('id')}"
                                            if x.get("id") else None),
                    "first_seen": now,
                })
                known.add(x.get("id"))
            total_new += len(fresh)
            job.msg(f"{s} : +{len(fresh)} nouvelle(s)")
        else:
            job.msg(f"{s} : {len(listings)} annonces (référence initiale)")
        merged = cur_ids + [i for i in entry.get("ids", []) if i not in set(cur_ids)]
        seen[s] = {"ids": merged[:3000], "bootstrapped": True, "last_scan": now}
        job.tick(s)
        save_json(SELLERS_SEEN_PATH, seen)
        save_json(SELLERS_NEW_PATH, queue)
    save_json(SELLERS_SEEN_PATH, seen)
    save_json(SELLERS_NEW_PATH, queue)
    job.finish(f"+{total_new} nouvelle(s) annonce(s) chez {len(sellers)} vendeur(s) · "
               f"file d'attente : {len(queue)}.")


def _veille_search(token, rule):
    """Releases Discogs correspondant à une règle de veille (dédoublonnées par id)."""
    styles = (rule.get("styles") or [])[:3]
    genre = (rule.get("genres") or [None])[0]
    yf, yt = rule.get("year_from"), rule.get("year_to")
    yr = f"{yf}-{yt}" if (yf and yt) else (str(yf) if yf else (str(yt) if yt else None))
    fmt = "Vinyl" if rule.get("vinyl_only") else None
    labels = rule.get("labels") or []
    base = {"sort": "year", "sort_order": "desc", "per_page": 100}
    if genre:
        base["genre"] = genre
    if yr:
        base["year"] = yr
    if fmt:
        base["format"] = fmt
    out, seen = [], set()

    def go(extra, pages):
        for pg in range(1, pages + 1):
            d = discogs_search(token, page=pg, **base, **extra)
            res = d.get("results", [])
            for r in res:
                if r.get("id") and r["id"] not in seen:
                    seen.add(r["id"])
                    out.append(r)
            if pg >= d.get("pagination", {}).get("pages", 1) or not res:
                break
            time.sleep(1.1)

    if labels:
        for lab in labels:
            go({"label": lab}, 1)
            time.sleep(1.1)
    elif styles:
        for stl in styles:
            go({"style": stl}, 2)
            time.sleep(1.1)
    else:
        go({}, 3)
    return out


def job_scan_veille(job, params):
    """Scanne chaque règle de veille active (+ la règle implicite « labels suivis »),
    repère les sorties Discogs nouvelles depuis le dernier passage et les empile dans
    veille_new.json. 1er passage d'une règle = référence."""
    cfg = cfg_load()
    token = cfg.get("token", "")
    if not token:
        return job.finish(error="Pas de token Discogs.")
    rules = [dict(r) for r in cfg.get("veille_rules", []) if r.get("active", True)]
    lcats = cfg.get("label_categories", {})
    wl = [w for cid in ("1", "2") for w in lcats.get(cid, []) if w and w.strip()]
    wl_cap = int(params.get("watchlist_cap", 150))
    if wl:
        rules.insert(0, {"id": "__watchlist__",
                         "name": f"Labels suivis ({min(len(wl), wl_cap)}/{len(wl)})",
                         "labels": wl[:wl_cap], "vinyl_only": True})
    if not rules:
        return job.finish("Aucune règle de veille active.")
    seen = load_json(VEILLE_SEEN_PATH, {})
    queue = load_json(VEILLE_NEW_PATH, [])
    known = {str(e.get("release_id")) for e in queue}
    job.st["total"] = len(rules)
    now = datetime.now().isoformat(timespec="seconds")
    total_new = 0
    for rule in rules:
        if job.stopped():
            break
        rid = rule.get("id") or normalize_label(rule.get("name", "")) or "r"
        ent = seen.get(rid) or {}
        seen_ids = set(ent.get("ids", []))
        boot = bool(ent.get("bootstrapped"))
        try:
            found = _veille_search(token, rule)
        except Exception as e:
            job.msg(f"{rule.get('name')} : erreur ({e})")
            found = []
        cur_ids = [str(r.get("id")) for r in found if r.get("id")]
        fresh = [r for r in found if r.get("id") and str(r["id"]) not in seen_ids
                 and str(r["id"]) not in known]
        if boot:
            for r in fresh:
                labs = r.get("label") or []
                t = r.get("title", "")
                art, _, ti = t.partition(" - ")
                queue.append({
                    "release_id": r.get("id"), "rule": rule.get("name"),
                    "artist": art.strip() if ti else "", "title": (ti or t).strip(),
                    "label": labs[0] if labs else None,
                    "style": r.get("style") or [], "year": r.get("year"),
                    "thumb": r.get("thumb") or r.get("cover_image"),
                    "uri": f"https://www.discogs.com{r.get('uri')}" if r.get("uri") else None,
                    "first_seen": now,
                })
                known.add(str(r.get("id")))
            total_new += len(fresh)
            job.msg(f"{rule.get('name')} : +{len(fresh)} nouveauté(s)")
        else:
            job.msg(f"{rule.get('name')} : {len(found)} sorties (référence initiale)")
        merged = cur_ids + [i for i in ent.get("ids", []) if i not in set(cur_ids)]
        seen[rid] = {"ids": merged[:4000], "bootstrapped": True, "last_scan": now}
        job.tick(rule.get("name", ""))
        save_json(VEILLE_SEEN_PATH, seen)
        save_json(VEILLE_NEW_PATH, queue)
    save_json(VEILLE_SEEN_PATH, seen)
    save_json(VEILLE_NEW_PATH, queue)
    job.finish(f"+{total_new} nouveauté(s) sur {len(rules)} règle(s) · file d'attente {len(queue)}.")


def job_seller_inventory(job, params):
    """Snapshot EXHAUSTIF du stock « For Sale » d'UN vendeur Discogs nommé à la
    volée depuis /search (demande utilisateur 2026-09-19, point 68 de CLAUDE.md).

    Pourquoi un job et pas un appel dans la requête HTTP : l'inventaire Discogs
    se lit 100 articles par page, cadencé à 1,1 s (60 requêtes/min côté
    Discogs). Un disquaire comme hhv a des dizaines de milliers d'articles, donc
    plusieurs minutes de pagination — au-delà de ce que tient une requête web, et
    la page de résultats n'a pas à rester bloquée dessus. Le snapshot est ensuite
    relu instantanément par /search, qui y applique ses filtres sur la TOTALITÉ
    du stock au lieu d'un échantillon.

    Écrit au même endroit et au même format que `scan_catalog`
    (`radar/sellers.py`, `seller_inventory/<vendeur>.json`) : les deux jobs se
    servent mutuellement leurs snapshots. La pagination, elle, n'est pas
    factorisée avec celle de `scan_catalog` — cette dernière est enchevêtrée
    dans la tenue du catalogue (compteur d'échecs, désactivation d'un compte
    injoignable) d'une fonctionnalité en pause depuis le point 56, que ce lot
    ne touche pas.

    `max_pages` (défaut 0 = pas de plafond) borne la pagination pour un essai
    rapide. Un arrêt demandé (bouton « arrêter ») coupe proprement : le snapshot
    partiel n'est enregistré que s'il n'y en avait aucun, pour ne jamais
    remplacer un inventaire complet par un morceau."""
    from radar_web.radar import discogs as dgs
    from radar_web.radar import sellers as scat

    seller = (params.get("seller") or "").strip().lstrip("@")
    if not seller:
        return job.finish(error="Aucun vendeur indiqué — ce job se lance depuis "
                                "« Chercher un disque » (champ Vendeur Discogs).")
    token = cfg_load().get("token", "")
    if not token:
        return job.finish(error="Pas de token Discogs (Mon profil → Discogs).")
    max_pages = int(params.get("max_pages", 0) or 0)
    job.msg(f"{seller} : lecture du stock en vente…")

    def _snap(listings):
        snap = {}
        for it in listings:
            rid = it.get("release_id")
            if not rid:
                continue
            snap[str(rid)] = {k: it.get(k) for k in
                              ("listing_id", "price", "currency", "condition",
                               "sleeve", "listed", "artist", "format", "title")}
        return snap

    # Première lecture seulement : un snapshot complet déjà présent ne doit
    # jamais être remplacé par un morceau. Les résultats s'affichent ainsi au
    # fil de la lecture (`reading` : /search sait que ça continue).
    first_read = not scat.load_inventory(seller)

    def _on_batch(listings, page):
        if first_read and page % 3 == 0:
            snap = _snap(listings)
            scat.save_inventory(seller, snap)
            scat.set_inv_meta(seller, {
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "n_items": len(snap), "n_listings": len(listings),
                "n_pages": page, "partial": True, "reading": True})

    def _on_page(n_items, page, pages):
        job.tick(f"{seller} : page {page}/{pages} · {n_items} article(s) lus", total=pages)
        job.sub(done=n_items, label=f"{seller} — articles lus")
        return not job.stopped()

    try:
        listings, truncated = dgs.seller_inventory(seller, token=token,
                                                   max_pages=max_pages, on_page=_on_page,
                                                   on_batch=_on_batch)
    except dgs.DiscogsError as e:
        return job.finish(error=f"Vendeur « {seller} » : {e}")

    snap = _snap(listings)
    if truncated and not first_read:
        return job.finish(f"{seller} : lecture interrompue à {len(snap)} disque(s) — "
                          "l'inventaire complet déjà enregistré est conservé.")
    scat.save_inventory(seller, snap)
    scat.set_inv_meta(seller, {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "n_items": len(snap), "n_listings": len(listings),
        "n_pages": job.st.get("done", 0), "partial": bool(truncated),
        "reading": False})
    suffix = " (lecture interrompue : stock incomplet)" if truncated else ""
    return job.finish(f"{seller} : {len(snap)} disque(s) en vente sur "
                      f"{len(listings)} annonce(s).{suffix}")


def job_scan_catalog(job, params):
    """Parcourt le catalogue partagé de vendeurs Discogs (radar/sellers.py) :
    vérifie chaque compte, prend un snapshot de son inventaire « For Sale »
    (release_id -> prix/état), et compte les nouveautés vs le snapshot précédent.
    Reprenable : on saute les vendeurs scannés il y a moins de `min_age_days`
    sauf si `force`. Le catalogue est partagé -> tourne toujours en tant que owner.
    Calcule aussi, par vendeur, une note d'affinité goût sur son stock 12" (pas
    les 45 tours) via `sellers.seller_affinity` — cf. Ctx.ascore.

    Priorise et espace les rescans avec cette note (impossible de filtrer les
    disques par genre côté API — cf. discussion : l'inventaire Discogs ne
    donne ni genre ni style, seulement artiste/format) : les vendeurs à faible
    affinité goût (déjà scannés, mais peu ou pas d'artiste connu sur leur
    stock 12") sont espacés à 30 jours au lieu de `min_age_days`, et les
    vendeurs jamais scannés ou à bonne affinité passent en premier dans
    l'ordre du scan.
    """
    from radar_web.radar import sellers as scat
    from radar_web.radar.scoring import Ctx

    cfg = cfg_load()
    token = cfg.get("token", "")
    if not token:
        return job.finish(error="Pas de token Discogs.")
    cat = scat.ensure_seeded()
    for u in cfg.get("sellers", []):                # vendeurs suivis en veille
        if u and u not in cat:
            cat[u] = {"name": u, "country": "", "city": "", "focus": "",
                      "type": "veille", "source": "veille", "active": True,
                      "verified": None, "n_items": None, "n_new": None, "last_scan": None}
    scat.save_catalog(cat)
    idx = scat.load_index() if os.path.isfile(scat.INDEX_PATH) else scat.rebuild_index(cat)
    ctx = Ctx(uid="owner")
    force = bool(params.get("force"))
    only = params.get("only")                       # 1 seul vendeur, pour test
    max_pages = int(params.get("max_pages", 60))
    min_age = float(params.get("min_age_days", 6))
    now = datetime.now().isoformat(timespec="seconds")
    now_ts = time.time()

    def _rescan_interval_days(e):
        """30 j pour un vendeur déjà scanné à faible affinité goût (note basse,
        ou du stock 12" mais aucun artiste connu dedans), min_age_days sinon —
        y compris les vendeurs jamais scannés, qui ont besoin d'un 1er passage."""
        if not e.get("last_scan"):
            return min_age
        note = e.get("note")
        low = (note is not None and note < 40) or (note is None and e.get("n_12in"))
        return max(min_age, 30) if low else min_age

    targets = [u for u, e in cat.items()
               if e.get("active") and (u == only if only else True)]
    if not only:
        def _due(u):
            e = cat[u]
            ls = e.get("last_scan")
            if force or not ls:
                return True
            try:
                return datetime.fromisoformat(ls).timestamp() < now_ts - _rescan_interval_days(e) * 86400
            except ValueError:
                return True
        targets = [u for u in targets if _due(u)]
        # priorité : jamais scannés d'abord (1er passage), puis meilleure affinité goût
        targets.sort(key=lambda u: (bool(cat[u].get("last_scan")), -(cat[u].get("note") or 0)))

    job.st["total"] = len(targets)
    if not targets:
        return job.finish("Catalogue à jour — rien à scanner.")
    job.msg(f"{len(targets)} vendeur(s) à scanner")
    total_new = 0

    for u in targets:
        if job.stopped():
            break
        e = cat.setdefault(u, {})
        prev = scat.load_inventory(u)
        snap, page, unreachable, complete = {}, 1, False, False
        while page <= max_pages:
            d = discogs_get(token, f"/users/{u}/inventory",
                            {"status": "For Sale", "per_page": 100, "page": page,
                             "sort": "listed", "sort_order": "desc"})
            if page == 1 and not d:                 # réponse vide = compte KO / 404
                unreachable = True
                break
            got = d.get("listings", [])
            for x in got:
                rel = x.get("release") or {}
                rid = rel.get("id")
                if not rid:
                    continue
                pr = x.get("price") or {}
                snap[str(rid)] = {"listing_id": x.get("id"),
                                  "price": pr.get("value"), "currency": pr.get("currency"),
                                  "condition": x.get("condition"),
                                  "sleeve": x.get("sleeve_condition"),
                                  "listed": x.get("posted"),
                                  "artist": rel.get("artist"), "format": rel.get("format")}
            job.sub(done=len(snap), total=d.get("pagination", {}).get("items", len(snap)), label=u)
            if page >= d.get("pagination", {}).get("pages", 1) or not got:
                complete = True
                break
            if job.stopped():                       # arrêt demandé : on coupe cette pagination
                break                                # sans marquer le vendeur comme scanné (repris au prochain lancement)
            page += 1
            time.sleep(1.1)

        if unreachable:
            fails = int(e.get("fails", 0)) + 1
            e.update(verified=False, fails=fails, last_scan=now)
            if fails >= 2:                          # KO deux fois de suite -> on désactive
                e["active"] = False
            job.msg(f"{u} : inventaire inaccessible ({fails})")
            cat[u] = e
        elif complete:
            n_new = len([r for r in snap if r not in prev])
            scat.save_inventory(u, snap)
            scat.update_index(idx, u, snap)
            e.update(verified=True, fails=0, n_items=len(snap), n_new=n_new, last_scan=now,
                     **scat.seller_affinity(snap, ctx))
            total_new += n_new
            job.msg(f"{u} : {len(snap)} article(s), +{n_new} nouveau(x)")
            cat[u] = e
        else:
            job.msg(f"{u} : arrêté en cours de scan — repris depuis le début au prochain lancement")
        scat.save_catalog(cat)
        scat.save_index(idx)
        job.tick(u)
        time.sleep(0.5)

    scat.save_catalog(cat)
    stopped = job.stopped()
    suffix = " — arrêté, relance le scan pour continuer." if stopped else ""
    job.finish(f"{job.st['done']}/{len(targets)} vendeur(s) scanné(s) · +{total_new} nouveauté(s) au total.{suffix}")
