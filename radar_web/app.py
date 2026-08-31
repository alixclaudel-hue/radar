"""Radar — interface FastAPI + HTMX (parité en cours avec l'appli Streamlit).

Lancement :  uvicorn radar_web.app:app --reload --port 8600
Données PARTAGÉES avec l'appli Streamlit (même CRATE_DATA_DIR)."""
import hashlib
import io
import os
import time

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .radar import discogs, jobs, store
from .radar.paths import CORPUS, SELLERS_NEW, VEILLE_NEW, VEILLE_SEEN, SELLERS_SEEN
from .radar.scoring import Ctx, real_tracks, yt_search_url
from .radar.store import load, save, normalize_label

HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
app = FastAPI(title="Radar")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

AUTH_TTL = 5 * 3600
COOKIE = "radar_auth"
STUB = {"artists": "Mes artistes", "sets": "Mes sets"}


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


def _authed(request: Request) -> bool:
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


def _cfg():
    return store.load_config()


def _save_cfg(cfg):
    store.save_config(cfg)


CURRENT_YEAR = time.gmtime().tm_year


# --------------------------------------------------------------------- home / search
@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/search", status_code=303)


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
        return templates.TemplateResponse(request, "partials/results.html",
                                          {"request": request, "error": str(e)})
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
    return templates.TemplateResponse(request, "partials/results.html",
                                      {"request": request, "results": scored[:120]})


@app.get("/release/{rid}/tracks", response_class=HTMLResponse)
def tracklist(request: Request, rid: int):
    c = Ctx()
    try:
        data = discogs.release(rid, token=c.cfg.get("token", ""))
    except discogs.DiscogsError as e:
        return templates.TemplateResponse(request, "partials/tracklist.html",
                                          {"request": request, "error": str(e)})
    rel_artist = ", ".join(a.get("name", "") for a in data.get("artists", []))
    tracks = [{"pos": (t.get("position") or "").strip(),
               "title": (t.get("title") or "").strip(),
               "yt": yt_search_url(f"{(', '.join(a.get('name','') for a in t.get('artists', [])) or rel_artist)} {t.get('title','')}")}
              for t in real_tracks(data.get("tracklist", []))]
    return templates.TemplateResponse(request, "partials/tracklist.html",
                                      {"request": request, "tracks": tracks})


# --------------------------------------------------------------------- Réglages
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0):
    c = _cfg()
    return render(request, "pages/settings.html", active="settings", cfg=c,
                  sc=c["scoring"], saved=saved)


@app.post("/settings")
async def settings_save(request: Request):
    f = await request.form()
    c = _cfg()
    for k in ("token", "youtube_api_key", "youtube_playlists",
              "bandcamp_sub_user", "bandcamp_sub_pass", "djset_sources"):
        if k in f:
            c[k] = f.get(k, "").strip()
    sc = c["scoring"]
    for grp, keys in (("reco", ("collection", "corpus", "artist", "affinity", "want_factor")),
                      ("album", ("label", "artist", "style", "artist_max_vs_mean")),
                      ("artist_score", ("manual", "corpus", "collection", "graph", "djset"))):
        for key in keys:
            fld = f.get(f"{grp}__{key}")
            if fld not in (None, ""):
                try:
                    sc[grp][key] = float(fld)
                except ValueError:
                    pass
    for t in ("1", "2"):
        v = f.get(f"artist_tiers__{t}")
        if v:
            sc["artist_tiers"][t] = float(v)
    for t in ("1", "2", "3"):
        v = f.get(f"taste_tiers__{t}")
        if v:
            sc["taste_tiers"][t] = float(v)
    fl = f.get("label_affinity_floor")
    if fl not in (None, ""):
        sc["label_affinity_floor"] = int(float(fl))
    _save_cfg(c)
    return RedirectResponse("/settings?saved=1", status_code=303)


# --------------------------------------------------------------------- Mes labels
def _labels_view(cfg, flt="", cap=200):
    labs = cfg.get("labels", [])
    if flt:
        f = flt.lower()
        shown = [l for l in labs if f in l.lower()]
    else:
        shown = labs
    return {"total": len(labs), "shown": shown[:cap], "n_shown": len(shown), "flt": flt}


@app.get("/labels", response_class=HTMLResponse)
def labels_page(request: Request, flt: str = ""):
    return render(request, "pages/labels.html", active="labels", **_labels_view(_cfg(), flt))


@app.get("/labels/list", response_class=HTMLResponse)
def labels_list(request: Request, flt: str = ""):
    return templates.TemplateResponse(request, "partials/labels_list.html",
                                      {"request": request, **_labels_view(_cfg(), flt)})


