"""Radar — interface FastAPI + HTMX. Données PARTAGÉES avec l'appli Streamlit.
Lancement :  uvicorn radar_web.app:app --reload --port 8600

Nav : 🧠 Mes sources · 🔍 Chercher un disque · 📻 Nouveautés · 🌐 Mes labels & artistes · 🎛️ Réglages
(URLs historiques inchangées : /patte, /search, /veille, /univers, /settings)
"""
import hashlib
import hmac
import html
import io
import os
import re
import secrets
import threading
import time
from datetime import datetime
from urllib.parse import quote_plus, urlencode

import requests
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .radar import (accounts, artistgraph, bandcamp, discogs, jobs, labelgraph, learn,
                    paths, sellers, store, vocab, ytcache)
from .radar.scoring import Ctx, real_tracks, track_row_id, yt_search_url
from .radar.store import load, normalize_label, save


def _pu():
    """Chemins de données de l'utilisateur de la requête courante."""
    return paths.user_paths(store.current_uid())

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

# migration douce : <DATA>/*.json -> users/owner/ + shared/  (idempotent, no-op si déjà fait)
_migrated = paths.migrate_layout()
if _migrated:
    import sys as _sys
    print(f"[radar] layout migré vers users/{paths.DEFAULT_UID}/ + shared/ : "
          f"{len(_migrated)} fichier(s)", file=_sys.stderr)

app = FastAPI(title="Radar")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

AUTH_TTL = 5 * 3600
COOKIE = "radar_auth"
CURRENT_YEAR = time.gmtime().tm_year


# --------------------------------------------------------------------- auth
def _session_secret():
    v = os.environ.get("APP_SESSION_SECRET")
    if v:
        return v.encode()
    p = os.path.join(paths.DATA, ".session_secret")
    if not os.path.isfile(p):
        with open(p, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(p, 0o600)
    with open(p) as f:
        return f.read().strip().encode()


SESSION_SECRET = _session_secret()
accounts.bootstrap()


def _dev_mode():
    """Authentification désactivée UNIQUEMENT sur demande explicite (CI, dev local).

    `RADAR_NO_AUTH=1` est obligatoire : sans lui, un accounts.json illisible ou un
    volume mal monté ferait passer `accounts.count()` à 0 et ouvrirait l'appli en
    grand sur Internet avec les droits du propriétaire."""
    return (os.environ.get("RADAR_NO_AUTH") == "1"
            and not os.environ.get("APP_PASSWORD") and accounts.count() == 0)


def _pw_epoch(uid):
    """Extrait du hash du mot de passe : change à chaque changement de mot de passe,
    ce qui invalide les sessions existantes (seule voie de révocation)."""
    return ((accounts.get(uid) or {}).get("pw", ""))[-16:]


def _sign(uid, exp):
    return hmac.new(SESSION_SECRET, f"{uid}|{exp}|{_pw_epoch(uid)}".encode(),
                    hashlib.sha256).hexdigest()[:32]


def _make_token(uid, exp):
    exp = int(exp)
    return f"{uid}.{exp}.{_sign(uid, exp)}"


def _https(request):
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _set_session(resp, uid, request):
    secure = os.environ.get("RADAR_SECURE_COOKIE") == "1" or _https(request)
    resp.set_cookie(COOKIE, _make_token(uid, time.time() + AUTH_TTL), max_age=AUTH_TTL,
                    httponly=True, samesite="lax", secure=secure)


def _parse_token(tok):
    try:
        uid, exp, sig = (tok or "").split(".")
        exp = int(exp)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(uid, exp)) or time.time() >= exp:
        return None
    return uid


def _req_uid(request):
    if _dev_mode():
        return paths.DEFAULT_UID
    uid = _parse_token(request.cookies.get(COOKIE, ""))
    return uid if uid and accounts.get(uid) else None


def _bad_origin(request):
    """Défense en profondeur CSRF : le cookie est déjà SameSite=lax et toutes les
    mutations sont des POST, mais on refuse en plus une origine étrangère."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return False
    origin = request.headers.get("origin")
    return bool(origin) and origin.rstrip("/") != str(request.base_url).rstrip("/")


@app.middleware("http")
async def _guard(request: Request, call_next):
    p = request.url.path
    if p.startswith("/static") or p in ("/login", "/health", "/register"):
        return await call_next(request)
    if _bad_origin(request):
        return HTMLResponse("Origine invalide.", status_code=403)
    uid = _req_uid(request)
    if not uid:
        if request.headers.get("hx-request"):
            return HTMLResponse("Session expirée — <a href='/login'>reconnexion</a>", status_code=401)
        return RedirectResponse("/login", status_code=303)
    store.set_current_uid(uid)          # lu par load_config() / Ctx() / _pu() / jobs.launch()
    resp = await call_next(request)
    # surtout pas sur /logout : le middleware réécrirait le cookie qu'on vient d'effacer
    if not _dev_mode() and p != "/logout":
        _set_session(resp, uid, request)
    return resp


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, bad: int = 0):
    if _req_uid(request):
        return RedirectResponse("/", status_code=303)
    msg = ("<p class='notice warn'>Trop de tentatives — réessaie dans une minute.</p>"
           if bad == 2 else "<p class='notice warn'>Identifiants incorrects.</p>" if bad else "")
    return HTMLResponse(f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Radar</title>
<link rel=stylesheet href=/static/app.css><div class=wrap style='max-width:360px'>
<p class=brand style='font-size:32px'>Rada<b>r</b></p>{msg}
<form method=post action=/login>
  <div class=field><label>Identifiant</label><input name=username autofocus autocapitalize=off autocomplete=username></div>
  <div class=field><label>Mot de passe</label><input type=password name=pw autocomplete=current-password></div>
  <button class=primary type=submit>Entrer</button>
</form></div>""")


_LOGIN_FAILS = {}          # ip -> [nb d'échecs, bloqué jusqu'à]
LOGIN_MAX_FAILS = 5
LOGIN_BLOCK_S = 60


def _login_blocked(ip):
    n, until = _LOGIN_FAILS.get(ip, (0, 0))
    return time.time() < until


def _login_note(ip, ok):
    if ok:
        _LOGIN_FAILS.pop(ip, None)
        return
    n, until = _LOGIN_FAILS.get(ip, (0, 0))
    n += 1
    if len(_LOGIN_FAILS) > 500:
        _LOGIN_FAILS.clear()
    _LOGIN_FAILS[ip] = (n, time.time() + LOGIN_BLOCK_S if n >= LOGIN_MAX_FAILS else until)
    if n >= LOGIN_MAX_FAILS:
        _LOGIN_FAILS[ip] = (0, time.time() + LOGIN_BLOCK_S)


@app.post("/login")
def login(request: Request, username: str = Form(""), pw: str = Form("")):
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else "?"))
    if _login_blocked(ip):
        return RedirectResponse("/login?bad=2", status_code=303)
    uid = accounts.verify(username, pw)
    _login_note(ip, bool(uid))
    if uid:
        r = RedirectResponse("/", status_code=303)
        _set_session(r, uid, request)
        return r
    return RedirectResponse("/login?bad=1", status_code=303)


