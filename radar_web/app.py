"""Radar — interface FastAPI + HTMX (parité en cours avec l'appli Streamlit).

Lancement :  uvicorn radar_web.app:app --reload
Les données sont PARTAGÉES avec l'appli Streamlit (même CRATE_DATA_DIR)."""
import hashlib
import os
import time

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .radar import discogs, jobs, store
from .radar.scoring import Ctx, real_tracks, yt_search_url

HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
app = FastAPI(title="Radar")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

AUTH_TTL = 5 * 3600
COOKIE = "radar_auth"
_STUB = {
    "veille": "Veille", "sellers": "Mes vendeurs", "labels": "Mes labels",
    "sources": "Sources & reco", "artists": "Mes artistes", "sets": "Mes sets",
    "settings": "Réglages",
}


# --------------------------------------------------------------------- auth
def _pw():
    return os.environ.get("APP_PASSWORD", "")


def _token(exp):
    exp = int(exp)
    sig = hashlib.sha256(f"{_pw()}|{exp}".encode()).hexdigest()[:20]
    return f"{exp}.{sig}"


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
    if _pw() and _token_ok(request.cookies.get(COOKIE, "")):   # renouvellement glissant
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


# --------------------------------------------------------------------- pages
def _page(request, tpl, **ctx):
    c = Ctx()
    ctx.setdefault("has_token", bool(c.cfg.get("token")))
    return templates.TemplateResponse(request, tpl, {"request": request, **ctx})


@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/search", status_code=303)


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request):
    return _page(request, "pages/search.html", active="search", q={})


@app.post("/search", response_class=HTMLResponse)
def search_run(request: Request, label: str = Form(""), genre: str = Form(""),
               style: str = Form(""), year_from: str = Form(""), year_to: str = Form(""),
               vinyl: str = Form(""), pages: str = Form("2")):
    c = Ctx()
    token = c.cfg.get("token", "")
    year = ""
    if year_from and year_to:
        year = f"{year_from}-{year_to}"
    elif year_from:
        year = year_from
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
                if genre:
                    p["genre"] = genre
                if style:
                    p["style"] = style
                if year:
                    p["year"] = year
                if fmt:
                    p["format"] = fmt
                d = discogs.search(token=token, **p)
                got = d.get("results", [])
                raw += got
                if pg >= d.get("pagination", {}).get("pages", 1) or not got:
                    break
    except discogs.DiscogsError as e:
        return templates.TemplateResponse(request, "partials/results.html",
                                          {"request": request, "error": str(e)})
    # dédoublonnage + notation
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
    tracks = []
    for t in real_tracks(data.get("tracklist", [])):
        ta = ", ".join(a.get("name", "") for a in t.get("artists", [])) or rel_artist
        tracks.append({"pos": (t.get("position") or "").strip(),
                       "title": (t.get("title") or "").strip(),
                       "yt": yt_search_url(f"{ta} {t.get('title', '')}")})
    return templates.TemplateResponse(request, "partials/tracklist.html",
                                      {"request": request, "tracks": tracks})


@app.get("/jobs/{name}/status", response_class=HTMLResponse)
def job_status(name: str):
    s = jobs.status(name)
    if not s:
        return HTMLResponse("<span class='muted small'>—</span>")
    done, total = s.get("done", 0), s.get("total", 0) or 1
    pct = min(100, round(100 * done / total))
    running = s.get("running")
    err = s.get("error")
    body = f"<div class='small muted'>{'⏳ ' if running else ''}{s.get('message') or ''}"
    body += f" · {done}/{s.get('total', 0)}</div>"
    if running:
        body += f"<div class='progress'><i style='width:{pct}%'></i></div>"
    if err:
        body = f"<div class='notice warn small'>{err}</div>"
    attrs = ("hx-get='/jobs/%s/status' hx-trigger='every 2s' hx-swap='outerHTML'" % name) if running else ""
    return HTMLResponse(f"<div id='job-{name}' {attrs}>{body}</div>")


@app.get("/{section}", response_class=HTMLResponse)
def stub(request: Request, section: str):
    if section not in _STUB:
        return HTMLResponse("Page inconnue", status_code=404)
    return _page(request, "pages/stub.html", active=section, heading=_STUB[section])
