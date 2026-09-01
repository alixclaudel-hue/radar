"""Radar — interface FastAPI + HTMX. Données PARTAGÉES avec l'appli Streamlit.
Lancement :  uvicorn radar_web.app:app --reload --port 8600

Nav : 🧠 Ma patte musicale · 🔍 Recherche ciblée · 📻 Veille Discogs · 🌐 Mon univers · 🎛️ Réglages
"""
import hashlib
import html
import io
import os
import re
import time
from typing import List
from urllib.parse import quote_plus

import requests
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .radar import discogs, jobs, learn, store, vocab
from .radar.paths import (CORPUS, DISCOGS_STATE, PENDING_ENRICH, RELEASE_META,
                          SEARCH_HIST, SELLERS_NEW, SELLERS_SEEN, SPOTIFY_META,
                          VEILLE_NEW, VEILLE_SEEN, YOUTUBE_META)
from .radar.scoring import Ctx, real_tracks, yt_search_url
from .radar.store import load, normalize_label, save

HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


def _pl_id(url):
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else ((url or "").strip() or None)


def _sp_id(url):
    m = re.search(r"playlist[/:]([A-Za-z0-9]+)", url or "")
    return m.group(1) if m else ((url or "").strip() or None)


templates.env.filters["pl_id"] = _pl_id
templates.env.filters["sp_id"] = _sp_id
app = FastAPI(title="Radar")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

AUTH_TTL = 5 * 3600
COOKIE = "radar_auth"
CURRENT_YEAR = time.gmtime().tm_year


# --------------------------------------------------------------------- auth
def _pw():
    return os.environ.get("APP_PASSWORD", "")


def _token(exp):
    exp = int(exp)
    return f"{exp}.{hashlib.sha256(f'{_pw()}|{exp}'.encode()).hexdigest()[:20]}"


def _token_ok(tok):
    try:
        exp = int((tok or "").split(".", 1)[0])
    except ValueError:
        return False
    return time.time() < exp and _token(exp) == tok


def _authed(request):
    return not _pw() or _token_ok(request.cookies.get(COOKIE, ""))


@app.middleware("http")
async def _guard(request: Request, call_next):
    p = request.url.path
    if p.startswith("/static") or p in ("/login", "/health"):
        return await call_next(request)
    if not _authed(request):
        if request.headers.get("hx-request"):
            return HTMLResponse("Session expirée — <a href='/login'>reconnexion</a>", status_code=401)
        return RedirectResponse("/login", status_code=303)
    resp = await call_next(request)
    if _pw() and _token_ok(request.cookies.get(COOKIE, "")):
        resp.set_cookie(COOKIE, _token(time.time() + AUTH_TTL), max_age=AUTH_TTL,
                        httponly=True, samesite="lax")
    return resp


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, bad: int = 0):
    if _authed(request):
        return RedirectResponse("/", status_code=303)
    msg = "<p class='notice warn'>Mot de passe incorrect.</p>" if bad else ""
    return HTMLResponse(f"""<!doctype html><meta charset=utf-8>
<link rel=stylesheet href=/static/app.css><div class=wrap style='max-width:360px'>
<p class=brand style='font-size:32px'>Rada<b>r</b></p>{msg}
<form method=post action=/login>
  <div class=field><label>Mot de passe</label><input type=password name=pw autofocus></div>
  <button class=primary type=submit>Entrer</button>
</form></div>""")


@app.post("/login")
def login(pw: str = Form("")):
    if _pw() and hashlib.sha256(pw.encode()).digest() == hashlib.sha256(_pw().encode()).digest():
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(COOKIE, _token(time.time() + AUTH_TTL), max_age=AUTH_TTL,
                     httponly=True, samesite="lax")
        return r
    return RedirectResponse("/login?bad=1", status_code=303)


# --------------------------------------------------------------------- helpers
def render(request, tpl, **ctx):
    ctx.setdefault("has_token", bool(store.load_config().get("token")))
    return templates.TemplateResponse(request, tpl, {"request": request, **ctx})


def frag(request, tpl, **ctx):
    return templates.TemplateResponse(request, tpl, {"request": request, **ctx})


def _cfg():
    return store.load_config()


# --------------------------------------------------------------------- home
@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/patte", status_code=303)


# ============================================================ 🧠 Mieux connaître ton univers
SYNC_ALL_JOBS = ["fetch_collection", "ingest_youtube", "ingest_spotify",
                 "ingest_bandcamp", "merge_corpus"]


@app.get("/patte", response_class=HTMLResponse)
def patte_page(request: Request, saved: int = 0):
    c = Ctx()
    pl_urls = [u for u in (c.cfg.get("youtube_playlists") or "").splitlines() if u.strip()]
    sp_urls = [u for u in (c.cfg.get("spotify_playlists") or "").splitlines() if u.strip()]
    return render(request, "pages/patte.html", active="patte", cfg=c.cfg, sc=c.scoring,
                  cats=c.cfg.get("taste_categories", {}), coll=c.collection,
                  pl_urls=pl_urls, pl_meta=load(YOUTUBE_META, {}),
                  sp_urls=sp_urls, sp_meta=load(SPOTIFY_META, {}),
                  src=c.corpus_by_source(), st=c.stats(), saved=saved)