@app.post("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(COOKIE)
    return r


_REG_PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Radar</title>
<link rel=stylesheet href=/static/app.css><div class=wrap style='max-width:360px'>
<p class=brand style='font-size:32px'>Rada<b>r</b></p>{msg}
<form method=post action=/register>
  <input type=hidden name=invite value="{tok}">
  <div class=field><label>Choisis un identifiant</label><input name=username autofocus autocapitalize=off autocomplete=username required></div>
  <div class=field><label>Mot de passe (10+ caractères)</label><input type=password name=pw autocomplete=new-password required minlength=10></div>
  <button class=primary type=submit>Créer mon compte</button>
</form></div>"""


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request, invite: str = "", bad: str = ""):
    if not accounts.invite_ok(invite):
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><link rel=stylesheet href=/static/app.css>"
            "<div class=wrap style='max-width:360px'><p class=brand style='font-size:32px'>Rada<b>r</b></p>"
            "<p class='notice warn'>Lien d'invitation invalide ou déjà utilisé.</p></div>",
            status_code=403)
    msg = f"<p class='notice warn'>{html.escape(bad)}</p>" if bad else ""
    return HTMLResponse(_REG_PAGE.format(msg=msg, tok=html.escape(invite)))


@app.post("/register")
def register(request: Request, invite: str = Form(""), username: str = Form(""), pw: str = Form("")):
    try:
        uid = accounts.consume_invite(invite, username, pw)
    except ValueError as e:
        return RedirectResponse(f"/register?invite={quote_plus(invite)}&bad={quote_plus(str(e))}",
                                status_code=303)
    r = RedirectResponse("/", status_code=303)
    _set_session(r, uid, request)
    return r


@app.post("/account/invite", response_class=HTMLResponse)
def account_invite(request: Request):
    if store.current_uid() != paths.DEFAULT_UID:
        return HTMLResponse("<p class='notice warn small'>Réservé au propriétaire.</p>",
                            status_code=403)
    tok = accounts.create_invite(store.current_uid())
    url = str(request.base_url).rstrip("/") + "/register?invite=" + tok
    return HTMLResponse(
        f"<p class='small'>Lien d'invitation (à usage unique) :</p>"
        f"<input readonly onclick='this.select()' value='{html.escape(url)}' style='width:100%'>")


# --------------------------------------------------------------------- helpers
def render(request, tpl, **ctx):
    ctx.setdefault("has_token", bool(store.read_config().get("token")))
    ctx.setdefault("me", (accounts.get(store.current_uid()) or {}).get("username"))
    ctx.setdefault("is_owner", store.current_uid() == paths.DEFAULT_UID)
    ctx.setdefault("n_cart", len(load(_pu().cart, [])))
    ctx.setdefault("n_notes", sum(1 for n in load(_pu().ui_notes, []) if n.get("status") == "nouveau"))
    return templates.TemplateResponse(request, tpl, {"request": request, **ctx})


def frag(request, tpl, **ctx):
    return templates.TemplateResponse(request, tpl, {"request": request, **ctx})


def _cfg():
    return store.load_config()


# --------------------------------------------------------------------- home
@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/patte", status_code=303)


# ============================================================ 🧠 Mes sources
def _last_import(job):
    s = jobs.status(job)
    fa = s.get("finished_at") if s else None
    return fa.replace("T", " ")[:16] if fa else None


@app.get("/patte", response_class=HTMLResponse)
def patte_page(request: Request, saved: int = 0, yt_connected: int = 0, yt_error: str = ""):
    from .radar import ytwatch, ytwrite   # imports locaux : dépendances optionnelles (cf. CI)
    c = Ctx()
    pl_urls = [u for u in (c.cfg.get("youtube_playlists") or "").splitlines() if u.strip()]
    sp_urls = [u for u in (c.cfg.get("spotify_playlists") or "").splitlines() if u.strip()]
    last = {j: _last_import(j) for j in
           ("fetch_collection", "ingest_youtube", "ingest_spotify", "ingest_bandcamp", "ingest_djsets",
            "scan_recos")}
    st = c.stats()
    # X1 : compte tout neuf (ni token, ni disque, ni titre analysé) -> flux d'accueil en 3 étapes
    onboarding = not c.cfg.get("token") and not st.get("tracks") and not (c.collection.get("n_collection") or 0)
    return render(request, "pages/patte.html", active="patte", cfg=c.cfg, sc=c.scoring,
                  cats=c.cfg.get("taste_categories", {}), coll=c.collection,
                  pl_urls=pl_urls, pl_meta=load(_pu().youtube_meta, {}),
                  sp_urls=sp_urls, sp_meta=load(_pu().spotify_meta, {}),
                  src=c.corpus_by_source(), st=st, saved=saved, last=last, onboarding=onboarding,
                  recos_connected=ytwrite.is_connected(c.uid),
                  recos_pending=len(load(_pu().recos_candidates, [])),
                  recos_watch_session=ytwatch.has_session(c.uid),
                  yt_connected=yt_connected, yt_error=yt_error)


def _yt_oauth_redirect_uri(request):
    """Callback fixe côté appli — HTTPS déjà en place (radar.hubclaudel.fr), donc
    utilisable tel quel comme redirect_uri d'un client OAuth Google de type
    Web application (cf. radar/ytwrite.py, docstring)."""
    scheme = "https" if _https(request) else "http"
    host = request.headers.get("host") or request.url.hostname
    return f"{scheme}://{host}/oauth/youtube/callback"


def _yt_oauth_error_redirect(msg):
    return RedirectResponse(f"/patte?yt_error={quote_plus(msg)}", status_code=303)


@app.get("/oauth/youtube/start")
def oauth_youtube_start(request: Request):
    """« Connecter YouTube (playlist RECOS RADAR) » — redirige vers l'écran de
    consentement Google. `state` posé en cookie court, revérifié au retour
    (cf. callback) : seule protection CSRF nécessaire pour ce flux, comme
    recommandé par Google."""
    from .radar import ytwrite
    try:
        url, state, code_verifier = ytwrite.authorization_url(_yt_oauth_redirect_uri(request))
    except ytwrite.YouTubeAuthError as e:
        return _yt_oauth_error_redirect(str(e))
    resp = RedirectResponse(url, status_code=303)
    secure = os.environ.get("RADAR_SECURE_COOKIE") == "1" or _https(request)
    resp.set_cookie("yt_oauth_state", state, max_age=600, httponly=True, samesite="lax", secure=secure)
    # PKCE : le code_verifier généré ici doit être réutilisé tel quel à l'échange
    # (cf. ytwrite.authorization_url) — transporté comme le state, par cookie court.
    resp.set_cookie("yt_oauth_verifier", code_verifier, max_age=600, httponly=True,
                    samesite="lax", secure=secure)
    return resp


@app.get("/oauth/youtube/callback")
def oauth_youtube_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    from .radar import ytwrite
    if error:
        return _yt_oauth_error_redirect(f"Autorisation refusée ({error}).")
    expected = request.cookies.get("yt_oauth_state", "")
    code_verifier = request.cookies.get("yt_oauth_verifier", "")
    if not code or not state or not expected or state != expected or not code_verifier:
        return _yt_oauth_error_redirect("Échange OAuth invalide (état expiré ou incohérent) — réessaie.")
    try:
        creds = ytwrite.exchange_code(_yt_oauth_redirect_uri(request), code, code_verifier)
    except Exception as e:                       # noqa: BLE001 — flux Google, forme d'erreur variable
        return _yt_oauth_error_redirect(f"Échange du code YouTube : {type(e).__name__}: {e}")
    ytwrite.save_credentials(store.current_uid(), creds)
    resp = RedirectResponse("/patte?yt_connected=1", status_code=303)
    resp.delete_cookie("yt_oauth_state")
    resp.delete_cookie("yt_oauth_verifier")
    return resp


@app.post("/oauth/youtube/disconnect")
def oauth_youtube_disconnect():
    from .radar import ytwrite
    ytwrite.disconnect(store.current_uid())
    return RedirectResponse("/patte", status_code=303)


@app.post("/patte/youtube-session/upload")
async def youtube_session_upload(file: UploadFile):
    """Import de storage_state.json (session de visionnage YouTube, cf.
    scripts/export_youtube_session.py) — sert au nettoyage automatique de RECOS RADAR
    (lot 3, radar/ytwatch.py), jamais à la playlist elle-même (OAuth2, ytwrite.py)."""
    from .radar import ytwatch
    raw = await file.read()
    ytwatch.save_session(store.current_uid(), raw)
    return RedirectResponse("/patte?yt_connected=1", status_code=303)


@app.post("/patte/youtube-session/clear")
def youtube_session_clear():
    from .radar import ytwatch
    ytwatch.clear_session(store.current_uid())
    return RedirectResponse("/patte", status_code=303)


def _apply_patte_form(f):
    c = _cfg()
    for k in ("token", "youtube_api_key", "spotify_client_id", "spotify_client_secret",
              "bandcamp_sub_user", "bandcamp_sub_pass"):
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


@app.post("/patte/save", response_class=HTMLResponse)
async def patte_save_frag(request: Request):
    """Enregistre les champs de la section (fragment, reste sur place)."""
    _apply_patte_form(await request.form())
    return HTMLResponse(f"<span class='small ok'>✓ enregistré {time.strftime('%H:%M')}</span>")


@app.post("/patte/run/{job}", response_class=HTMLResponse)
async def patte_run(request: Request, job: str):
    """Enregistre les identifiants/champs saisis PUIS lance le job (pour que le job
    utilise bien ce qui vient d'être tapé, sans étape « Enregistrer » séparée)."""
    _apply_patte_form(await request.form())
    return job_launch(job)


def _scanned_djs(c):
    by = {}
    for r in c.corpus:
        if r.get("source") != "djset":
            continue
        d = by.setdefault(r.get("dj") or "?", {"vids": set(), "tracks": 0, "last": ""})
        d["vids"].add(r.get("video"))
        d["tracks"] += 1
        d["last"] = max(d["last"], r.get("added_at") or "")
    return sorted(({"name": k, "sets": len(v["vids"]), "tracks": v["tracks"],
                    "date": (v["last"] or "")[:10]} for k, v in by.items()),
                  key=lambda x: x["name"].lower())


@app.get("/patte/djset/panel", response_class=HTMLResponse)
def patte_djset_panel(request: Request):
    return frag(request, "partials/djset_panel.html",
                scanned=_scanned_djs(Ctx()), job=jobs.status("ingest_djsets"))


@app.post("/patte/djset/scan", response_class=HTMLResponse)
def patte_djset_scan(request: Request, name: str = Form("")):
    name = name.strip()
    if not name:
        return frag(request, "partials/djset_panel.html",
                    scanned=_scanned_djs(Ctx()), job=jobs.status("ingest_djsets"),
                    err="Renseigne le nom d'un DJ.")
    c = _cfg()
    have = [s.strip() for s in (c.get("djset_sources") or "").splitlines() if s.strip()]
    if name.lower() not in {s.lower() for s in have}:
        have.append(name)
        c["djset_sources"] = "\n".join(have)
        store.save_config(c)
    d = os.path.join(store.paths.JOBS_DIR, store.current_uid())
    os.makedirs(d, exist_ok=True)
    save(os.path.join(d, "djsets.input.json"),
         {"sources": [name], "max_per_source": 25, "min_minutes": 35,
          "require_hint": True, "deep": True})
    jobs.launch("ingest_djsets")
    return frag(request, "partials/djset_panel.html",
                scanned=_scanned_djs(Ctx()), job=jobs.status("ingest_djsets"))


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
        q = load(_pu().pending_enrich, {})
        q.setdefault("artists", []).extend(names)
        save(_pu().pending_enrich, q)
        jobs.launch("enrich")
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


# ============================================================ 🔍 Chercher un disque
SEARCH_MIN_YEAR = 1960


def _year_bounds(year_from, year_to):
    """(a, b) entiers, bornés à [SEARCH_MIN_YEAR, année courante]."""
    mx = int(time.strftime("%Y"))
    try:
        a = int(year_from) if str(year_from).strip() else SEARCH_MIN_YEAR
        b = int(year_to) if str(year_to).strip() else mx
    except ValueError:
        return SEARCH_MIN_YEAR, mx
    a, b = min(a, b), max(a, b)
    return max(SEARCH_MIN_YEAR, a), min(mx, b)


def _year_param(year_from, year_to):
    """Construit 'AAAA-AAAA' pour Discogs ; '' si l'intervalle couvre tout."""
    mx = int(time.strftime("%Y"))
    a, b = _year_bounds(year_from, year_to)
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
SEARCH_PAGE_SIZE = 100

FEEDBACK_GH_REPO = "alixclaudel-hue/radar"
FEEDBACK_GH_ISSUE = 62


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
    if p.get("base_metric"):
        lbl = {"aff": "affinité", "reco": "reco", "owned": "collection"}.get(p["base_metric"], p["base_metric"])
        bits.append(f"mes labels ({lbl}"
                    + (f"≥{p['base_min']}" if p.get("base_min") else "") + ")")
    return " · ".join(bits) or "tous filtres vides"


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, sid: str = "", style: str = ""):
    hist = load(_pu().search_hist, [])
    entry = next((e for e in hist if e.get("id") == sid), hist[0] if hist else None)
    q = (entry or {}).get("params", {})
    if not entry and style.strip():
        q = {"style": [style.strip()]}
    return render(request, "pages/search.html", active="search",
                  q=q, last_id=(entry or {}).get("id", ""),
                  history=[{"id": e["id"], "ts": e.get("ts", ""), "n": e.get("n", 0),
                            "summary": _hist_summary(e.get("params", {}))} for e in hist],
                  year_min=SEARCH_MIN_YEAR, year_max=int(time.strftime("%Y")))


def _voted_map():
    """{release_id (int): 'up'|'down'} d'après le feedback de l'utilisateur courant."""
    out = {}
    for e in load(_pu().feedback, []):
        k = str(e.get("key", ""))
        if e.get("kind") == "album" and k.startswith("rid:"):
            try:
                out[int(k[4:])] = e.get("verdict")
            except ValueError:
                pass
    return out


@app.get("/search/replay/{sid}", response_class=HTMLResponse)
def search_replay(request: Request, sid: str):
    entry = next((e for e in load(_pu().search_hist, []) if e.get("id") == sid), None)
    if not entry:
        return frag(request, "partials/results.html", results=[])
    return frag(request, "partials/results.html", results=entry.get("results", []),
                searched=entry.get("searched", []), voted=_voted_map(), in_cart=_cart_ids(),
                dump_date=entry.get("dump_date"), has_token=bool(Ctx().cfg.get("token", "")),
                n_matches=entry.get("n_matches"), page=1, total_pages=1)


def _base_labels_ranked(c):
    """Labels de la base -> nom canonique + affinité. Tri : affinité de style
    décroissante, départagée par le score de reco (collection + corpus + artistes),
    puis alpha. Dédoublonné par nom canonique."""
    ridx = c.reco_index
    lc = c.collection.get("label_counts", {})
    lids = c.collection.get("label_ids", {})
    keys = [store.normalize_label(name) for name in c.cfg.get("labels", [])]
    affinities = c.label_affinities(keys)
    rows, seen = [], set()
    for name, key in zip(c.cfg.get("labels", []), keys):
        res = c.resolved.get(key) or {}
        disp = res.get("discogs_name") or res.get("original") or name
        aff, coverage = affinities[key]["aff"], affinities[key]["coverage"]
        did = res.get("discogs_id") or lids.get(key, {}).get("id")
        rows.append({"disp": disp, "norm": store.normalize_label(disp), "key": key,
                     "aff": aff, "coverage": coverage, "_reco": ridx.get(key, 0),
                     "owned": lc.get(key, 0), "id": did})
    rows.sort(key=lambda r: (r["aff"] is None, -(r["aff"] or 0), -r["_reco"], r["disp"].lower()))
    uniq = []
    for r in rows:
        if r["norm"] in seen:
            continue
        seen.add(r["norm"])
        uniq.append(r)
    return uniq


_BASE_METRIC = {"aff": "aff", "reco": "_reco", "owned": "owned"}


def _pick_base_labels(c, metric, minv, topn):
    """Noms des labels de la base classés selon `metric` (aff|reco|owned),
    filtrés par seuil `minv`, limités à `topn` (borne dure 12)."""
    field = _BASE_METRIC.get(metric)
    if not field:
        return []

    def val(r):
        v = r.get(field)
        return -1.0 if v is None else float(v)

    rows = _base_labels_ranked(c)
    if minv is not None:
        rows = [r for r in rows if val(r) >= minv]
    rows.sort(key=lambda r: -val(r))
    return [r["disp"] for r in rows[:max(1, min(12, topn))]]


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


def _suggest_vocab(request, pool, q, label):
    term = (q or "").strip().lower()
    hits = [s for s in pool if term in s.lower()] if term else list(pool)
    rows = [{"v": s} for s in hits[:60]]
    header = (f"{len(hits)} {label}" if term else f"{label} — vocabulaire Discogs")
    return frag(request, "partials/suggest.html", rows=rows, header=header,
                empty=f"Aucun {label[:-1]} ne correspond.")


@app.get("/suggest/styles", response_class=HTMLResponse)
def suggest_styles(request: Request, q: str = ""):
    c = Ctx()
    mine, more = _search_styles(c)
    seen, pool = set(), []
    for s in mine + more:
        if s.lower() not in seen:
            seen.add(s.lower())
            pool.append(s)
    return _suggest_vocab(request, pool, q, "styles")


@app.get("/suggest/genres", response_class=HTMLResponse)
def suggest_genres(request: Request, q: str = ""):
    return _suggest_vocab(request, vocab.GENRES, q, "genres")


_DISCOGS_SUGGEST_CACHE = {}  # (type, term) -> (ts, rows)


@app.get("/suggest/discogs", response_class=HTMLResponse)
def suggest_discogs(request: Request, q: str = "", type: str = "label"):
    dtype = "artist" if type == "artist" else "label"
    term = (q or "").strip()
    if dtype == "label":
        from .radar import discogs_dump as dd
        if dd.available():
            if len(term) < 2:
                return frag(request, "partials/suggest.html", rows=[],
                            empty="Tape au moins 2 lettres.")
            names = dd.suggest_labels(term, limit=12)
            rows = [{"v": n, "meta": "", "dim": True} for n in names]
            header = f"Référentiel local — {len(rows)} label{'s' if len(rows) != 1 else ''}"
            return frag(request, "partials/suggest.html", rows=rows, header=header,
                        empty=f"Aucun label du référentiel local pour « {term} ».")
    if len(term) < 3:
        return frag(request, "partials/suggest.html", rows=[],
                    empty="Tape au moins 3 lettres pour interroger Discogs.")
    token = _cfg().get("token", "")
    if not token:
        return frag(request, "partials/suggest.html", rows=[],
                    empty="Token Discogs manquant (Ma patte → Connexions).")
    key = (dtype, term.lower())
    hit = _DISCOGS_SUGGEST_CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        rows = hit[1]
    else:
        try:
            res = discogs.search(token=token, type=dtype, q=term, per_page=12).get("results", [])
        except discogs.DiscogsError as e:
            return frag(request, "partials/suggest.html", rows=[], empty=str(e))
        seen, rows = set(), []
        for r in res:
            name = (r.get("title") or "").strip()
            nk = name.lower()
            if not name or nk in seen:
                continue
            seen.add(nk)
            rows.append({"v": name, "meta": str(r.get("id") or ""), "dim": True})
        if len(_DISCOGS_SUGGEST_CACHE) > 200:
            _DISCOGS_SUGGEST_CACHE.clear()
        _DISCOGS_SUGGEST_CACHE[key] = (time.time(), rows)
    label = "label" if dtype == "label" else "artiste"
    header = f"Discogs — {len(rows)} {label}{'s' if len(rows) != 1 else ''}"
    return frag(request, "partials/suggest.html", rows=rows, header=header,
                empty=f"Aucun {label} Discogs pour « {term} ».")


def _score_min(raw):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _local_rows_to_raw(rows, genres):
    """Résultats `discogs_dump.search_local` -> même forme que les résultats
    de l'API (album_score/results.html attendent title="Artiste - Titre",
    label/style en listes). Filtre le genre ici (colonne non normalisée dans
    le référentiel local, cf. D6) sur ce sous-ensemble déjà borné."""
    out = []
    for row in rows:
        row_genres = (row.get("genres") or "").split(", ") if row.get("genres") else []
        if genres and not any(g in row_genres for g in genres):
            continue
        title = f"{row['artist']} - {row['title']}" if row.get("artist") else (row.get("title") or "")
        out.append({
            "id": row["id"], "title": title,
            "label": [row["label"]] if row.get("label") else [],
            "style": row["styles"].split(", ") if row.get("styles") else [],
            "catno": row.get("catno") or "", "year": row.get("year") or "",
            "cover_image": None, "thumb": None, "uri": f"/release/{row['id']}",
        })
    return out


@app.post("/search", response_class=HTMLResponse)
def search_run(request: Request, label: str = Form(""),
               genre: str = Form(""), style: str = Form(""),
               year_from: str = Form(""), year_to: str = Form(""),
               pages: str = Form("2"),
               base_metric: str = Form(""), base_min: str = Form(""),
               label_min: str = Form(""), artist_min: str = Form(""),
               page: str = Form("1")):
    from .radar import discogs_dump as dd
    c = Ctx()
    token = c.cfg.get("token", "")
    year = _year_param(year_from, year_to)
    genres = [g.strip() for g in genre.splitlines() if g.strip()]
    styles = [s.strip() for s in style.splitlines() if s.strip()]
    try:
        npages = max(1, min(4, int(pages or 2)))
    except ValueError:
        npages = 2

    # mode « chercher dans mes labels » : prend le pas sur le label unique
    base_metric = (base_metric or "").strip()
    try:
        b_min = float(base_min) if str(base_min).strip() else None
    except ValueError:
        b_min = None
    base_labels = _pick_base_labels(c, base_metric, b_min, 12) if base_metric else []

    # --- local d'abord (référentiel Discogs importé, cf. diagnostic D6) : instantané,
    # mais un dump est mensuel — ignore les sorties des ~30 derniers jours. ---
    raw, seen_ids = [], set()
    used_local = dd.available()
    dump_date = None
    if used_local:
        if base_labels:
            label_keys = [normalize_label(lb) for lb in base_labels]
        elif label.strip():
            label_keys = [normalize_label(label)]
        else:
            label_keys = []
        yb = _year_bounds(year_from, year_to)
        # BETWEEN exclut les year IS NULL (nombreuses en local) : ne filtrer que si
        # l'utilisateur a vraiment resserré l'intervalle, jamais sur les bornes par défaut
        # qui couvrent tout — sinon toute sortie sans année connue disparaît en silence du
        # local alors que le chemin API les incluait (_year_param renvoie '' = pas de filtre
        # dans ce cas). Diagnostic R5.
        year_range = None if (yb[0] <= SEARCH_MIN_YEAR and yb[1] >= int(time.strftime("%Y"))) else yb
        local_rows = dd.search_local(label_keys or None, styles or None, year_range)
        for r in _local_rows_to_raw(local_rows, genres):
            seen_ids.add(r["id"])
            raw.append(r)
        dump_date = dd.get_meta().get("dump_date")

    if not used_local:
        gs = [(g, s) for g in (genres or [""]) for s in (styles or [""])]
        if base_labels:
            npages = min(npages, 1)                       # N labels -> 1 page chacun
            combos = [(lab, g, s) for lab in base_labels for (g, s) in gs][:12]
        else:
            combos = [(label.strip() or None, g, s) for (g, s) in gs][:8]

        try:
            for i, (lab, g, s) in enumerate(combos):
                if i:
                    time.sleep(1.0)
                if lab:
                    part = discogs.search_label_releases(token, lab, genre=g,
                                                         style=s, year=year, max_pages=npages)
                else:
                    part = []
                    for pg in range(1, npages + 1):
                        p = {"per_page": 100, "page": pg, "sort": "year", "sort_order": "desc"}
                        for k, v in (("genre", g), ("style", s), ("year", year)):
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
            if not raw:              # le local a déjà des résultats : ne pas tout perdre sur un raté API
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
    n_before_thresholds = len(scored)
    mins = {"label": _score_min(label_min), "artist": _score_min(artist_min)}
    if any(v is not None for v in mins.values()):
        scored = [x for x in scored if all(
            v is None or ((x["detail"].get(k) or 0) >= v) for k, v in mins.items())]
    n_matches = len(scored)
    scored.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))
    # pagination (100/page) : montrer TOUS les matches plutôt que tronquer au
    # premier écran — cf. diagnostic utilisateur, un plafond fixe (48) masquait
    # la quasi-totalité des correspondances sur une recherche large.
    total_pages = max(1, -(-n_matches // SEARCH_PAGE_SIZE))       # division entière arrondie au sup.
    try:
        page_num = max(1, int(page))
    except (ValueError, TypeError):
        page_num = 1
    page_num = min(page_num, total_pages)
    page_results = scored[(page_num - 1) * SEARCH_PAGE_SIZE: page_num * SEARCH_PAGE_SIZE]
    # X4 : état vide explicite — distinguer les causes déjà connues côté serveur
    empty_reason = None
    if not n_matches:
        if n_before_thresholds and any(v is not None for v in mins.values()):
            empty_reason = "thresholds"
        elif not used_local and not token:
            empty_reason = "no_source"
        elif not label.strip() and not base_labels and not styles and not genres:
            empty_reason = "no_criteria"
        else:
            empty_reason = "no_match"
    params = {"label": label.strip(), "genre": genres, "style": styles,
              "year_from": year_from.strip(), "year_to": year_to.strip(),
              "pages": npages,
              "base_metric": base_metric, "base_min": str(base_min or "").strip(),
              "label_min": str(label_min or "").strip(), "artist_min": str(artist_min or "").strip()}
    if page_num == 1:      # ne pas ré-enregistrer un doublon de l'historique à chaque page tournée
        hist = [e for e in load(_pu().search_hist, []) if e.get("params") != params]
        hist.insert(0, {"id": hashlib.md5(f"{time.time()}{params}".encode()).hexdigest()[:10],
                        "ts": time.strftime("%Y-%m-%d %H:%M"), "params": params,
                        "n": len(page_results), "n_matches": n_matches, "results": page_results,
                        "searched": base_labels, "dump_date": dump_date})
        save(_pu().search_hist, hist[:SEARCH_HIST_MAX])
    return frag(request, "partials/results.html", results=page_results,
                searched=base_labels, voted=_voted_map(), in_cart=_cart_ids(), dump_date=dump_date,
                empty_reason=empty_reason, has_token=bool(token), n_matches=n_matches,
                page=page_num, total_pages=total_pages)


_DISCO_CACHE = {}  # (kind, key) -> (ts, raw releases)


def _disco_taste_styles(c):
    cats = c.cfg.get("taste_categories", {})
    out, seen = [], set()
    for cid in ("1", "2"):
        for s in cats.get(cid, []):
            s = (s or "").strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
    return out


def _disco_resolve(c, kind, key):
    """-> (nom d'affichage, valeur de requête Discogs)."""
    if kind == "label":
        res = c.resolved.get(key) or {}
        name = res.get("discogs_name") or res.get("original") or key
        return name, name
    if key.startswith("id:"):
        name = c.artist_disp().get(key, key)
        return (name, name) if not str(name).startswith("id:") else (key, key[3:])
    e = c.artists_res.get(key) or {}
    name = e.get("discogs_name") or key
    return name, name


@app.get("/disco", response_class=HTMLResponse)
def disco_page(request: Request, kind: str = "artist", key: str = "",
               styles: str = "", pages: str = "3"):
    if kind not in ("artist", "label") or not key:
        return render(request, "pages/disco.html", active="", name="?", kind=kind, key=key,
                      results=None, mystyles=[], sel=[], error="Entité inconnue.")
    c = Ctx()
    token = c.cfg.get("token", "")
    name, qval = _disco_resolve(c, kind, key)
    try:
        npages = max(1, min(5, int(pages or 3)))
    except ValueError:
        npages = 3
    ck = (kind, key)
    hit = _DISCO_CACHE.get(ck)
    if hit and time.time() - hit[0] < 300:
        raw = hit[1]
    elif not token:
        return render(request, "pages/disco.html", active="", name=name, kind=kind, key=key,
                      results=None, mystyles=[], sel=[],
                      error="Token Discogs manquant (Ma patte → Connexions).")
    else:
        try:
            fn = discogs.search_label_releases if kind == "label" else discogs.search_artist_releases
            raw = fn(token, qval, fmt="Vinyl", max_pages=npages)
        except discogs.DiscogsError as e:
            return render(request, "pages/disco.html", active="", name=name, kind=kind, key=key,
                          results=None, mystyles=[], sel=[], error=str(e))
        if len(_DISCO_CACHE) > 60:
            _DISCO_CACHE.clear()
        _DISCO_CACHE[ck] = (time.time(), raw)

    sel = [s for s in (styles or "").split(",") if s.strip()]
    sel_lc = {s.lower() for s in sel}
    seen, scored = set(), []
    for r in raw:
        rid = r.get("id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        rstyles = r.get("style") or []
        if sel_lc and not any((s or "").lower() in sel_lc for s in rstyles):
            continue
        sc, det = c.album_score(r)
        thumb = r.get("cover_image") or r.get("thumb")
        lab1 = next((x for x in (r.get("label") or []) if x), "")
        scored.append({"raw": {"id": rid, "title": r.get("title", ""), "label1": lab1,
                               "style": rstyles, "catno": r.get("catno", ""),
                               "year": r.get("year", ""), "thumb": thumb, "uri": r.get("uri", "")},
                       "score": sc,
                       "detail": {"label": det.get("label"), "artist": det.get("artist"),
                                  "style": det.get("style")}})
    scored.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))
    return render(request, "pages/disco.html", active="", name=name, kind=kind, key=key,
                  results=scored[:120], mystyles=_disco_taste_styles(c), sel=sel,
                  voted=_voted_map(), in_cart=_cart_ids(), total_raw=len(raw))


# ---------------------------------------------------------------- panier = wantlist Discogs
def _cart_ids():
    return {str(x.get("id")) for x in load(_pu().cart, [])}


def _discogs_username(cfg, token):
    """Résout le pseudo Discogs une fois puis le met en cache dans la config
    (évite un aller-retour `/oauth/identity` à chaque ajout/retrait)."""
    user = cfg.get("discogs_username", "")
    if user:
        return user
    user = discogs.identity(token).get("username", "")
    if user:
        cfg["discogs_username"] = user
        store.save_config(cfg)
    return user


@app.get("/release_matches", response_class=HTMLResponse)
def release_matches(request: Request, a: str = "", t: str = ""):
    """Vinyles Discogs contenant cette track (une track peut être sortie sur
    plusieurs sorties — VA, rééditions...) — pour l'ajout à la wantlist depuis un
    DJ set, où on n'a résolu qu'un seul release_id à l'ingestion."""
    a, t = a.strip(), t.strip()
    token = _cfg().get("token", "")
    if not token:
        return HTMLResponse("<p class='small msg-err'>Token Discogs manquant.</p>")
    try:
        res = discogs.search(token, artist=a, track=t, per_page=25).get("results", [])
    except discogs.DiscogsError as e:
        return HTMLResponse(f"<p class='small msg-err'>{html.escape(str(e))}</p>")
    vinyl = [r for r in res if "vinyl" in " ".join(r.get("format") or []).lower()]
    rows = [{"id": r.get("id"), "title": r.get("title"), "label": r.get("label") or [],
             "year": r.get("year"), "format": r.get("format") or [],
             "thumb": r.get("cover_image") or r.get("thumb")} for r in vinyl[:12]]
    return frag(request, "partials/release_matches.html", rows=rows, a=a, t=t, in_cart=_cart_ids())


@app.get("/cart", response_class=HTMLResponse)
def cart_frag(request: Request):
    return frag(request, "partials/cart.html", cart=load(_pu().cart, []))


@app.post("/cart/add", response_class=HTMLResponse)
def cart_add(rid: str = Form(""), title: str = Form(""), artist: str = Form(""),
             thumb: str = Form(""), label: str = Form("")):
    rid = (rid or "").strip()
    if not rid:
        return HTMLResponse("<span class='small msg-err'>id manquant</span>")
    cfg = _cfg()
    token = cfg.get("token", "")
    if not token:
        return HTMLResponse("<span class='small msg-err'>Token Discogs manquant.</span>")
    try:
        user = _discogs_username(cfg, token)
        if not user:
            return HTMLResponse("<span class='small msg-err'>Identité Discogs illisible.</span>")
        discogs.add_to_wantlist(token, user, rid)
    except discogs.DiscogsError as e:
        return HTMLResponse(f"<span class='small msg-err'>{html.escape(str(e))}</span>")
    cart = load(_pu().cart, [])
    if rid not in {str(x.get("id")) for x in cart}:
        cart.insert(0, {"id": rid, "title": title.strip(), "artist": artist.strip(),
                        "thumb": thumb.strip(), "label": label.strip(),
                        "added_at": time.strftime("%Y-%m-%d")})
        save(_pu().cart, cart)
    return HTMLResponse("<span class='small ok'>✓ en wantlist</span>")


@app.post("/cart/remove", response_class=HTMLResponse)
def cart_remove(request: Request, rid: str = Form("")):
    rid = (rid or "").strip()
    cfg = _cfg()
    token = cfg.get("token", "")
    if token:
        try:
            user = _discogs_username(cfg, token)
            if user:
                discogs.remove_from_wantlist(token, user, rid)
        except discogs.DiscogsError as e:
            return HTMLResponse(f"<p class='small msg-err'>{html.escape(str(e))}</p>")
    cart = [x for x in load(_pu().cart, []) if str(x.get("id")) != rid]
    save(_pu().cart, cart)
    return frag(request, "partials/cart.html", cart=cart)


@app.post("/cart/sync", response_class=HTMLResponse)
def cart_sync(request: Request):
    """Recharge la wantlist Discogs réelle et remplace le cache local — la
    wantlist Discogs est la source de vérité, le cache local (`cart.json`)
    n'existe que pour l'affichage sans appel API à chaque page."""
    cfg = _cfg()
    token = cfg.get("token", "")
    if not token:
        return HTMLResponse("<p class='small msg-err'>Token Discogs manquant.</p>")
    try:
        user = _discogs_username(cfg, token)
        if not user:
            return HTMLResponse("<p class='small msg-err'>Identité Discogs illisible.</p>")
        items, page, pages = [], 1, 1
        while page <= pages and page <= 20:
            d = discogs.wants(token, user, page=page, per_page=100)
            items += d.get("wants", [])
            pages = d.get("pagination", {}).get("pages", 1)
            page += 1
    except discogs.DiscogsError as e:
        return HTMLResponse(f"<p class='small msg-err'>{html.escape(str(e))}</p>")
    cart = []
    for w in items:
        bi = w.get("basic_information", {}) or {}
        cart.append({"id": str(w.get("id")), "title": bi.get("title", ""),
                     "artist": ", ".join(a.get("name", "") for a in bi.get("artists") or []),
                     "label": ((bi.get("labels") or [{}])[0]).get("name", ""),
                     "thumb": bi.get("thumb") or bi.get("cover_image") or "",
                     "added_at": (w.get("date_added") or "")[:10]})
    save(_pu().cart, cart)
    return frag(request, "partials/cart.html", cart=cart)


@app.get("/cart/sellers", response_class=HTMLResponse)
def cart_sellers_frag(request: Request):
    cart = load(_pu().cart, [])
    titles = {str(x.get("id")): x for x in cart}
    cov = sellers.cart_coverage(titles.keys())
    rows = [{"u": u, "name": e.get("name", u), "country": e.get("country", ""),
             "n": len(hits), "items": [titles[h] for h in hits if h in titles]}
            for u, e, hits in cov[:20]]
    cat = sellers.load_catalog()
    scanned = sum(1 for e in cat.values() if e.get("last_scan"))
    return frag(request, "partials/cart_sellers.html", rows=rows, n_cart=len(cart),
                n_catalog=len(cat), n_scanned=scanned)


# ------------------------------------------------------------- 💬 retours UI
def _notes_sorted():
    return sorted(load(_pu().ui_notes, []), key=lambda n: n.get("ts", ""), reverse=True)


def _post_feedback_to_github(note):
    """Relaie une note sur l'issue GitHub permanente (#62) — best-effort : sans
    token configuré (RADAR_FEEDBACK_GH_TOKEN) ou en cas d'erreur réseau, la note
    reste de toute façon dans ui_notes.json, rien n'est perdu."""
    token = os.environ.get("RADAR_FEEDBACK_GH_TOKEN", "")
    if not token:
        return False
    body = f"**Nouveau retour** — `{note['page']}`\n\n"
    if note.get("target"):
        body += f"> à propos de : {note['target']}\n\n"
    body += f"{note['note']}\n\n_id: {note['id']}_"
    try:
        r = requests.post(
            f"https://api.github.com/repos/{FEEDBACK_GH_REPO}/issues/{FEEDBACK_GH_ISSUE}/comments",
            json={"body": body}, timeout=10,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json", "User-Agent": "Radar/1.0"})
        return r.ok
    except requests.RequestException:
        return False


@app.get("/feedback", response_class=HTMLResponse)
def feedback_page(request: Request):
    return render(request, "pages/feedback.html", active="", notes=_notes_sorted())


@app.post("/feedback/add", response_class=HTMLResponse)
def feedback_add(page: str = Form(""), target: str = Form(""), note: str = Form("")):
    note = note.strip()
    if not note:
        return HTMLResponse("<span class='small msg-err'>note vide</span>")
    n = {"id": "nt_" + hashlib.md5(f"{time.time()}{note}".encode()).hexdigest()[:10],
         "ts": time.strftime("%Y-%m-%d %H:%M"), "page": page.strip(), "target": target.strip(),
         "note": note, "status": "nouveau"}
    n["gh_posted"] = _post_feedback_to_github(n)
    notes = load(_pu().ui_notes, [])
    notes.insert(0, n)
    save(_pu().ui_notes, notes)
    return HTMLResponse("<span class='small ok'>✓ envoyé</span>")


@app.post("/feedback/status", response_class=HTMLResponse)
def feedback_status(request: Request, id: str = Form(""), status: str = Form("")):
    notes = load(_pu().ui_notes, [])
    for n in notes:
        if n.get("id") == id:
            n["status"] = status
    save(_pu().ui_notes, notes)
    return frag(request, "partials/feedback_list.html", notes=_notes_sorted())


@app.post("/feedback/retry", response_class=HTMLResponse)
def feedback_retry(request: Request, id: str = Form("")):
    notes = load(_pu().ui_notes, [])
    for n in notes:
        if n.get("id") == id:
            n["gh_posted"] = _post_feedback_to_github(n)
    save(_pu().ui_notes, notes)
    return frag(request, "partials/feedback_list.html", notes=_notes_sorted())


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
        bc = "/bc/go?" + urlencode({"a": tart, "t": ttl, "l": label1, "kind": "t"})
        rows.append({"pos": (t.get("position") or "").strip(), "title": ttl,
                     "play": play, "kind": kind, "bc": bc})
    return frag(request, "partials/tracklist.html", tracks=rows)


@app.get("/yt/first")
def yt_first(q: str = ""):
    """Redirige vers LA vidéo YouTube la plus pertinente (API Data si clé dispo),
    sinon vers la page de résultats YouTube."""
    q = (q or "").strip()
    if not q:
        return RedirectResponse("https://www.youtube.com", status_code=302)
    try:
        vid = ytcache.search_video(q, ytcache.youtube_keys(_cfg()))
        if vid:
            return RedirectResponse(f"https://www.youtube.com/watch?v={vid}", status_code=302)
    except (ytcache.QuotaExhausted, RuntimeError, requests.RequestException):
        pass
    return RedirectResponse(yt_search_url(q), status_code=302)


@app.get("/bc/go")
def bc_go(a: str = "", t: str = "", l: str = "", kind: str = "t"):
    """Redirige vers LA page Bandcamp la plus pertinente (API d'autocomplétion),
    sinon vers la page de recherche Bandcamp."""
    kind = "a" if kind == "a" else "t"
    try:
        hit = bandcamp.search(a, t, kind=kind, label=l)
        if hit and hit.get("url"):
            return RedirectResponse(hit["url"], status_code=302)
    except Exception:                       # noqa: BLE001 — repli toujours possible
        pass
    return RedirectResponse(bandcamp.search_url(a, t, kind), status_code=302)


RELEASE_META_TTL = 86400
# La pochette d'une sortie ne change (quasi) jamais, contrairement à la note/au prix —
# une fois trouvée, elle sort du cycle de rafraîchissement pour de bon (ou presque) au
# lieu de reconsommer le quota API (RELEASE_META_MAX_FETCH) juste pour la reconfirmer :
# le budget par chargement se concentre sur les sorties qui n'ont encore AUCUNE pochette,
# et leur nombre ne peut donc que croître au fil des visites (jamais retomber à zéro).
RELEASE_META_THUMB_TTL = 180 * 86400
RELEASE_META_MAX_FETCH = 20          # au-delà, on sert ce qu'on a plutôt que saturer le quota
_release_meta_lock = threading.Lock()


def _release_meta_ensure(rids):
    """Complète et renvoie le cache partagé release_meta pour `rids` (note, prix,
    nombre en vente, pochette) — ne demande à l'API que les ids manquants ou
    périmés (plafonné), écrit le cache UNE SEULE fois, sous verrou, en relisant
    juste avant d'écrire pour fusionner ce qu'un autre process aurait ajouté
    entretemps. Partagé par `/release/meta` (grilles de résultats) et
    `/inbox/meta` (file Nouveautés) — même appel Discogs, juste deux gabarits
    de réponse différents selon qui l'utilise."""
    cache = load(_pu().release_meta, {})
    now = time.time()
    missing = []
    for rid in rids:
        e = cache.get(rid)
        if not e:
            missing.append(rid)
            continue
        ttl = RELEASE_META_THUMB_TTL if e.get("thumb") else RELEASE_META_TTL
        if now - e.get("ts", 0) > ttl:
            missing.append(rid)
    if missing:
        token = _cfg().get("token", "")
        fetched = {}
        for rid in missing[:RELEASE_META_MAX_FETCH]:
            rating = rcount = nfs = low = thumb = None
            try:
                d = discogs.release(int(rid), token=token)
                rt = (d.get("community") or {}).get("rating") or {}
                rating, rcount = rt.get("average"), rt.get("count")
                nfs, low = d.get("num_for_sale"), d.get("lowest_price")
                thumb = d.get("thumb") or ""
            except discogs.DiscogsError:
                pass
            if not thumb:
                # pas de nouvelle pochette cette fois (erreur, ou Discogs n'en a
                # toujours pas) : ne jamais effacer une pochette déjà connue.
                thumb = (cache.get(rid) or {}).get("thumb") or ""
            fetched[rid] = {"ts": now, "rating": rating, "rcount": rcount, "nfs": nfs, "low": low,
                            "thumb": thumb}
            time.sleep(1.1)
        with _release_meta_lock:
            cache = load(_pu().release_meta, {})   # relu sous le verrou : fusionne un accès concurrent
            cache.update(fetched)
            if len(cache) > 4000:
                for k in sorted(cache, key=lambda k: cache[k].get("ts", 0))[:1200]:
                    cache.pop(k, None)
            save(_pu().release_meta, cache)
    return cache


@app.get("/release/meta", response_class=HTMLResponse)
def release_meta_batch(ids: str = ""):
    """Une seule requête pour toute une grille de résultats (cf. diagnostic
    P3) — remplace le hx-get par carte qui déclenchait jusqu'à 48 appels API
    et 48 réécritures du cache partagé sans verrou. Répond en pastilles
    hx-swap-oob, une par carte (#cover-meta-{id}) — et complète la pochette
    (#cover-{id}) pour les résultats du référentiel local, qui n'a aucune URL
    d'image (cf. discogs_dump.search_local) : même appel API déjà fait pour
    la note/le prix, zéro coût supplémentaire pour en extraire aussi `thumb`."""
    rids = [r for r in dict.fromkeys(i.strip() for i in ids.split(",")) if r.isdigit()]
    if not rids:
        return HTMLResponse("")
    cache = _release_meta_ensure(rids)
    tpl = templates.get_template("partials/release_meta.html")
    return HTMLResponse("".join(tpl.render({"m": cache.get(rid) or {}, "rid": rid}) for rid in rids))


@app.get("/inbox/meta", response_class=HTMLResponse)
def inbox_meta_batch(ids: str = ""):
    """Même mécanique que /release/meta (cache partagé, ids manquants/périmés
    seulement), pour la file Nouveautés (/veille) : un match de règle n'est pas
    une annonce précise (pas de vendeur/état connus — cf. Pièges connus,
    marketplace Discogs inobtenable en direct), mais le prix le plus bas et le
    nombre d'annonces sont un signal du catalogue Discogs, déjà utilisé sur les
    pastilles de la Recherche. Répond en pastilles hx-swap-oob (#inbox-meta-{id}),
    distinctes de #cover-{id} (la file Nouveautés n'a pas de grande pochette carrée
    à remplacer, juste une ligne compacte)."""
    rids = [r for r in dict.fromkeys(i.strip() for i in ids.split(",")) if r.isdigit()]
    if not rids:
        return HTMLResponse("")
    cache = _release_meta_ensure(rids)
    tpl = templates.get_template("partials/inbox_meta.html")
    return HTMLResponse("".join(tpl.render({"m": cache.get(rid) or {}, "rid": rid}) for rid in rids))


# ============================================================ 📻 Nouveautés (+ vendeurs + reco)
def _inbox(request, path, source_key, key_ns, mins=30):
    items = load(path, [])
    if not items:
        return frag(request, "partials/inbox.html", rows=[], key_ns=key_ns, sources=[],
                    mins=mins, n_total=0)
    c = Ctx()
    meta_cache = load(_pu().release_meta, {})
    scored = []
    for it in items:
        sc, det = c.album_score({"title": f"{it.get('artist','')} - {it.get('title','')}",
                                 "label": [it["label"]] if it.get("label") else [],
                                 "style": it.get("style") or []})
        rid = str(it.get("release_id") or it.get("listing_id") or "")
        # le scan (règles ou vendeurs) pose déjà sa propre pochette sur l'item — cf.
        # job_scan_veille/job_scan_sellers (crate_jobs.py) — le cache release_meta n'est
        # qu'un repli pour les items plus anciens scannés avant l'ajout de ce champ.
        thumb = it.get("thumb") or (meta_cache.get(rid) or {}).get("thumb") or ""
        scored.append({"it": it, "score": sc, "det": det, "src": it.get(source_key), "thumb": thumb})
    scored.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))
    rows = [r for r in scored if (r["score"] or 0) >= mins]
    return frag(request, "partials/inbox.html", rows=rows[:150], key_ns=key_ns, mins=mins,
                sources=sorted({r["src"] for r in scored if r["src"]}), n_total=len(items))