@app.post("/labels/add", response_class=HTMLResponse)
def labels_add(request: Request, name: str = Form("")):
    c = _cfg()
    name = name.strip()
    if name and normalize_label(name) not in {normalize_label(x) for x in c["labels"]}:
        c["labels"].append(name)
        q = load(store.paths.PENDING_ENRICH, {})
        q.setdefault("labels", []).append(name)
        save(store.paths.PENDING_ENRICH, q)
        _save_cfg(c)
    return templates.TemplateResponse(request, "partials/labels_list.html",
                                      {"request": request, **_labels_view(c)})


@app.post("/labels/remove", response_class=HTMLResponse)
def labels_remove(request: Request, name: str = Form("")):
    c = _cfg()
    c["labels"] = [l for l in c["labels"] if l != name]
    _save_cfg(c)
    return templates.TemplateResponse(request, "partials/labels_list.html",
                                      {"request": request, **_labels_view(c)})


@app.post("/labels/import", response_class=HTMLResponse)
async def labels_import(request: Request, file: UploadFile, replace: str = Form("")):
    raw = (await file.read()).decode("utf-8", "ignore")
    names = []
    for i, line in enumerate(io.StringIO(raw)):
        if i == 0:
            continue
        n = line.split(",")[0].strip().strip('"')
        if n:
            names.append(n)
    c = _cfg()
    if replace:
        c["labels"] = []
    have = {normalize_label(x) for x in c["labels"]}
    for n in names:
        if normalize_label(n) not in have:
            have.add(normalize_label(n))
            c["labels"].append(n)
    _save_cfg(c)
    return templates.TemplateResponse(request, "partials/labels_list.html",
                                      {"request": request, **_labels_view(c)})


# --------------------------------------------------------------------- inbox partagée (veille + vendeurs)
def _inbox(request, path, source_key, source_label, key_ns, mins=30):
    items = load(path, [])
    if not items:
        return templates.TemplateResponse(request, "partials/inbox.html",
                                          {"request": request, "rows": [], "key_ns": key_ns,
                                           "source_label": source_label, "sources": [], "mins": mins,
                                           "n_total": 0})
    c = Ctx()
    scored = []
    for it in items:
        sc, det = c.album_score({"title": f"{it.get('artist','')} - {it.get('title','')}",
                                 "label": [it["label"]] if it.get("label") else [],
                                 "style": it.get("style") or []})
        scored.append({"it": it, "score": sc, "src": it.get(source_key)})
    scored.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))
    rows = [r for r in scored if (r["score"] or 0) >= mins]
    return templates.TemplateResponse(request, "partials/inbox.html",
                                      {"request": request, "rows": rows[:150], "key_ns": key_ns,
                                       "source_label": source_label, "mins": mins,
                                       "sources": sorted({r["src"] for r in scored if r["src"]}),
                                       "n_total": len(items), "path_key": key_ns})


@app.get("/inbox/{kind}", response_class=HTMLResponse)
def inbox(request: Request, kind: str, mins: int = 30):
    if kind == "veille":
        return _inbox(request, VEILLE_NEW, "rule", "Règle", "veille", mins)
    return _inbox(request, SELLERS_NEW, "seller", "Vendeur", "sellers", mins)


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


# --------------------------------------------------------------------- Veille
@app.get("/veille", response_class=HTMLResponse)
def veille_page(request: Request):
    c = _cfg()
    seen = load(VEILLE_SEEN, {})
    last = max((v.get("last_scan", "") for v in seen.values()), default="")
    return render(request, "pages/veille.html", active="veille",
                  rules=c.get("veille_rules", []), watchlist=c.get("watchlist", []),
                  last=last, year=CURRENT_YEAR)