def _apply_patte_form(f):
    c = _cfg()
    for k in ("token", "youtube_api_key", "spotify_client_id", "spotify_client_secret",
              "bandcamp_sub_user", "bandcamp_sub_pass", "djset_sources"):
        if k in f:
            c[k] = f.get(k, "").strip()
    if "yt_pl" in f:
        c["youtube_playlists"] = "\n".join(u.strip() for u in f.getlist("yt_pl") if u.strip())
    if "sp_pl" in f:
        c["spotify_playlists"] = "\n".join(u.strip() for u in f.getlist("sp_pl") if u.strip())
    cats = c.setdefault("taste_categories", {})
    for cid in ("1", "2"):
        if f"styles_{cid}" in f:
            cats[cid] = [x.strip() for x in f.get(f"styles_{cid}", "").splitlines() if x.strip()]
    store.save_config(c)


@app.post("/patte")
async def patte_save(request: Request):
    _apply_patte_form(await request.form())
    return RedirectResponse("/patte?saved=1", status_code=303)


@app.post("/patte/run/{job}", response_class=HTMLResponse)
async def patte_run(request: Request, job: str):
    """Enregistre les identifiants/champs saisis PUIS lance le job (pour que le job
    utilise bien ce qui vient d'être tapé, sans étape « Enregistrer » séparée)."""
    _apply_patte_form(await request.form())
    return job_launch(job)


@app.post("/patte/sync-all", response_class=HTMLResponse)
def patte_sync_all():
    n = sum(1 for j in SYNC_ALL_JOBS if jobs.launch(j))
    return HTMLResponse(f"<span class='small ok'>{n} tâche(s) lancée(s) — voir l'avancement par rubrique.</span>")


@app.post("/patte/import-csv", response_class=HTMLResponse)
async def patte_import_csv(request: Request, kind: str = "labels", file: UploadFile = None,
                           replace: str = Form(""), tier: str = Form("2")):
    raw = (await file.read()).decode("utf-8", "ignore") if file else ""
    names = [line.split(",")[0].strip().strip('"')
             for i, line in enumerate(io.StringIO(raw)) if i and line.split(",")[0].strip()]
    c = _cfg()
    if kind == "artists":
        ac = c.setdefault("artist_categories", {"1": [], "2": []})
        t = tier if tier in ("1", "2") else "2"
        have = {normalize_label(x) for cid in ("1", "2") for x in ac.get(cid, [])}
        added = 0
        for n in names:
            if normalize_label(n) not in have:
                have.add(normalize_label(n))
                ac.setdefault(t, []).append(n)
                added += 1
        q = load(PENDING_ENRICH, {})
        q.setdefault("artists", []).extend(names)
        save(PENDING_ENRICH, q)
        store.save_config(c)
        return HTMLResponse(f"✓ {added} artiste(s) ajouté(s) en « {'Cœur' if t == '1' else 'Aimés'} ».")
    if replace:
        c["labels"] = []
    have = {normalize_label(x) for x in c["labels"]}
    added = 0
    for n in names:
        if normalize_label(n) not in have:
            have.add(normalize_label(n))
            c["labels"].append(n)
            added += 1
    store.save_config(c)
    return HTMLResponse(f"✓ {added} label(s) ajouté(s) (base : {len(c['labels'])}).")


# ============================================================ 🔍 Recherche ciblée
SEARCH_MIN_YEAR = 1960


def _year_param(year_from, year_to):
    """Construit 'AAAA-AAAA' pour Discogs ; '' si l'intervalle couvre tout."""
    mx = int(time.strftime("%Y"))
    try:
        a = int(year_from) if str(year_from).strip() else SEARCH_MIN_YEAR
        b = int(year_to) if str(year_to).strip() else mx
    except ValueError:
        return ""
    a, b = min(a, b), max(a, b)
    a, b = max(SEARCH_MIN_YEAR, a), min(mx, b)
    if a <= SEARCH_MIN_YEAR and b >= mx:
        return ""
    return f"{a}-{b}"


def _search_styles(c):
    """Styles proposés : d'abord les catégories de goût de l'utilisateur
    (noms canoniques), puis le reste du vocabulaire Discogs."""
    cats = c.cfg.get("taste_categories", {})
    mine, seen = [], set()
    for cid in ("1", "2"):
        for s in cats.get(cid, []):
            s = (s or "").strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                mine.append(s)
    more = [s for s in vocab.STYLES if s.lower() not in seen]
    return mine, more


SEARCH_HIST_MAX = 20


def _hist_summary(p):
    bits = []
    if p.get("label"):
        bits.append(p["label"])
    if p.get("genre"):
        bits.append(" / ".join(p["genre"]))
    if p.get("style"):
        bits.append(" / ".join(p["style"]))
    yr = _year_param(p.get("year_from", ""), p.get("year_to", ""))
    if yr:
        bits.append(yr)
    if p.get("vinyl"):
        bits.append("vinyle")
    return " · ".join(bits) or "tous filtres vides"


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, sid: str = ""):
    c = Ctx()
    styles_mine, styles_more = _search_styles(c)
    hist = load(SEARCH_HIST, [])
    entry = next((e for e in hist if e.get("id") == sid), hist[0] if hist else None)
    return render(request, "pages/search.html", active="search",
                  q=(entry or {}).get("params", {}), last_id=(entry or {}).get("id", ""),
                  history=[{"id": e["id"], "ts": e.get("ts", ""), "n": e.get("n", 0),
                            "summary": _hist_summary(e.get("params", {}))} for e in hist],
                  genres=vocab.GENRES, styles_mine=styles_mine, styles_more=styles_more,
                  year_min=SEARCH_MIN_YEAR, year_max=int(time.strftime("%Y")))