@app.get("/inbox/{kind}", response_class=HTMLResponse)
def inbox(request: Request, kind: str, mins: int = 30):
    if kind == "veille":
        return _inbox(request, _pu().veille_new, "rule", "veille", mins)
    return _inbox(request, _pu().sellers_new, "seller", "sellers", mins)


@app.post("/inbox/{kind}/clear", response_class=HTMLResponse)
def inbox_clear(request: Request, kind: str):
    save(_pu().veille_new if kind == "veille" else _pu().sellers_new, [])
    return inbox(request, kind)


@app.post("/inbox/{kind}/dismiss", response_class=HTMLResponse)
def inbox_dismiss(request: Request, kind: str, rid: str = Form("")):
    path = _pu().veille_new if kind == "veille" else _pu().sellers_new
    idf = "release_id" if kind == "veille" else "listing_id"
    save(path, [x for x in load(path, []) if str(x.get(idf)) != rid])
    return inbox(request, kind)


@app.get("/veille", response_class=HTMLResponse)
def veille_page(request: Request, saved: int = 0):
    c = _cfg()
    return render(request, "pages/veille.html", active="veille", saved=saved,
                  rules=c.get("veille_rules", []), watchlist=c.get("watchlist", []),
                  sellers=c.get("sellers", []), year=CURRENT_YEAR,
                  v_last=max((v.get("last_scan", "") for v in load(_pu().veille_seen, {}).values()), default=""),
                  s_last=max((v.get("last_scan", "") for v in load(_pu().sellers_seen, {}).values()), default=""))


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
    return RedirectResponse("/veille?saved=1", status_code=303)


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
        q = load(_pu().pending_enrich, {})
        q.setdefault("labels", []).append(name)
        save(_pu().pending_enrich, q)
        jobs.launch("enrich")
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
            q = load(_pu().pending_enrich, {})
            q.setdefault("artists", []).append(name)
            save(_pu().pending_enrich, q)
            jobs.launch("enrich")
            store.save_config(c)
    return HTMLResponse("<span class='small muted'>✓ ajouté</span>")


