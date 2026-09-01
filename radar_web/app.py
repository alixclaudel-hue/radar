"""Radar — interface FastAPI + HTMX. Données PARTAGÉES avec l'appli Streamlit.
Lancement :  uvicorn radar_web.app:app --reload --port 8600

Nav : 🧠 Ma patte musicale · 🔍 Recherche ciblée · 📻 Veille Discogs · 🌐 Mon univers · 🎛️ Réglages
"""
import hashlib
import hmac
import html
import io
import os
import re
import secrets
import time
from typing import List
from urllib.parse import quote_plus

import requests
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .radar import accounts, artistgraph, discogs, jobs, labelgraph, learn, paths, store, vocab, ytcache
from .radar.scoring import Ctx, real_tracks, yt_search_url
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
    """Ni APP_PASSWORD ni compte -> pas d'authentification (CI, dev local)."""
    return not os.environ.get("APP_PASSWORD") and accounts.count() == 0


def _make_token(uid, exp):
    exp = int(exp)
    sig = hmac.new(SESSION_SECRET, f"{uid}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{uid}.{exp}.{sig}"


def _https(request):
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _set_session(resp, uid, request):
    resp.set_cookie(COOKIE, _make_token(uid, time.time() + AUTH_TTL), max_age=AUTH_TTL,
                    httponly=True, samesite="lax", secure=_https(request))


def _parse_token(tok):
    try:
        uid, exp, sig = (tok or "").split(".")
        exp = int(exp)
    except ValueError:
        return None
    good = hmac.new(SESSION_SECRET, f"{uid}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good) or time.time() >= exp:
        return None
    return uid


def _req_uid(request):
    if _dev_mode():
        return paths.DEFAULT_UID
    uid = _parse_token(request.cookies.get(COOKIE, ""))
    return uid if uid and accounts.get(uid) else None


@app.middleware("http")
async def _guard(request: Request, call_next):
    p = request.url.path
    if p.startswith("/static") or p in ("/login", "/health", "/register"):
        return await call_next(request)
    uid = _req_uid(request)
    if not uid:
        if request.headers.get("hx-request"):
            return HTMLResponse("Session expirée — <a href='/login'>reconnexion</a>", status_code=401)
        return RedirectResponse("/login", status_code=303)
    store.set_current_uid(uid)          # lu par load_config() / Ctx() / _pu() / jobs.launch()
    resp = await call_next(request)
    if not _dev_mode():
        _set_session(resp, uid, request)
    return resp


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, bad: int = 0):
    if _req_uid(request):
        return RedirectResponse("/", status_code=303)
    msg = "<p class='notice warn'>Identifiants incorrects.</p>" if bad else ""
    return HTMLResponse(f"""<!doctype html><meta charset=utf-8>
<link rel=stylesheet href=/static/app.css><div class=wrap style='max-width:360px'>
<p class=brand style='font-size:32px'>Rada<b>r</b></p>{msg}
<form method=post action=/login>
  <div class=field><label>Identifiant</label><input name=username autofocus autocapitalize=off></div>
  <div class=field><label>Mot de passe</label><input type=password name=pw></div>
  <button class=primary type=submit>Entrer</button>
</form></div>""")


@app.post("/login")
def login(request: Request, username: str = Form(""), pw: str = Form("")):
    uid = accounts.verify(username, pw)
    if uid:
        r = RedirectResponse("/", status_code=303)
        _set_session(r, uid, request)
        return r
    return RedirectResponse("/login?bad=1", status_code=303)


@app.get("/logout")
@app.post("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(COOKIE)
    return r


_REG_PAGE = """<!doctype html><meta charset=utf-8>
<link rel=stylesheet href=/static/app.css><div class=wrap style='max-width:360px'>
<p class=brand style='font-size:32px'>Rada<b>r</b></p>{msg}
<form method=post action=/register>
  <input type=hidden name=invite value="{tok}">
  <div class=field><label>Choisis un identifiant</label><input name=username autofocus autocapitalize=off required></div>
  <div class=field><label>Mot de passe (6+ caractères)</label><input type=password name=pw required minlength=6></div>
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
    tok = accounts.create_invite(store.current_uid())
    url = str(request.base_url).rstrip("/") + "/register?invite=" + tok
    return HTMLResponse(
        f"<p class='small'>Lien d'invitation (à usage unique) :</p>"
        f"<input readonly onclick='this.select()' value='{html.escape(url)}' style='width:100%'>")


# --------------------------------------------------------------------- helpers
def render(request, tpl, **ctx):
    ctx.setdefault("has_token", bool(store.load_config().get("token")))
    ctx.setdefault("me", (accounts.get(store.current_uid()) or {}).get("username"))
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
def _last_import(job):
    s = jobs.status(job)
    fa = s.get("finished_at") if s else None
    return fa.replace("T", " ")[:16] if fa else None


@app.get("/patte", response_class=HTMLResponse)
def patte_page(request: Request, saved: int = 0):
    c = Ctx()
    pl_urls = [u for u in (c.cfg.get("youtube_playlists") or "").splitlines() if u.strip()]
    sp_urls = [u for u in (c.cfg.get("spotify_playlists") or "").splitlines() if u.strip()]
    last = {j: _last_import(j) for j in
           ("fetch_collection", "ingest_youtube", "ingest_spotify", "ingest_bandcamp", "ingest_djsets")}
    return render(request, "pages/patte.html", active="patte", cfg=c.cfg, sc=c.scoring,
                  cats=c.cfg.get("taste_categories", {}), coll=c.collection,
                  pl_urls=pl_urls, pl_meta=load(_pu().youtube_meta, {}),
                  sp_urls=sp_urls, sp_meta=load(_pu().spotify_meta, {}),
                  src=c.corpus_by_source(), st=c.stats(), saved=saved, last=last)


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
    if p.get("base_metric"):
        lbl = {"aff": "affinité", "reco": "reco", "owned": "collection"}.get(p["base_metric"], p["base_metric"])
        bits.append(f"mes labels ({lbl}"
                    + (f"≥{p['base_min']}" if p.get("base_min") else "") + ")")
    return " · ".join(bits) or "tous filtres vides"


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, sid: str = ""):
    c = Ctx()
    styles_mine, styles_more = _search_styles(c)
    hist = load(_pu().search_hist, [])
    entry = next((e for e in hist if e.get("id") == sid), hist[0] if hist else None)
    return render(request, "pages/search.html", active="search",
                  q=(entry or {}).get("params", {}), last_id=(entry or {}).get("id", ""),
                  history=[{"id": e["id"], "ts": e.get("ts", ""), "n": e.get("n", 0),
                            "summary": _hist_summary(e.get("params", {}))} for e in hist],
                  genres=vocab.GENRES, styles_mine=styles_mine, styles_more=styles_more,
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
                searched=entry.get("searched", []), voted=_voted_map())


def _base_labels_ranked(c):
    """Labels de la base -> nom canonique + affinité. Tri : affinité de style
    décroissante, départagée par le score de reco (collection + corpus + artistes),
    puis alpha. Dédoublonné par nom canonique."""
    ridx = c.reco_index
    lc = c.collection.get("label_counts", {})
    lids = c.collection.get("label_ids", {})
    rows, seen = [], set()
    for name in c.cfg.get("labels", []):
        key = store.normalize_label(name)
        res = c.resolved.get(key) or {}
        disp = res.get("discogs_name") or res.get("original") or name
        aff = c.affinity_score(c.profile.get(key)) if c.profile.get(key) else None
        did = res.get("discogs_id") or lids.get(key, {}).get("id")
        rows.append({"disp": disp, "norm": store.normalize_label(disp), "key": key,
                     "aff": aff, "_reco": ridx.get(key, 0), "owned": lc.get(key, 0), "id": did})
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


@app.get("/suggest/base-artists", response_class=HTMLResponse)
def suggest_base_artists(request: Request, q: str = ""):
    c = Ctx()
    disp, tiers = c.artist_disp(), c.artist_tier_map()
    names = sorted({v for k, v in disp.items() if not str(v).startswith("id:")},
                   key=str.lower)
    term = (q or "").strip().lower()
    hits = [n for n in names if term in n.lower()] if term else names
    tset = {disp.get(k) for k in tiers}
    rows = [{"v": n, "meta": ("classé" if n in tset else None), "dim": True} for n in hits[:60]]
    header = (f"{len(hits)} artiste" + ("s" if len(hits) != 1 else "")
              if term else "Tes artistes connus")
    return frag(request, "partials/suggest.html", rows=rows, header=header,
                empty="Aucun artiste connu ne correspond.")


_DISCOGS_SUGGEST_CACHE = {}  # (type, term) -> (ts, rows)


@app.get("/suggest/discogs", response_class=HTMLResponse)
def suggest_discogs(request: Request, q: str = "", type: str = "label"):
    dtype = "artist" if type == "artist" else "label"
    term = (q or "").strip()
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


@app.post("/search", response_class=HTMLResponse)
def search_run(request: Request, label: str = Form(""),
               genre: List[str] = Form(default=[]), style: List[str] = Form(default=[]),
               year_from: str = Form(""), year_to: str = Form(""),
               vinyl: str = Form(""), pages: str = Form("2"),
               base_metric: str = Form(""), base_min: str = Form("")):
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

    # mode « chercher dans mes labels » : prend le pas sur le label unique
    base_metric = (base_metric or "").strip()
    try:
        b_min = float(base_min) if str(base_min).strip() else None
    except ValueError:
        b_min = None
    base_labels = _pick_base_labels(c, base_metric, b_min, 12) if base_metric else []

    gs = [(g, s) for g in (genres or [""]) for s in (styles or [""])]
    if base_labels:
        npages = min(npages, 1)                       # N labels -> 1 page chacun
        combos = [(lab, g, s) for lab in base_labels for (g, s) in gs][:12]
    else:
        combos = [(label.strip() or None, g, s) for (g, s) in gs][:8]

    raw, seen_ids = [], set()
    try:
        for i, (lab, g, s) in enumerate(combos):
            if i:
                time.sleep(1.0)
            if lab:
                part = discogs.search_label_releases(token, lab, genre=g,
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
              "vinyl": bool(vinyl), "pages": npages,
              "base_metric": base_metric, "base_min": str(base_min or "").strip()}
    hist = [e for e in load(_pu().search_hist, []) if e.get("params") != params]
    hist.insert(0, {"id": hashlib.md5(f"{time.time()}{params}".encode()).hexdigest()[:10],
                    "ts": time.strftime("%Y-%m-%d %H:%M"), "params": params,
                    "n": len(scored), "results": scored, "searched": base_labels})
    save(_pu().search_hist, hist[:SEARCH_HIST_MAX])
    return frag(request, "partials/results.html", results=scored,
                searched=base_labels, voted=_voted_map())


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
    try:
        vid = ytcache.search_video(q, ytcache.youtube_keys(_cfg()))
        if vid:
            return RedirectResponse(f"https://www.youtube.com/watch?v={vid}", status_code=302)
    except (ytcache.QuotaExhausted, RuntimeError, requests.RequestException):
        pass
    return RedirectResponse(yt_search_url(q), status_code=302)


RELEASE_META_TTL = 86400


@app.get("/release/{rid}/meta", response_class=HTMLResponse)
def release_meta(request: Request, rid: int):
    cache = load(_pu().release_meta, {})
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
        save(_pu().release_meta, cache)
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
def veille_page(request: Request):
    c = _cfg()
    return render(request, "pages/veille.html", active="veille",
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


# --------------------------------------------------------------------- recos (dans Mon univers)
def _label_discogs_url(c, key, name):
    did = ((c.resolved.get(key) or {}).get("discogs_id")
           or (c.collection.get("label_ids", {}).get(key) or {}).get("id"))
    if did:
        return f"https://www.discogs.com/label/{did}"
    return f"https://www.discogs.com/search/?q={quote_plus(name)}&type=label"


@app.get("/univers/reco/labels", response_class=HTMLResponse)
def reco_labels_frag(request: Request):
    c = Ctx()
    base = {normalize_label(x) for x in c.cfg.get("labels", [])}
    rows = []
    for r in c.reco_rows():
        if r["key"] not in base:
            r["url"] = _label_discogs_url(c, r["key"], r["name"])
            rows.append(r)
    # candidats issus du graphe (labels où tes artistes ont sorti, absents de la base)
    graph_rows = []
    for lk, v in c.graph_rescore()["labels"].items():
        if lk in base:
            continue
        graph_rows.append({"name": v["name"], "score": round(v["score"]), "aff": None,
                           "owned": 0, "want": 0, "corpus": 0, "artists": v["n_seeds"],
                           "url": _label_discogs_url(c, lk, v["name"]),
                           "seeds": ", ".join(v["seeds"])})
    return frag(request, "partials/reco_labels.html", reco=rows[:30],
                graph_reco=graph_rows[:30], n_reco=len(rows), n_graph=len(graph_rows))


def _artist_discogs_url(name, artist_id=None):
    if artist_id:
        return f"https://www.discogs.com/artist/{artist_id}"
    return f"https://www.discogs.com/search/?q={quote_plus(name)}&type=artist"


@app.get("/univers/reco/artists", response_class=HTMLResponse)
def reco_artists_frag(request: Request):
    c = Ctx()
    g = c.graph_rescore()["artists"]
    rows = [{"name": v["name"], "prox": round(v["score"]), "note": c.ascore.get(k, 0),
             "why": ", ".join(v["why"]), "url": _artist_discogs_url(v["name"], v.get("id"))}
            for k, v in list(g.items())[:40] if not str(v["name"]).startswith("id:")]
    return frag(request, "partials/reco_artists.html", reco=rows, n_reco=len(g))


# ============================================================ 🌐 Mon univers
@app.get("/univers", response_class=HTMLResponse)
def univers_page(request: Request, tab: str = "labels"):
    c = Ctx()
    ac = c.cfg.get("artist_categories", {})
    label_graphs = load(_pu().label_graphs, [])
    artist_graphs = load(_pu().artist_graphs, [])
    disp, tiers = c.artist_disp(), c.artist_tier_map()
    classified_artists = sorted(
        ({"key": ck, "name": disp.get(ck, ck), "tier": t} for ck, t in tiers.items()
         if not str(disp.get(ck, ck)).startswith("id:")),
        key=lambda r: r["name"].lower())
    artist_names = sorted({v for v in disp.values() if not str(v).startswith("id:")},
                          key=str.lower)
    label_names = sorted({r["disp"] for r in _base_labels_ranked(c)}, key=str.lower)
    return render(request, "pages/univers.html", active="univers", tab=tab, cfg=c.cfg,
                  n_labels=len(c.cfg.get("labels", [])), n_profiled=len(c.profile),
                  n_artists=sum(len(v) for v in ac.values()),
                  n_sets=len([r for r in c.corpus if r.get("source") == "djset"]),
                  label_graphs=label_graphs,
                  artist_graphs=artist_graphs,
                  classified_artists=classified_artists,
                  artist_names=artist_names, label_names=label_names,
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


def _label_graph_render(request, entry):
    pos = labelgraph.layout(entry["nodes"])
    return frag(request, "partials/label_graph_svg.html", graph=entry, pos=pos)


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
        q = load(_pu().pending_enrich, {})
        q.setdefault("artists", []).append(name)
        save(_pu().pending_enrich, q)
        jobs.launch("enrich")
    store.save_config(c)
    return HTMLResponse("<span class='small ok'>✓</span>")


def _artist_graph_render(request, entry):
    pos = labelgraph.layout(entry["nodes"])
    return frag(request, "partials/label_graph_svg.html", graph=entry, pos=pos)


@app.post("/univers/artists/graph/build", response_class=HTMLResponse)
async def univers_artist_graph_build(request: Request):
    f = await request.form()
    seed_keys = f.getlist("seeds")
    if not seed_keys:
        return HTMLResponse("<p class='notice warn small'>Choisis au moins un artiste de départ.</p>")
    c = Ctx()
    disp = c.artist_disp()
    seed_items = [(k, disp.get(k, k)) for k in seed_keys]
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
              "ingest_djsets", "resolve_artists", "canonicalize", "enrich"}
JOB_PARAMS = {"ingest_youtube": {"deep": True}, "ingest_spotify": {"deep": True},
              "ingest_bandcamp": {"deep": True}}


@app.post("/jobs/{name}/launch", response_class=HTMLResponse)
def job_launch(name: str):
    if name == "ingest_djsets":
        srcs = [s.strip() for s in (_cfg().get("djset_sources") or "").splitlines() if s.strip()]
        if not srcs:
            return HTMLResponse("<div id='job-ingest_djsets' class='notice warn small'>"
                                "Aucune source DJ — renseigne-les dans « Mieux connaître ton univers → DJ sets ».</div>")
        d = os.path.join(store.paths.JOBS_DIR, store.current_uid())
        os.makedirs(d, exist_ok=True)
        save(os.path.join(d, "djsets.input.json"),
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
    run, err, queued = s.get("running"), s.get("error"), s.get("queued")
    msg = html.escape(str(s.get("message") or ""))
    if err:
        inner = f"<span class='notice warn small'>{html.escape(str(err))}</span>"
    elif queued:
        inner = f"<span class='small muted'>🕓 {msg}</span>"
    elif run:
        inner = (f"<span class='small muted'>⏳ {msg} · {done}/{s.get('total', 0)}</span>"
                 f"<div class='progress'><i style='width:{pct}%'></i></div>")
    else:
        inner = f"<span class='small muted'>✓ {msg or 'terminé'}</span>"
    poll = (f"hx-get='/jobs/{name}/status' hx-trigger='every 2s' hx-swap='outerHTML'"
            if (run or queued) else "")
    return HTMLResponse(f"<div id='job-{name}' {poll}>{inner}</div>")


# ============================================================ 🎛️ Réglages
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0):
    c = Ctx()
    g = c.graph or {}
    ac = c.cfg.get("artist_categories", {})
    return render(request, "pages/settings.html", active="settings", cfg=c.cfg,
                  sc=c.scoring, saved=saved,
                  graph_meta={"built_at": g.get("built_at", ""), "mode": g.get("mode", ""),
                              "n_seeds": len(g.get("seeds", {})), "n_edges": len(g.get("edges", {}))},
                  coeur="\n".join(ac.get("1", [])),
                  coeur_aimes="\n".join(ac.get("1", []) + ac.get("2", [])))


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