@app.get("/search/replay/{sid}", response_class=HTMLResponse)
def search_replay(request: Request, sid: str):
    entry = next((e for e in load(SEARCH_HIST, []) if e.get("id") == sid), None)
    if not entry:
        return frag(request, "partials/results.html", results=[])
    return frag(request, "partials/results.html", results=entry.get("results", []))


def _base_labels_ranked(c):
    """Labels de la base -> nom canonique + affinité. Tri : affinité de style
    décroissante, départagée par le score de reco (collection + corpus + artistes),
    puis alpha. Dédoublonné par nom canonique."""
    ridx = c.reco_index
    rows, seen = [], set()
    for name in c.cfg.get("labels", []):
        key = store.normalize_label(name)
        res = c.resolved.get(key) or {}
        disp = res.get("discogs_name") or res.get("original") or name
        aff = c.affinity_score(c.profile.get(key)) if c.profile.get(key) else None
        rows.append({"disp": disp, "norm": store.normalize_label(disp),
                     "aff": aff, "_reco": ridx.get(key, 0)})
    rows.sort(key=lambda r: (r["aff"] is None, -(r["aff"] or 0), -r["_reco"], r["disp"].lower()))
    uniq = []
    for r in rows:
        if r["norm"] in seen:
            continue
        seen.add(r["norm"])
        uniq.append(r)
    return uniq


@app.get("/search/labels", response_class=HTMLResponse)
def search_labels(request: Request, label: str = "", q: str = ""):
    c = Ctx()
    term = store.normalize_label(label or q)
    ranked = _base_labels_ranked(c)
    matched = [r for r in ranked if term and term in r["norm"]]
    no_match = bool(term) and not matched
    if matched:
        header = f"{len(matched)} correspondance" + ("s" if len(matched) > 1 else "")
    elif no_match:
        header = "Aucun label reconnu — ta base, classée par affinité"
    else:
        header = "Tes labels, classés par affinité"
    return frag(request, "partials/label_suggest.html",
                rows=(matched or ranked)[:60], header=header)


@app.post("/search", response_class=HTMLResponse)
def search_run(request: Request, label: str = Form(""),
               genre: List[str] = Form(default=[]), style: List[str] = Form(default=[]),
               year_from: str = Form(""), year_to: str = Form(""),
               vinyl: str = Form(""), pages: str = Form("2")):
    c = Ctx()
    token = c.cfg.get("token", "")
    year = _year_param(year_from, year_to)
    fmt = "Vinyl" if vinyl else ""
    genres = [g.strip() for g in genre if g and g.strip()]
    styles = [s.strip() for s in style if s and s.strip()]
    try:
        npages = max(1, min(4, int(pages or 2)))
    except ValueError:
        npages = 2
    # produit genres × styles (sémantique OU) ; borné pour ménager l'API
    combos = [(g, s) for g in (genres or [""]) for s in (styles or [""])][:8]
    raw, seen_ids = [], set()
    try:
        for i, (g, s) in enumerate(combos):
            if i:
                time.sleep(1.0)
            if label.strip():
                part = discogs.search_label_releases(token, label.strip(), genre=g,
                                                     style=s, fmt=fmt, year=year, max_pages=npages)
            else:
                part = []
                for pg in range(1, npages + 1):
                    p = {"per_page": 100, "page": pg, "sort": "year", "sort_order": "desc"}
                    for k, v in (("genre", g), ("style", s), ("year", year), ("format", fmt)):
                        if v:
                            p[k] = v
                    d = discogs.search(token=token, **p)
                    got = d.get("results", [])
                    part += got
                    if pg >= d.get("pagination", {}).get("pages", 1) or not got:
                        break
            for r in part:
                rid = r.get("id")
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    raw.append(r)
    except discogs.DiscogsError as e:
        return frag(request, "partials/results.html", error=str(e))
    seen, scored = set(), []
    for r in raw:
        rid = r.get("id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        sc, det = c.album_score(r)
        thumb = r.get("cover_image") or r.get("thumb")
        lab1 = next((x for x in (r.get("label") or []) if x), "")
        scored.append({"raw": {"id": rid, "title": r.get("title", ""), "label1": lab1,
                               "style": r.get("style") or [], "catno": r.get("catno", ""),
                               "year": r.get("year", ""), "thumb": thumb, "uri": r.get("uri", "")},
                       "score": sc,
                       "detail": {"label": det.get("label"), "artist": det.get("artist"),
                                  "style": det.get("style")}})
    scored.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))
    scored = scored[:48]
    params = {"label": label.strip(), "genre": genres, "style": styles,
              "year_from": year_from.strip(), "year_to": year_to.strip(),
              "vinyl": bool(vinyl), "pages": npages}
    hist = [e for e in load(SEARCH_HIST, []) if e.get("params") != params]
    hist.insert(0, {"id": hashlib.md5(f"{time.time()}{params}".encode()).hexdigest()[:10],
                    "ts": time.strftime("%Y-%m-%d %H:%M"), "params": params,
                    "n": len(scored), "results": scored})
    save(SEARCH_HIST, hist[:SEARCH_HIST_MAX])
    return frag(request, "partials/results.html", results=scored)