# --------------------------------------------------------------------- recos (dans Mes labels & artistes)
@app.get("/univers/reco/labels", response_class=HTMLResponse)
def reco_labels_frag(request: Request):
    c = Ctx()
    base = {normalize_label(x) for x in c.cfg.get("labels", [])}
    rows = []
    for r in c.reco_rows():
        if r["key"] not in base:
            rows.append(r)
    # candidats issus du graphe (labels où tes artistes ont sorti, absents de la base) : rang
    # de proximité, pas une note /100 — le score de graphe n'est pas sur une échelle absolue
    # ni bornée, l'afficher à côté d'un vrai score /100 avec le même badge induisait en erreur
    # (cf. diagnostic N2). c.graph_rescore()["labels"] est déjà trié par score décroissant.
    graph_rows = []
    for lk, v in c.graph_rescore()["labels"].items():
        if lk in base:
            continue
        graph_rows.append({"name": v["name"], "key": lk, "aff": None,
                           "owned": 0, "want": 0, "corpus": 0, "artists": v["n_seeds"],
                           "seeds": ", ".join(v["seeds"])})
    for i, r in enumerate(graph_rows):
        r["rank"] = i + 1
    return frag(request, "partials/reco_labels.html", reco=rows[:30],
                graph_reco=graph_rows[:30], n_reco=len(rows), n_graph=len(graph_rows))


