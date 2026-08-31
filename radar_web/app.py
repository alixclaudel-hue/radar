"""Radar — interface FastAPI + HTMX. Données PARTAGÉES avec l'appli Streamlit.
Lancement :  uvicorn radar_web.app:app --reload --port 8600

Nav : 🧠 Ma patte musicale · 🔍 Recherche ciblée · 📻 Veille Discogs · 🌐 Mon univers · 🎛️ Réglages
"""
import hashlib
import io
import os
import re
import time

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .radar import discogs, jobs, store
from .radar.paths import (CORPUS, PENDING_ENRICH, SELLERS_NEW, SELLERS_SEEN,
                          VEILLE_NEW, VEILLE_SEEN, YOUTUBE_META)
from .radar.scoring import Ctx, real_tracks, yt_search_url
from .radar.store import load, normalize_label, save

HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


def _pl_id(url):
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else ((url or "").strip() or None)


templates.env.filters["pl_id"] = _pl_id
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
SYNC_ALL_JOBS = ["fetch_collection", "ingest_youtube", "ingest_bandcamp", "merge_corpus"]


@app.get("/patte", response_class=HTMLResponse)
def patte_page(request: Request, saved: int = 0):
    c = Ctx()
    pl_urls = [u for u in (c.cfg.get("youtube_playlists") or "").splitlines() if u.strip()]
    return render(request, "pages/patte.html", active="patte", cfg=c.cfg, sc=c.scoring,
                  cats=c.cfg.get("taste_categories", {}), coll=c.collection,
                  pl_urls=pl_urls, pl_meta=load(YOUTUBE_META, {}),
                  src=c.corpus_by_source(), st=c.stats(), saved=saved)


@app.post("/patte")
async def patte_save(request: Request):
    f = await request.form()
    c = _cfg()
    for k in ("token", "youtube_api_key", "bandcamp_sub_user", "bandcamp_sub_pass",
              "djset_sources"):
        if k in f:
            c[k] = f.get(k, "").strip()
    if "yt_pl" in f:
        c["youtube_playlists"] = "\n".join(u.strip() for u in f.getlist("yt_pl") if u.strip())
    cats = c.setdefault("taste_categories", {})
    for cid in ("1", "2"):
        if f"styles_{cid}" in f:
            cats[cid] = [x.strip() for x in f.get(f"styles_{cid}", "").splitlines() if x.strip()]
    store.save_config(c)
    return RedirectResponse("/patte?saved=1", status_code=303)


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
@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request):
    return render(request, "pages/search.html", active="search", q={})


@app.post("/search", response_class=HTMLResponse)
def search_run(request: Request, label: str = Form(""), genre: str = Form(""),
               style: str = Form(""), year_from: str = Form(""), year_to: str = Form(""),
               vinyl: str = Form(""), pages: str = Form("2")):
    c = Ctx()
    token = c.cfg.get("token", "")
    year = f"{year_from}-{year_to}" if year_from and year_to else (year_from or year_to or "")
    fmt = "Vinyl" if vinyl else ""
    try:
        npages = max(1, min(4, int(pages or 2)))
    except ValueError:
        npages = 2
    try:
        if label.strip():
            raw = discogs.search_label_releases(token, label.strip(), genre=genre,
                                                style=style, fmt=fmt, year=year, max_pages=npages)
        else:
            raw = []
            for pg in range(1, npages + 1):
                p = {"per_page": 100, "page": pg, "sort": "year", "sort_order": "desc"}
                for k, v in (("genre", genre), ("style", style), ("year", year), ("format", fmt)):
                    if v:
                        p[k] = v
                d = discogs.search(token=token, **p)
                got = d.get("results", [])
                raw += got
                if pg >= d.get("pagination", {}).get("pages", 1) or not got:
                    break
    except discogs.DiscogsError as e:
        return frag(request, "partials/results.html", error=str(e))
    seen, scored = set(), []
    for r in raw:
        rid = r.get("id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        sc, det = c.album_score(r)
        r["thumb"] = r.get("thumb") or r.get("cover_image")
        scored.append({"raw": r, "score": sc, "detail": det})
    scored.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))
    return frag(request, "partials/results.html", results=scored[:120])


@app.get("/release/{rid}/tracks", response_class=HTMLResponse)
def tracklist(request: Request, rid: int):
    c = Ctx()
    try:
        data = discogs.release(rid, token=c.cfg.get("token", ""))
    except discogs.DiscogsError as e:
        return frag(request, "partials/tracklist.html", error=str(e))
    ra = ", ".join(a.get("name", "") for a in data.get("artists", []))
    tracks = [{"pos": (t.get("position") or "").strip(),
               "title": (t.get("title") or "").strip(),
               "yt": yt_search_url(f"{(', '.join(a.get('name','') for a in t.get('artists', [])) or ra)} {t.get('title','')}")}
              for t in real_tracks(data.get("tracklist", []))]
    return frag(request, "partials/tracklist.html", tracks=tracks)


# ============================================================ 📻 Veille Discogs (+ vendeurs + reco)
def _inbox(request, path, source_key, key_ns, mins=30):
    items = load(path, [])
    if not items:
        return frag(request, "partials/inbox.html", rows=[], key_ns=key_ns, sources=[],
                    mins=mins, n_total=0)
    c = Ctx()
    scored = []
    for it in items:
        sc, _ = c.album_score({"title": f"{it.get('artist','')} - {it.get('title','')}",
                               "label": [it["label"]] if it.get("label") else [],
                               "style": it.get("style") or []})
        scored.append({"it": it, "score": sc, "src": it.get(source_key)})
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
    return render(request, "pages/univers.html", active="univers", tab=tab, cfg=c.cfg,
                  n_labels=len(c.cfg.get("labels", [])),
                  n_profiled=len(c.profile),
                  n_artists=sum(len(v) for v in c.cfg.get("artist_categories", {}).values()),
                  n_sets=len([r for r in c.corpus if r.get("source") == "djset"]))


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


# ============================================================ jobs
VALID_JOBS = {"fetch_collection", "ingest_youtube", "ingest_bandcamp", "merge_corpus",
              "scan_veille", "scan_sellers", "build_graph", "profile_labels",
              "ingest_djsets", "resolve_artists", "canonicalize", "enrich"}
JOB_PARAMS = {"ingest_youtube": {"deep": True}, "ingest_bandcamp": {"deep": True}}


@app.post("/jobs/{name}/launch", response_class=HTMLResponse)
def job_launch(name: str):
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
    if err:
        inner = f"<span class='notice warn small'>{err}</span>"
    elif run:
        inner = (f"<span class='small muted'>⏳ {s.get('message') or ''} · {done}/{s.get('total', 0)}</span>"
                 f"<div class='progress'><i style='width:{pct}%'></i></div>")
    else:
        inner = f"<span class='small muted'>✓ {s.get('message') or 'terminé'}</span>"
    poll = f"hx-get='/jobs/{name}/status' hx-trigger='every 2s' hx-swap='outerHTML'" if run else ""
    return HTMLResponse(f"<div id='job-{name}' {poll}>{inner}</div>")


# ============================================================ 🎛️ Réglages
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0):
    c = _cfg()
    return render(request, "pages/settings.html", active="settings", cfg=c, sc=c["scoring"], saved=saved)


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