def _toks(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


@app.get("/release/{rid}/tracks", response_class=HTMLResponse)
def tracklist(request: Request, rid: int):
    c = Ctx()
    try:
        data = discogs.release(rid, token=c.cfg.get("token", ""))
    except discogs.DiscogsError as e:
        return frag(request, "partials/tracklist.html", error=str(e))
    ra = ", ".join(a.get("name", "") for a in data.get("artists", []))
    labels = data.get("labels") or []
    label1 = (labels[0].get("name") if labels and isinstance(labels[0], dict) else "") or ""
    year = data.get("year") or ""
    videos = [{"uri": v.get("uri"), "tok": _toks(v.get("title"))}
              for v in (data.get("videos") or []) if v.get("uri")]
    rows = []
    for t in real_tracks(data.get("tracklist", [])):
        ttl = (t.get("title") or "").strip()
        tart = ", ".join(a.get("name", "") for a in t.get("artists", [])) or ra
        want = _toks(f"{tart} {ttl}")
        best, best_sc = None, 0.0
        for v in videos:
            if not v["tok"] or not want:
                continue
            sc = len(want & v["tok"]) / len(want)
            if sc > best_sc:
                best, best_sc = v, sc
        if best and best_sc >= 0.55:
            play, kind = best["uri"], "discogs"
        else:
            q = " ".join(x for x in (tart, ttl, label1, str(year)) if x)
            play, kind = "/yt/first?q=" + quote_plus(q), "yt"
        rows.append({"pos": (t.get("position") or "").strip(), "title": ttl,
                     "play": play, "kind": kind})
    return frag(request, "partials/tracklist.html", tracks=rows)


@app.get("/yt/first")
def yt_first(q: str = ""):
    """Redirige vers LA vidéo YouTube la plus pertinente (API Data si clé dispo),
    sinon vers la page de résultats YouTube."""
    q = (q or "").strip()
    if not q:
        return RedirectResponse("https://www.youtube.com", status_code=302)
    key = _cfg().get("youtube_api_key", "")
    if key:
        try:
            r = requests.get("https://www.googleapis.com/youtube/v3/search",
                             params={"part": "id", "type": "video", "maxResults": 1,
                                     "q": q, "key": key}, timeout=12)
            if r.ok:
                items = r.json().get("items", [])
                vid = (items[0].get("id", {}) or {}).get("videoId") if items else ""
                if vid:
                    return RedirectResponse(f"https://www.youtube.com/watch?v={vid}",
                                            status_code=302)
        except (requests.RequestException, ValueError, KeyError, IndexError):
            pass
    return RedirectResponse(yt_search_url(q), status_code=302)


RELEASE_META_TTL = 86400


@app.get("/release/{rid}/meta", response_class=HTMLResponse)
def release_meta(request: Request, rid: int):
    cache = load(RELEASE_META, {})
    ent = cache.get(str(rid))
    if not ent or time.time() - ent.get("ts", 0) > RELEASE_META_TTL:
        token = _cfg().get("token", "")
        rating = rcount = nfs = low = None
        try:
            d = discogs.release(rid, token=token)
            rt = (d.get("community") or {}).get("rating") or {}
            rating, rcount = rt.get("average"), rt.get("count")
            nfs, low = d.get("num_for_sale"), d.get("lowest_price")
        except discogs.DiscogsError:
            pass
        ent = {"ts": time.time(), "rating": rating, "rcount": rcount,
               "nfs": nfs, "low": low}
        cache[str(rid)] = ent
        if len(cache) > 4000:
            for k in sorted(cache, key=lambda k: cache[k].get("ts", 0))[:1200]:
                cache.pop(k, None)
        save(RELEASE_META, cache)
    return frag(request, "partials/release_meta.html", m=ent, rid=rid)


# ============================================================ 📻 Veille Discogs (+ vendeurs + reco)
def _inbox(request, path, source_key, key_ns, mins=30):
    items = load(path, [])
    if not items:
        return frag(request, "partials/inbox.html", rows=[], key_ns=key_ns, sources=[],
                    mins=mins, n_total=0)
    c = Ctx()
    scored = []
    for it in items:
        sc, det = c.album_score({"title": f"{it.get('artist','')} - {it.get('title','')}",
                                 "label": [it["label"]] if it.get("label") else [],
                                 "style": it.get("style") or []})
        scored.append({"it": it, "score": sc, "det": det, "src": it.get(source_key)})
    scored.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))
    rows = [r for r in scored if (r["score"] or 0) >= mins]
    return frag(request, "partials/inbox.html", rows=rows[:150], key_ns=key_ns, mins=mins,
                sources=sorted({r["src"] for r in scored if r["src"]}), n_total=len(items))