@app.get("/univers/reco/artists", response_class=HTMLResponse)
def reco_artists_frag(request: Request):
    c = Ctx()
    g = c.graph_rescore()["artists"]
    # candidats retenus par proximité de graphe (pertinence), mais AFFICHÉS triés par note
    # (le badge visible) : sinon la liste semble mal triée puisque le tri interne de `g`
    # porte sur un score de proximité brut, différent du score affiché (cf. retour utilisateur).
    rows = [{"name": v["name"], "key": k, "prox": round(v["score"]), "note": c.ascore.get(k, 0),
             "why": ", ".join(v["why"])}
            for k, v in list(g.items())[:40] if not str(v["name"]).startswith("id:")]
    rows.sort(key=lambda r: -r["note"])
    return frag(request, "partials/reco_artists.html", reco=rows, n_reco=len(g))


def _review_rows(resolved, status):
    """[{key, original, discogs_name, discogs_id, candidates}] pour les
    entrées dans ce statut — alimente la section "À vérifier" (diagnostic C3) :
    approx = deviné par l'API, à confirmer/rejeter ; not_found = jamais
    identifié, purement informationnel (rien à confirmer)."""
    out = []
    for k, e in (resolved or {}).items():
        if e.get("status") == status:
            out.append({"key": k, "original": e.get("original") or k,
                        "discogs_name": e.get("discogs_name"), "discogs_id": e.get("discogs_id"),
                        "candidates": e.get("candidates") or []})
    out.sort(key=lambda r: r["original"].lower())
    return out