@app.post("/veille/rules")
async def veille_rules_save(request: Request):
    f = await request.form()
    c = _cfg()
    rules = c.setdefault("veille_rules", [])
    if f.get("_action") == "add":
        rules.append({"name": "Nouvelle règle", "active": True, "styles": [], "genres": ["Electronic"],
                      "year_from": 2000, "year_to": CURRENT_YEAR, "labels": [], "artists": [],
                      "vinyl_only": True})
    elif f.get("_action", "").startswith("del:"):
        i = int(f["_action"][4:])
        if 0 <= i < len(rules):
            rules.pop(i)
    else:
        for i, r in enumerate(rules):
            r["name"] = f.get(f"name_{i}", r.get("name", ""))
            r["active"] = f.get(f"active_{i}") == "on"
            r["vinyl_only"] = f.get(f"vinyl_{i}") == "on"
            r["styles"] = [x.strip() for x in f.get(f"styles_{i}", "").splitlines() if x.strip()]
            r["genres"] = [x.strip() for x in f.get(f"genres_{i}", "").splitlines() if x.strip()]
            r["labels"] = [x.strip() for x in f.get(f"labels_{i}", "").splitlines() if x.strip()]
            r["artists"] = [x.strip() for x in f.get(f"artists_{i}", "").splitlines() if x.strip()]
            try:
                r["year_from"] = int(f.get(f"yf_{i}") or r.get("year_from") or 2000)
                r["year_to"] = int(f.get(f"yt_{i}") or r.get("year_to") or CURRENT_YEAR)
            except ValueError:
                pass
    _save_cfg(c)
    return RedirectResponse("/veille", status_code=303)


# --------------------------------------------------------------------- Vendeurs
@app.get("/sellers", response_class=HTMLResponse)
def sellers_page(request: Request):
    c = _cfg()
    seen = load(SELLERS_SEEN, {})
    last = max((v.get("last_scan", "") for v in seen.values()), default="")
    return render(request, "pages/sellers.html", active="sellers",
                  sellers=c.get("sellers", []), last=last)


@app.post("/sellers/add")
def sellers_add(name: str = Form("")):
    c = _cfg()
    import re
    for n in re.split(r"[,\s]+", name.strip()):
        n = n.strip().strip("@/")
        m = re.search(r"/seller/([^/]+)", n)
        if m:
            n = m.group(1)
        if n and n not in c.setdefault("sellers", []):
            c["sellers"].append(n)
    _save_cfg(c)
    return RedirectResponse("/sellers", status_code=303)


@app.post("/sellers/remove")
def sellers_remove(name: str = Form("")):
    c = _cfg()
    c["sellers"] = [s for s in c.get("sellers", []) if s != name]
    _save_cfg(c)
    return RedirectResponse("/sellers", status_code=303)


# --------------------------------------------------------------------- Sources & reco
JOB_BUTTONS = [
    ("fetch_collection", "Charger collection + wantlist Discogs", {}),
    ("ingest_youtube", "Importer playlists YouTube", {"deep": True}),
    ("ingest_bandcamp", "Importer collection Bandcamp", {"deep": True}),
    ("scan_veille", "Scanner la veille", {}),
    ("scan_sellers", "Scanner les vendeurs", {}),
    ("merge_corpus", "Consolider corpus → base", {}),
]


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    c = Ctx()
    rows = c.reco_rows()
    base = {normalize_label(x) for x in c.cfg.get("labels", [])}
    watch = {normalize_label(x) for x in c.cfg.get("watchlist", [])}
    for r in rows:
        r["in_base"] = r["key"] in base
        r["watched"] = r["key"] in watch
    return render(request, "pages/sources.html", active="sources",
                  jobs=JOB_BUTTONS, reco=rows[:60], n_reco=len(rows),
                  cfg=c.cfg)


@app.post("/jobs/{name}/launch", response_class=HTMLResponse)
def job_launch(request: Request, name: str):
    valid = {b[0] for b in JOB_BUTTONS} | {"build_graph", "profile_labels", "ingest_djsets",
                                           "resolve_artists", "canonicalize", "enrich"}
    if name in valid:
        params = next((p for n, _, p in JOB_BUTTONS if n == name), {})
        jobs.launch(name, params)
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


@app.post("/reco/label", response_class=HTMLResponse)
def reco_label(request: Request, name: str = Form(""), dest: str = Form("base")):
    c = _cfg()
    name = name.strip()
    if name:
        if dest in ("base", "both") and normalize_label(name) not in {normalize_label(x) for x in c["labels"]}:
            c["labels"].append(name)
        if dest in ("veille", "both") and normalize_label(name) not in {normalize_label(x) for x in c.get("watchlist", [])}:
            c.setdefault("watchlist", []).append(name)
        q = load(store.paths.PENDING_ENRICH, {})
        q.setdefault("labels", []).append(name)
        save(store.paths.PENDING_ENRICH, q)
        _save_cfg(c)
    return HTMLResponse("<span class='small muted'>✓ ajouté</span>")


# --------------------------------------------------------------------- stubs restants
@app.get("/{section}", response_class=HTMLResponse)
def stub(request: Request, section: str):
    if section not in STUB:
        return HTMLResponse("Page inconnue", status_code=404)
    return render(request, "pages/stub.html", active=section, heading=STUB[section])