@app.get("/inbox/{kind}", response_class=HTMLResponse)
def inbox(request: Request, kind: str, mins: int = 30):
    if kind == "veille":
        return _inbox(request, VEILLE_NEW, "rule", "veille", mins)
    return _inbox(request, SELLERS_NEW, "seller", "sellers", mins)


@app.post("/inbox/{kind}/clear", response_class=HTMLResponse)
def inbox_clear(request: Request, kind: str):
    save(VEILLE_NEW if kind == "veille" else SELLERS_NEW, [])
    return inbox(request, kind)


@app.post("/inbox/{kind}/dismiss", response_class=HTMLResponse)
def inbox_dismiss(request: Request, kind: str, rid: str = Form("")):
    path = VEILLE_NEW if kind == "veille" else SELLERS_NEW
    idf = "release_id" if kind == "veille" else "listing_id"
    save(path, [x for x in load(path, []) if str(x.get(idf)) != rid])
    return inbox(request, kind)


@app.get("/veille", response_class=HTMLResponse)
def veille_page(request: Request):
    c = _cfg()
    return render(request, "pages/veille.html", active="veille",
                  rules=c.get("veille_rules", []), watchlist=c.get("watchlist", []),
                  sellers=c.get("sellers", []), year=CURRENT_YEAR,
                  v_last=max((v.get("last_scan", "") for v in load(VEILLE_SEEN, {}).values()), default=""),
                  s_last=max((v.get("last_scan", "") for v in load(SELLERS_SEEN, {}).values()), default=""))


@app.post("/veille/rules")
async def veille_rules_save(request: Request):
    f = await request.form()
    c = _cfg()
    rules = c.setdefault("veille_rules", [])
    act = f.get("_action", "")
    if act == "add":
        rules.append({"name": "Nouvelle règle", "active": True, "styles": [], "genres": ["Electronic"],
                      "year_from": 2000, "year_to": CURRENT_YEAR, "labels": [], "artists": [],
                      "vinyl_only": True})
    elif act.startswith("del:"):
        i = int(act[4:])
        if 0 <= i < len(rules):
            rules.pop(i)
    else:
        for i, r in enumerate(rules):
            r["name"] = f.get(f"name_{i}", r.get("name", ""))
            r["active"] = f.get(f"active_{i}") == "on"
            r["vinyl_only"] = f.get(f"vinyl_{i}") == "on"
            for key in ("styles", "genres", "labels", "artists"):
                r[key] = [x.strip() for x in f.get(f"{key}_{i}", "").splitlines() if x.strip()]
            try:
                r["year_from"] = int(f.get(f"yf_{i}") or r.get("year_from") or 2000)
                r["year_to"] = int(f.get(f"yt_{i}") or r.get("year_to") or CURRENT_YEAR)
            except ValueError:
                pass
    store.save_config(c)
    return RedirectResponse("/veille", status_code=303)


@app.post("/sellers/add")
def sellers_add(name: str = Form("")):
    c = _cfg()
    for n in re.split(r"[,\s]+", name.strip()):
        n = n.strip().strip("@/")
        m = re.search(r"/seller/([^/]+)", n)
        if m:
            n = m.group(1)
        if n and n not in c.setdefault("sellers", []):
            c["sellers"].append(n)
    store.save_config(c)
    return RedirectResponse("/veille", status_code=303)


@app.post("/sellers/remove")
def sellers_remove(name: str = Form("")):
    c = _cfg()
    c["sellers"] = [s for s in c.get("sellers", []) if s != name]
    store.save_config(c)
    return RedirectResponse("/veille", status_code=303)


@app.post("/reco/label", response_class=HTMLResponse)
def reco_label(name: str = Form(""), dest: str = Form("base")):
    c = _cfg()
    name = name.strip()
    if name:
        nk = normalize_label(name)
        if dest in ("base", "both") and nk not in {normalize_label(x) for x in c["labels"]}:
            c["labels"].append(name)
        if dest in ("veille", "both") and nk not in {normalize_label(x) for x in c.get("watchlist", [])}:
            c.setdefault("watchlist", []).append(name)
        q = load(PENDING_ENRICH, {})
        q.setdefault("labels", []).append(name)
        save(PENDING_ENRICH, q)
        store.save_config(c)
    return HTMLResponse("<span class='small muted'>✓ ajouté</span>")


@app.post("/reco/artist", response_class=HTMLResponse)
def reco_artist(name: str = Form(""), tier: str = Form("2")):
    c = _cfg()
    name = name.strip()
    if name and tier in ("1", "2"):
        ac = c.setdefault("artist_categories", {"1": [], "2": []})
        nk = normalize_label(name)
        if nk not in {normalize_label(x) for cid in ("1", "2") for x in ac.get(cid, [])}:
            ac.setdefault(tier, []).append(name)
            q = load(PENDING_ENRICH, {})
            q.setdefault("artists", []).append(name)
            save(PENDING_ENRICH, q)
            store.save_config(c)
    return HTMLResponse("<span class='small muted'>✓ ajouté</span>")