def _review_ctx(c):
    return {"labels_approx": _review_rows(c.resolved, "approx"),
            "labels_not_found": _review_rows(c.resolved, "not_found"),
            "artists_approx": _review_rows(c.artists_res, "approx"),
            "artists_not_found": _review_rows(c.artists_res, "not_found")}


@app.post("/univers/review/{kind}/{action}", response_class=HTMLResponse)
def univers_review_action(request: Request, kind: str, action: str, key: str = Form("")):
    if kind not in ("label", "artist") or action not in ("confirm", "reject"):
        return HTMLResponse("", status_code=404)
    path = _pu().resolved if kind == "label" else _pu().artists_res
    data = load(path, {})
    e = data.get(key)
    if e and e.get("status") == "approx":
        e["status"] = "confirmed" if action == "confirm" else "not_found"
        e["candidates"] = []
        e["reviewed_by"] = "user"
        save(path, data)
    return frag(request, "partials/review.html", review=_review_ctx(Ctx()))


# ============================================================ 🌐 Mes labels & artistes
@app.get("/univers", response_class=HTMLResponse)
def univers_page(request: Request, tab: str = "labels"):
    c = Ctx()
    ac = c.cfg.get("artist_categories", {})
    label_graphs = load(_pu().label_graphs, [])
    artist_graphs = load(_pu().artist_graphs, [])
    review = _review_ctx(c)
    return render(request, "pages/univers.html", active="univers", tab=tab, cfg=c.cfg,
                  n_labels=len(c.cfg.get("labels", [])), n_profiled=len(c.profile),
                  n_artists=sum(len(v) for v in ac.values()),
                  n_sets=len([r for r in c.corpus if r.get("source") == "djset"]),
                  n_cart=len(load(_pu().cart, [])),
                  label_graphs=label_graphs,
                  artist_graphs=artist_graphs,
                  review=review,
                  # total (approx + not_found), pas seulement approx : sinon le panneau
                  # "À vérifier" reste masqué (n_review=0) quand tout est not_found — cas
                  # réel constaté (281 not_found, 0 approx) où le lien depuis /patte
                  # ("jamais identifiés" -> /univers) menait à un panneau invisible (diag. Lot 5 C3).
                  n_review=(len(review["labels_approx"]) + len(review["artists_approx"])
                            + len(review["labels_not_found"]) + len(review["artists_not_found"])),
                  top_aff="\n".join(_pick_base_labels(c, "aff", None, 5)),
                  top_reco="\n".join(_pick_base_labels(c, "reco", None, 5)))