# --------------------------------------------------------------------- recos (dans Mon univers)
@app.get("/univers/reco/labels", response_class=HTMLResponse)
def reco_labels_frag(request: Request):
    c = Ctx()
    base = {normalize_label(x) for x in c.cfg.get("labels", [])}
    watch = {normalize_label(x) for x in c.cfg.get("watchlist", [])}
    rows = []
    for r in c.reco_rows():
        if r["key"] not in base:
            r["watched"] = r["key"] in watch
            rows.append(r)
    # candidats issus du graphe (labels où tes artistes ont sorti, absents de la base)
    graph_rows = []
    for lk, v in c.graph_rescore()["labels"].items():
        if lk in base:
            continue
        graph_rows.append({"name": v["name"], "score": round(v["score"]), "aff": None,
                           "owned": 0, "want": 0, "corpus": 0, "artists": v["n_seeds"],
                           "watched": lk in watch,
                           "seeds": ", ".join(v["seeds"])})
    return frag(request, "partials/reco_labels.html", reco=rows[:30],
                graph_reco=graph_rows[:30], n_reco=len(rows), n_graph=len(graph_rows))


@app.get("/univers/reco/artists", response_class=HTMLResponse)
def reco_artists_frag(request: Request):
    c = Ctx()
    g = c.graph_rescore()["artists"]
    disp = {}
    for cid, names in c.cfg.get("artist_categories", {}).items():
        for n in names:
            disp[c.canon_artist_key(n)] = c.canon_artist_name(n)
    rows = [{"name": v["name"], "prox": round(v["score"]), "note": c.ascore.get(k, 0),
             "why": ", ".join(v["why"])}
            for k, v in list(g.items())[:40] if not str(v["name"]).startswith("id:")]
    return frag(request, "partials/reco_artists.html", reco=rows, n_reco=len(g))


# ============================================================ 🌐 Mon univers
@app.get("/univers", response_class=HTMLResponse)
def univers_page(request: Request, tab: str = "labels"):
    c = Ctx()
    g = c.graph or {}
    ac = c.cfg.get("artist_categories", {})
    return render(request, "pages/univers.html", active="univers", tab=tab, cfg=c.cfg,
                  n_labels=len(c.cfg.get("labels", [])), n_profiled=len(c.profile),
                  n_artists=sum(len(v) for v in ac.values()),
                  n_sets=len([r for r in c.corpus if r.get("source") == "djset"]),
                  graph_meta={"built_at": g.get("built_at", ""), "mode": g.get("mode", ""),
                              "n_seeds": len(g.get("seeds", {})), "n_edges": len(g.get("edges", {}))},
                  coeur="\n".join(ac.get("1", [])),
                  coeur_aimes="\n".join(ac.get("1", []) + ac.get("2", [])))


@app.get("/univers/labels", response_class=HTMLResponse)
def univers_labels(request: Request, flt: str = ""):
    c = _cfg()
    labs = c.get("labels", [])
    shown = [l for l in labs if flt.lower() in l.lower()] if flt else labs
    return frag(request, "partials/labels_list.html", total=len(labs), shown=shown[:200],
                n_shown=len(shown), flt=flt)


@app.post("/univers/labels/add", response_class=HTMLResponse)
def univers_labels_add(request: Request, name: str = Form("")):
    c = _cfg()
    name = name.strip()
    if name and normalize_label(name) not in {normalize_label(x) for x in c["labels"]}:
        c["labels"].append(name)
        q = load(PENDING_ENRICH, {})
        q.setdefault("labels", []).append(name)
        save(PENDING_ENRICH, q)
        store.save_config(c)
    return univers_labels(request)


@app.post("/univers/labels/remove", response_class=HTMLResponse)
def univers_labels_remove(request: Request, name: str = Form("")):
    c = _cfg()
    c["labels"] = [l for l in c["labels"] if l != name]
    store.save_config(c)
    return univers_labels(request)


@app.get("/univers/artists/table", response_class=HTMLResponse)
def univers_artists_table(request: Request, flt: str = "", hide: str = ""):
    c = Ctx()
    disp, tiers, asc = c.artist_disp(), c.artist_tier_map(), c.ascore
    catn = {"1": "Cœur", "2": "Aimé", None: "—"}
    rows = []
    for ck in set(asc) | set(tiers):
        name = disp.get(ck, ck)
        if str(name).startswith("id:"):
            continue
        t = tiers.get(ck)
        if hide and t:
            continue
        if flt and flt.lower() not in str(name).lower():
            continue
        note = asc.get(ck, 0)
        if not t and note == 0:
            continue
        rows.append({"name": name, "note": note, "cat": catn.get(t, "—")})
    rows.sort(key=lambda r: -r["note"])
    return frag(request, "partials/artists_table.html", rows=rows[:250], n=len(rows))


@app.post("/univers/artist/set", response_class=HTMLResponse)
def univers_artist_set(name: str = Form(""), cat: str = Form("")):
    c = _cfg()
    ac = c.setdefault("artist_categories", {"1": [], "2": []})
    ck = normalize_label(name)
    for cid in ("1", "2"):
        ac[cid] = [x for x in ac.get(cid, []) if normalize_label(x) != ck]
    tgt = {"Cœur": "1", "Aimé": "2"}.get(cat)
    if tgt:
        ac.setdefault(tgt, []).append(name)
        q = load(PENDING_ENRICH, {})
        q.setdefault("artists", []).append(name)
        save(PENDING_ENRICH, q)
    store.save_config(c)
    return HTMLResponse("<span class='small ok'>✓</span>")


@app.post("/univers/graph/build", response_class=HTMLResponse)
async def univers_graph_build(request: Request):
    f = await request.form()
    mode = f.get("mode", "top")
    try:
        pages = max(1, min(3, int(f.get("pages") or 2)))
    except ValueError:
        pages = 2
    params = {"pages": pages, "incremental": f.get("incremental") == "on"}
    if mode == "global":
        params["mode"] = "global"
    elif mode == "seeds":
        params["seed_names"] = [x.strip() for x in f.get("seed_names", "").splitlines() if x.strip()]
    else:
        params["mode"] = "top"
        try:
            params["seeds"] = max(5, min(400, int(f.get("top_n") or 40)))
        except ValueError:
            params["seeds"] = 40
    jobs.launch("build_graph", params)
    return job_status_frag("build_graph")


@app.get("/univers/sets", response_class=HTMLResponse)
def univers_sets(request: Request, dj: str = "", mins: int = 0):
    c = Ctx()
    by_dj = c.djset_rows()
    djs = sorted(by_dj)
    if dj:
        by_dj = {dj: by_dj.get(dj, {})}
    out = []
    for d in sorted(by_dj):
        vids = []
        for vid, tracks in by_dj[d].items():
            tr = sorted([t for t in tracks if (t.get("_score") or 0) >= mins],
                        key=lambda x: -(x.get("_score") or -1))
            if not tr:
                continue
            vids.append({"vid": vid, "title": tr[0].get("set_title") or vid, "tracks": tr,
                         "best": max((t.get("_score") or 0 for t in tr), default=0)})
        if vids:
            out.append({"dj": d, "vids": sorted(vids, key=lambda v: -v["best"]),
                        "n": sum(len(v["tracks"]) for v in vids)})
    return frag(request, "partials/sets.html", djs=djs, groups=out, dj=dj, mins=mins)


@app.post("/univers/labels/import", response_class=HTMLResponse)
async def univers_labels_import(request: Request, file: UploadFile, replace: str = Form("")):
    raw = (await file.read()).decode("utf-8", "ignore")
    names = [line.split(",")[0].strip().strip('"')
             for i, line in enumerate(io.StringIO(raw)) if i and line.split(",")[0].strip()]
    c = _cfg()
    if replace:
        c["labels"] = []
    have = {normalize_label(x) for x in c["labels"]}
    for n in names:
        if normalize_label(n) not in have:
            have.add(normalize_label(n))
            c["labels"].append(n)
    store.save_config(c)
    return univers_labels(request)


def _csv_cell(s):
    s = str(s).replace('"', '""')
    return f'"{s}"'


@app.get("/univers/labels/export")
def univers_labels_export():
    lines = ["name"] + [_csv_cell(l) for l in _cfg().get("labels", [])]
    return Response("\n".join(lines) + "\n", media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=radar_labels.csv"})


@app.get("/univers/artists/export")
def univers_artists_export():
    ac = _cfg().get("artist_categories", {})
    rows = ["name,categorie"]
    for cid, cat in (("1", "Coeur"), ("2", "Aime")):
        for n in ac.get(cid, []):
            rows.append(f"{_csv_cell(n)},{cat}")
    return Response("\n".join(rows) + "\n", media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=radar_artists.csv"})


# ============================================================ jobs
VALID_JOBS = {"fetch_collection", "ingest_youtube", "ingest_spotify", "ingest_bandcamp",
              "merge_corpus", "scan_veille", "scan_sellers", "build_graph", "profile_labels",
              "ingest_djsets", "resolve_artists", "canonicalize", "enrich", "market_fr"}
JOB_PARAMS = {"ingest_youtube": {"deep": True}, "ingest_spotify": {"deep": True},
              "ingest_bandcamp": {"deep": True}}


@app.post("/jobs/{name}/launch", response_class=HTMLResponse)
def job_launch(name: str):
    if name == "ingest_djsets":
        srcs = [s.strip() for s in (_cfg().get("djset_sources") or "").splitlines() if s.strip()]
        if not srcs:
            return HTMLResponse("<div id='job-ingest_djsets' class='notice warn small'>"
                                "Aucune source DJ — renseigne-les dans « Mieux connaître ton univers → DJ sets ».</div>")
        save(os.path.join(store.paths.JOBS_DIR, "djsets.input.json"),
             {"sources": srcs, "max_per_source": 25, "min_minutes": 35,
              "require_hint": True, "deep": True})
    if name in VALID_JOBS:
        jobs.launch(name, JOB_PARAMS.get(name, {}))
    return job_status_frag(name)


@app.get("/jobs/{name}/status", response_class=HTMLResponse)
def job_status_frag(name: str):
    s = jobs.status(name)
    if not s:
        return HTMLResponse(f"<span id='job-{name}' class='muted small'>—</span>")
    done, total = s.get("done", 0), s.get("total", 0) or 1
    pct = min(100, round(100 * done / total))
    run, err = s.get("running"), s.get("error")
    msg = html.escape(str(s.get("message") or ""))
    if err:
        inner = f"<span class='notice warn small'>{html.escape(str(err))}</span>"
    elif run:
        inner = (f"<span class='small muted'>⏳ {msg} · {done}/{s.get('total', 0)}</span>"
                 f"<div class='progress'><i style='width:{pct}%'></i></div>")
    else:
        inner = f"<span class='small muted'>✓ {msg or 'terminé'}</span>"
    poll = f"hx-get='/jobs/{name}/status' hx-trigger='every 2s' hx-swap='outerHTML'" if run else ""
    return HTMLResponse(f"<div id='job-{name}' {poll}>{inner}</div>")


# ============================================================ 🎛️ Réglages
def _discogs_sess():
    try:
        mt = os.stat(DISCOGS_STATE).st_mtime
    except OSError:
        return {"present": False}
    cookies = (load(DISCOGS_STATE, {}) or {}).get("cookies", [])
    exp = [c["expires"] for c in cookies
           if c.get("name") in ("sgp", "session") and (c.get("expires") or 0) > 0]
    return {"present": True, "cookies": len(cookies),
            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(mt)),
            "expires": time.strftime("%Y-%m-%d", time.localtime(min(exp))) if exp else ""}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0):
    c = _cfg()
    return render(request, "pages/settings.html", active="settings", cfg=c, sc=c["scoring"],
                  saved=saved, sess=_discogs_sess())