LABELS_PAGE_SIZE = 25


@app.get("/univers/labels/table", response_class=HTMLResponse)
def univers_labels_table(request: Request, flt: str = "", page: int = 1):
    c = Ctx()
    rows = _base_labels_ranked(c)
    if flt:
        f = flt.lower()
        rows = [r for r in rows if f in r["disp"].lower()]
    total = len(rows)
    pages = max(1, -(-total // LABELS_PAGE_SIZE))
    page = max(1, min(page, pages))
    shown = rows[(page - 1) * LABELS_PAGE_SIZE: page * LABELS_PAGE_SIZE]
    for r in shown:
        r["url"] = (f"https://www.discogs.com/label/{r['id']}" if r.get("id")
                    else f"https://www.discogs.com/search/?q={quote_plus(r['disp'])}&type=label")
    return frag(request, "partials/labels_table.html", rows=shown, total=total,
                page=page, pages=pages, flt=flt)


@app.post("/univers/labels/add", response_class=HTMLResponse)
def univers_labels_add(request: Request, name: str = Form("")):
    c = _cfg()
    name = name.strip()
    ok, msg = False, "Identifiant vide."
    if name:
        if normalize_label(name) in {normalize_label(x) for x in c["labels"]}:
            msg = f"« {name} » est déjà dans ta base."
        else:
            c["labels"].append(name)
            q = load(_pu().pending_enrich, {})
            q.setdefault("labels", []).append(name)
            save(_pu().pending_enrich, q)
            jobs.launch("enrich")
            store.save_config(c)
            ok, msg = True, f"✓ « {name} » ajouté."
    return HTMLResponse(f"<span class='small {'ok' if ok else 'notice warn'}'>{html.escape(msg)}</span>")


@app.post("/univers/labels/remove", response_class=HTMLResponse)
def univers_labels_remove(request: Request, name: str = Form(""), flt: str = Form(""),
                          page: int = Form(1)):
    c = _cfg()
    c["labels"] = [l for l in c["labels"] if l != name]
    store.save_config(c)
    return univers_labels_table(request, flt=flt, page=page)


def _graph_link_facts(deg, weight, unit):
    facts = []
    if deg:
        facts.append(f"{deg} lien{'s' if deg > 1 else ''} dans le graphe")
    if weight:
        facts.append(f"{weight} {unit}")
    return facts


def _graph_extras(entry, kind):
    """(notes, infos) par nœud pour le rendu d'un graphe — notes /100 et détails
    de la fiche plein écran. Uniquement des données déjà en cache : aucun appel API."""
    c = Ctx()
    nodes = entry.get("nodes") or {}
    deg, weight = {}, {}
    for e in entry.get("edges") or []:
        for k in (e.get("a"), e.get("b")):
            if k in nodes:
                deg[k] = deg.get(k, 0) + 1
                weight[k] = weight.get(k, 0) + int(e.get("w") or 0)
    notes, infos = {}, {}
    if kind == "label":
        from .radar import discogs_dump as dd
        owned = c.collection.get("label_counts", {})
        reco, gl = c.reco_index, c.graph_rescore()["labels"]
        dump_styles = dd.label_style_counts(nodes)
        for k in nodes:
            dstyles = dump_styles.get(k)
            prof = c.profile.get(k)
            style_counts = dstyles or (prof or {}).get("style_counts") or {}
            notes[k] = None
            if dstyles or prof:
                notes[k], _cov = c.affinity_score({"style_counts": style_counts})
            styles = sorted(style_counts.items(), key=lambda kv: -kv[1])
            facts = _graph_link_facts(deg.get(k, 0), weight.get(k, 0), "artiste(s) en commun")
            if dstyles:
                facts.append("profil : catalogue Discogs complet")
            elif prof and prof.get("sampled"):
                facts.append(f"{prof['sampled']} sorties profilées (échantillon)")
            else:
                facts.append("pas encore profilé")
            if owned.get(k):
                facts.append(f"{owned[k]} dans ta collection")
            if reco.get(k):
                facts.append(f"reco {reco[k]}/100")
            seeds = (gl.get(k) or {}).get("seeds") or []
            infos[k] = {"styles": [s for s, _ in styles[:5]], "facts": facts,
                        "why": [f"tes artistes : {', '.join(seeds[:4])}"] if seeds else []}
    else:
        tiers, asc = c.artist_tier_map(), c.ascore
        ga = c.graph_rescore()["artists"]
        tname = {"1": "Cœur", "2": "Aimé"}
        for k in nodes:
            notes[k] = asc.get(k, 0)
            facts = _graph_link_facts(deg.get(k, 0), weight.get(k, 0), "crédits partagés")
            if tiers.get(k):
                facts.insert(0, tname.get(tiers[k], ""))
            infos[k] = {"styles": [], "facts": [f for f in facts if f],
                        "why": (ga.get(k) or {}).get("why") or []}
    return notes, infos


def _label_graph_render(request, entry):
    pos = labelgraph.layout(entry["nodes"])
    notes, infos = _graph_extras(entry, "label")
    return frag(request, "partials/label_graph_svg.html", graph=entry, pos=pos,
                kind="label", notes=notes, infos=infos)


@app.post("/univers/labels/graph/build", response_class=HTMLResponse)
async def univers_label_graph_build(request: Request):
    f = await request.form()
    seeds = [s.strip() for s in f.get("seeds", "").splitlines() if s.strip()]
    if not seeds:
        return HTMLResponse("<p class='notice warn small'>Au moins un label de départ.</p>")
    try:
        depth = max(1, min(2, int(f.get("depth") or 1)))
    except ValueError:
        depth = 1
    try:
        min_shared = max(1, int(f.get("min_shared") or 1))
    except ValueError:
        min_shared = 1
    c = Ctx()
    built = labelgraph.build(c.graph or {}, seeds, depth=depth, min_shared=min_shared)
    if len(built["nodes"]) <= len(seeds):
        return HTMLResponse(
            "<p class='notice warn small'>Aucun voisin trouvé — ces labels n'ont pas (ou pas assez) "
            "d'artistes en commun dans le graphe. Reconstruis le graphe producteur avec plus de graines "
            "(onglet Base d'artistes), ou baisse le seuil.</p>")
    entry = {"id": hashlib.md5(f"{time.time()}{seeds}".encode()).hexdigest()[:10],
             "ts": time.strftime("%Y-%m-%d %H:%M"), "depth": depth, "min_shared": min_shared,
             **built}
    hist = load(_pu().label_graphs, [])
    hist.insert(0, entry)
    save(_pu().label_graphs, hist[:15])
    return _label_graph_render(request, entry)


@app.get("/univers/labels/graph/{gid}", response_class=HTMLResponse)
def univers_label_graph_show(request: Request, gid: str):
    entry = next((e for e in load(_pu().label_graphs, []) if e.get("id") == gid), None)
    if not entry:
        return HTMLResponse("<p class='muted small'>Graphe introuvable (supprimé ?).</p>")
    return _label_graph_render(request, entry)


ARTISTS_PAGE_SIZE = 25


@app.get("/univers/artists/table", response_class=HTMLResponse)
def univers_artists_table(request: Request, flt: str = "", hide: str = "", page: int = 1):
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
        rows.append({"name": name, "note": note, "cat": catn.get(t, "—"), "key": ck})
    rows.sort(key=lambda r: -r["note"])
    total = len(rows)
    pages = max(1, -(-total // ARTISTS_PAGE_SIZE))
    page = max(1, min(page, pages))
    shown = rows[(page - 1) * ARTISTS_PAGE_SIZE: page * ARTISTS_PAGE_SIZE]
    return frag(request, "partials/artists_table.html", rows=shown, n=total,
                page=page, pages=pages, flt=flt, hide=hide)


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
        q = load(_pu().pending_enrich, {})
        q.setdefault("artists", []).append(name)
        save(_pu().pending_enrich, q)
        jobs.launch("enrich")
    store.save_config(c)
    return HTMLResponse("<span class='small ok'>✓</span>")


def _artist_graph_render(request, entry):
    pos = labelgraph.layout(entry["nodes"])
    notes, infos = _graph_extras(entry, "artist")
    return frag(request, "partials/label_graph_svg.html", graph=entry, pos=pos,
                kind="artist", notes=notes, infos=infos)


@app.get("/univers/artists/suggest", response_class=HTMLResponse)
def univers_artists_suggest(request: Request, q: str = ""):
    c = Ctx()
    disp, tiers = c.artist_disp(), c.artist_tier_map()
    term = store.normalize_label(q)
    rows = sorted(
        ({"name": disp.get(ck, ck), "tier": t} for ck, t in tiers.items()
         if not str(disp.get(ck, ck)).startswith("id:")
         and (not term or term in store.normalize_label(disp.get(ck, ck)))),
        key=lambda r: r["name"].lower())[:60]
    return frag(request, "partials/artist_suggest.html", rows=rows)


@app.post("/univers/artists/graph/build", response_class=HTMLResponse)
async def univers_artist_graph_build(request: Request):
    f = await request.form()
    names = [s.strip() for s in f.get("seeds", "").splitlines() if s.strip()]
    if not names:
        return HTMLResponse("<p class='notice warn small'>Choisis au moins un artiste de départ.</p>")
    c = Ctx()
    disp = c.artist_disp()
    seed_items = list({c.canon_artist_key(n): disp.get(c.canon_artist_key(n), n) for n in names}.items())
    built = artistgraph.build(c.graph or {}, seed_items)
    if len(built["nodes"]) <= len(seed_items):
        return HTMLResponse(
            "<p class='notice warn small'>Aucun voisin trouvé — reconstruis d'abord le graphe producteur "
            "avec ces graines (Constructeur de graphe ci-dessus), ou choisis d'autres graines.</p>")
    entry = {"id": hashlib.md5(f"{time.time()}{seed_keys}".encode()).hexdigest()[:10],
             "ts": time.strftime("%Y-%m-%d %H:%M"), "depth": 1, "min_shared": None,
             **built}
    hist = load(_pu().artist_graphs, [])
    hist.insert(0, entry)
    save(_pu().artist_graphs, hist[:15])
    return _artist_graph_render(request, entry)


@app.get("/univers/artists/graph/{gid}", response_class=HTMLResponse)
def univers_artist_graph_show(request: Request, gid: str):
    entry = next((e for e in load(_pu().artist_graphs, []) if e.get("id") == gid), None)
    if not entry:
        return HTMLResponse("<p class='muted small'>Graphe introuvable (supprimé ?).</p>")
    return _artist_graph_render(request, entry)


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


@app.post("/univers/sets/delete", response_class=HTMLResponse)
def univers_sets_delete_track(rid: str = Form("")):
    """Retire UNE track de corpus (source djset) — page Mes sets. Elle ne
    compte alors plus dans ascore/reco_rows ni comme graine du graphe
    (mode taste)."""
    if not rid:
        return HTMLResponse("")
    corpus = load(_pu().corpus, [])
    new_corpus = [r for r in corpus if not (r.get("source") == "djset" and track_row_id(r) == rid)]
    if len(new_corpus) != len(corpus):
        save(_pu().corpus, new_corpus)
    return HTMLResponse("")


@app.post("/univers/labels/import", response_class=HTMLResponse)
async def univers_labels_import(request: Request, file: UploadFile, replace: str = Form("")):
    raw = (await file.read()).decode("utf-8", "ignore")
    names = [line.split(",")[0].strip().strip('"')
             for i, line in enumerate(io.StringIO(raw)) if i and line.split(",")[0].strip()]
    c = _cfg()
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
    return HTMLResponse(f"<span class='small ok'>✓ {added} label(s) ajouté(s) "
                        f"(base : {len(c['labels'])}).</span>")


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
              "ingest_djsets", "resolve_artists", "canonicalize", "enrich", "scan_catalog",
              "import_discogs_dump", "scan_recos", "publish_recos", "clean_recos"}
JOB_PARAMS = {"ingest_youtube": {"deep": True}, "ingest_spotify": {"deep": True},
              "ingest_bandcamp": {"deep": True}}


@app.post("/jobs/{name}/launch", response_class=HTMLResponse)
def job_launch(name: str, force: str = Form("")):
    if name == "ingest_djsets":
        srcs = [s.strip() for s in (_cfg().get("djset_sources") or "").splitlines() if s.strip()]
        if not srcs:
            return HTMLResponse("<div id='job-ingest_djsets' class='notice warn small'>"
                                "Aucune source DJ — renseigne-les dans « Mes sources → DJ sets ».</div>")
        d = os.path.join(store.paths.JOBS_DIR, store.current_uid())
        os.makedirs(d, exist_ok=True)
        save(os.path.join(d, "djsets.input.json"),
             {"sources": srcs, "max_per_source": 25, "min_minutes": 35,
              "require_hint": True, "deep": True})
    if name in VALID_JOBS:
        # le paramètre "force" (bouton "forcer" de scan_catalog/import_discogs_dump,
        # envoyé via hx-vals) n'était jamais lu ici : la route ignorait tout hors de
        # JOB_PARAMS, donc "forcer" relançait le job SANS le flag -> job_import_discogs_dump
        # retombait sur son test "déjà à jour" comme un lancement normal.
        params = dict(JOB_PARAMS.get(name, {}))
        if force:
            params["force"] = True
        jobs.launch(name, params)
    return job_status_frag(name)


@app.post("/jobs/{name}/stop", response_class=HTMLResponse)
def job_stop(name: str):
    if name in VALID_JOBS:
        jobs.stop(name)
    return job_status_frag(name)


# X5 : durée typique annoncée dès le lancement (jobs longs seulement — les autres
# se voient bien assez à leur barre de progression).
JOB_DURATION_HINTS = {
    "import_discogs_dump": "~1 h 45 (dump complet Discogs)",
    "scan_catalog": "jusqu'à ~1 h au premier passage, plus rapide ensuite",
    "build_graph": "de quelques minutes à ~1 h selon la taille du graphe",
    "ingest_djsets": "quelques minutes par source",
}
JOB_LONG_PAGE_NOTE_S = 600  # au-delà, rappeler qu'on peut fermer la page


def _job_elapsed_s(s):
    started = s.get("started_at")
    if not started:
        return None
    try:
        return max(0.0, time.time() - datetime.fromisoformat(started).timestamp())
    except ValueError:
        return None


def _fmt_duration_s(secs):
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs} s"
    m, sec = divmod(secs, 60)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d}"


@app.get("/jobs/{name}/status", response_class=HTMLResponse)
def job_status_frag(name: str):
    s = jobs.status(name)
    if not s:
        return HTMLResponse(f"<span id='job-{name}'></span>")
    done, total = s.get("done", 0), s.get("total", 0) or 1
    pct = min(100, round(100 * done / total))
    run, err, queued = s.get("running"), s.get("error"), s.get("queued")
    msg = html.escape(str(s.get("message") or ""))
    elapsed = _job_elapsed_s(s)
    hint = JOB_DURATION_HINTS.get(name)
    stopbtn = (f"<button type='button' class='small btn-stop' hx-post='/jobs/{name}/stop' "
               f"hx-target='#job-{name}' hx-swap='outerHTML' hx-confirm='Arrêter ce job ?'>■ arrêter</button>")
    if err:
        inner = f"<span class='notice warn small'>{html.escape(str(err))}</span>"
    elif queued:
        hint_txt = f" · dure généralement {hint}" if hint else ""
        inner = f"<div class='job-status'><span class='small muted'>🕓 {msg}{hint_txt}</span>{stopbtn}</div>"
    elif run:
        subbar = ""
        sub_total = s.get("sub_total")
        if sub_total:
            sub_done = s.get("sub_done", 0)
            spct = min(100, round(100 * sub_done / sub_total))
            sub_label = html.escape(str(s.get("sub_label") or ""))
            subbar = (f"<div class='small muted' style='margin-top:4px'>{sub_label} · {sub_done}/{sub_total} article(s)</div>"
                      f"<div class='progress'><i style='width:{spct}%'></i></div>")
        eta_txt = ""
        if elapsed and elapsed > 5 and done and s.get("total") and done < s["total"]:
            rate = done / elapsed
            if rate > 0:
                eta_txt = f" · reste ~{_fmt_duration_s((s['total'] - done) / rate)}"
        elif hint:
            eta_txt = f" · dure généralement {hint}"
        close_note = ""
        if elapsed and elapsed > JOB_LONG_PAGE_NOTE_S:
            close_note = ("<div class='small muted' style='margin-top:4px'>"
                          "Tu peux fermer cette page, le job continue en fond sur le serveur.</div>")
        inner = (f"<div class='job-status'><span class='small muted'>⏳ {msg} · {done}/{s.get('total', 0)}{eta_txt}</span>{stopbtn}</div>"
                 f"<div class='progress'><i style='width:{pct}%'></i></div>{subbar}{close_note}")
    else:
        inner = f"<span class='small muted'>✓ {msg or 'terminé'}</span>"
    poll_s = 10 if (elapsed and elapsed > 30) else 2
    poll = (f"hx-get='/jobs/{name}/status' hx-trigger='every {poll_s}s' hx-swap='outerHTML'"
            if (run or queued) else "")
    return HTMLResponse(f"<div id='job-{name}' {poll}>{inner}</div>")


# ============================================================ 🎛️ Réglages
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0):
    c = Ctx()
    return render(request, "pages/settings.html", active="settings", cfg=c.cfg,
                  sc=c.scoring, saved=saved)