@app.post("/settings/discogs-session", response_class=HTMLResponse)
async def discogs_session_set(request: Request, file: UploadFile = None):
    ok, msg = False, "Aucun fichier."
    if file is not None:
        import json as _json
        try:
            d = _json.loads((await file.read()).decode("utf-8", "ignore"))
            n = len(d.get("cookies", []))
            if n and any("discogs.com" in c.get("domain", "") for c in d["cookies"]):
                save(DISCOGS_STATE, d)
                ok, msg = True, f"Session chargée — {n} cookies."
            else:
                msg = "Pas de cookie discogs.com dans ce fichier."
        except ValueError:
            msg = "JSON invalide."
    return frag(request, "partials/discogs_session.html", sess=_discogs_sess(), msg=msg, ok=ok)


@app.post("/settings/discogs-session/clear", response_class=HTMLResponse)
def discogs_session_clear(request: Request):
    try:
        os.remove(DISCOGS_STATE)
    except OSError:
        pass
    return frag(request, "partials/discogs_session.html", sess=_discogs_sess(),
                msg="Session supprimée.", ok=True)


@app.post("/feedback", response_class=HTMLResponse)
def feedback(kind: str = Form("album"), key: str = Form(""), name: str = Form(""),
             verdict: str = Form(""), score: str = Form(""),
             f_label: str = Form("0"), f_artist: str = Form("0"), f_style: str = Form("0"),
             f_collection: str = Form("0"), f_corpus: str = Form("0"), f_affinity: str = Form("0")):
    feat = {"label": f_label, "artist": f_artist, "style": f_style,
            "collection": f_collection, "corpus": f_corpus, "affinity": f_affinity}
    try:
        sc = int(float(score)) if score else None
    except ValueError:
        sc = None
    learn.log(kind, key, name, verdict, sc, {k: float(v or 0) for k, v in feat.items()})
    return HTMLResponse("<span class='small ok'>👍 noté</span>" if verdict == "up"
                        else "<span class='small ok'>👎 noté</span>")


@app.get("/settings/learn", response_class=HTMLResponse)
def settings_learn(request: Request):
    return frag(request, "partials/learn.html", data=learn.summary(_cfg()["scoring"]))


@app.post("/settings/learn/apply")
def settings_learn_apply(kind: str = Form("")):
    d = learn.summary(_cfg()["scoring"]).get(kind)
    if d and d.get("proposal"):
        c = _cfg()
        for row in d["proposal"]:
            c["scoring"][d["subkey"]][row["k"]] = row["new"]
        store.save_config(c)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings")
async def settings_save(request: Request):
    f = await request.form()
    c = _cfg()
    sc = c["scoring"]
    for grp, keys in (("reco", ("collection", "corpus", "artist", "affinity", "want_factor")),
                      ("album", ("label", "artist", "style", "artist_max_vs_mean")),
                      ("artist_score", ("manual", "corpus", "collection", "graph", "djset"))):
        for key in keys:
            v = f.get(f"{grp}__{key}")
            if v not in (None, ""):
                try:
                    sc[grp][key] = float(v)
                except ValueError:
                    pass
    for t in ("1", "2"):
        if f.get(f"artist_tiers__{t}"):
            sc["artist_tiers"][t] = float(f[f"artist_tiers__{t}"])
    for t in ("1", "2", "3"):
        if f.get(f"taste_tiers__{t}"):
            sc["taste_tiers"][t] = float(f[f"taste_tiers__{t}"])
    if f.get("label_affinity_floor") not in (None, ""):
        sc["label_affinity_floor"] = int(float(f["label_affinity_floor"]))
    store.save_config(c)
    return RedirectResponse("/settings?saved=1", status_code=303)