def _catalog_rows():
    cat = sellers.ensure_seeded()
    rows = [dict(u=u, **e) for u, e in cat.items()]
    rows.sort(key=lambda r: (not r.get("active"), r.get("country", ""),
                             (r.get("name") or r["u"]).lower()))
    return rows, cat


@app.get("/discogs_dump", response_class=HTMLResponse)
def discogs_dump_frag(request: Request):
    from .radar import discogs_dump as dd
    return frag(request, "partials/discogs_dump.html", meta=dd.get_meta())


@app.get("/sellers/catalog", response_class=HTMLResponse)
def sellers_catalog_frag(request: Request):
    rows, cat = _catalog_rows()
    return frag(request, "partials/sellers_catalog.html", rows=rows,
                n_active=sum(1 for r in rows if r.get("active")),
                n_scanned=sum(1 for r in rows if r.get("last_scan")),
                job=jobs.status("scan_catalog"))


@app.post("/sellers/catalog/toggle", response_class=HTMLResponse)
def sellers_catalog_toggle(request: Request, u: str = Form("")):
    cat = sellers.load_catalog()
    if u in cat:
        cat[u]["active"] = not cat[u].get("active")
        cat[u].pop("fails", None)
        sellers.save_catalog(cat)
    return sellers_catalog_frag(request)


@app.post("/sellers/catalog/add", response_class=HTMLResponse)
def sellers_catalog_add(request: Request, u: str = Form(""), name: str = Form("")):
    u = u.strip().lstrip("@")
    cat = sellers.load_catalog()
    if u and u not in cat:
        cat[u] = {"name": name.strip() or u, "country": "", "city": "", "focus": "",
                  "type": "custom", "source": "manuel", "active": True, "verified": None,
                  "n_items": None, "n_new": None, "last_scan": None}
        sellers.save_catalog(cat)
    return sellers_catalog_frag(request)


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
    for grp, keys in (("reco", ("collection", "corpus", "artist", "affinity", "want_factor", "db_link")),
                      ("album", ("label", "artist", "style", "artist_max_vs_mean")),
                      ("artist_score", ("manual", "corpus", "collection", "graph", "djset", "label_link")),
                      ("recos", ("min_score", "max_new_releases"))):
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
