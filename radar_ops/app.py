"""Tableau de bord de maintenance et de diagnostic de Radar.

Service séparé (`radar-ops`), même image et même volume que l'appli principale,
mais processus distinct : un outil de diagnostic doit rester debout quand
l'application qu'il observe ne l'est plus.

LECTURE SEULE sur `/data` : aucune route ne lance, n'arrête ni ne modifie un
job, une base ou un fichier (cf. la docstring du paquet pour les deux écritures
d'infrastructure héritées des modules partagés).
"""
import asyncio
import json
import os
import time

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from radar_web.radar import accounts, codeversion, opslog, paths, scorestore, store, websession
from radar_web.radar import scoring as sc

from . import delegation, freshness, inventory, probe
from .sampler import Sampler

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Radar — diagnostic")
tpl = Jinja2Templates(directory=os.path.join(HERE, "templates"))

SAMPLE_EVERY = 2.0          # cadence d'échantillonnage du débit des jobs
STREAM_EVERY = 2.0          # cadence d'envoi aux pages ouvertes
sampler = Sampler()

accounts.bootstrap()


# ---------------------------------------------------------------- échantillonnage
async def _sample_loop():
    """Alimente le calcul de débit en continu, indépendamment des pages ouvertes.

    Sans cette boucle de fond, le débit ne commencerait à se construire qu'à
    l'ouverture d'une page et afficherait « — » pendant les premières
    secondes — précisément quand on regarde."""
    while True:
        try:
            statuses = probe.read_statuses(paths.JOBS_DIR)
            sampler.observe(statuses)
            sampler.forget_idle({(s["uid"], s["name"]) for s in statuses})
        except Exception:
            pass                  # une sonde qui meurt emporterait tout le suivi
        await asyncio.sleep(SAMPLE_EVERY)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_sample_loop())


# ------------------------------------------------------------------------- auth
@app.middleware("http")
async def _guard(request: Request, call_next):
    p = request.url.path
    if p in ("/login", "/health"):
        return await call_next(request)
    if websession.bad_origin(request):
        return HTMLResponse("Origine invalide.", status_code=403)
    uid = websession.req_uid(request)
    # Le diagnostic est réservé au propriétaire : il expose le contenu des
    # données de TOUS les utilisateurs, ce qu'aucun compte invité ne doit voir.
    if uid != paths.DEFAULT_UID:
        return RedirectResponse("/login", status_code=303)
    resp = await call_next(request)
    if not websession.dev_mode() and p != "/logout":
        websession.set_session(resp, uid, request)
    return resp


@app.get("/health")
def health():
    return {"ok": True, "sha": codeversion.short()}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, err: str = ""):
    return tpl.TemplateResponse(request, "login.html", {"err": err})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    uid = accounts.verify(username, password)
    if not uid or uid != paths.DEFAULT_UID:
        return RedirectResponse("/login?err=1", status_code=303)
    r = RedirectResponse("/", status_code=303)
    websession.set_session(r, uid, request)
    return r


@app.post("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(websession.COOKIE)
    return r


# ------------------------------------------------------------------------- jobs
def _jobs_payload():
    """État courant des jobs, enrichi du débit et du temps restant estimés.

    Le débit ne vient pas du fichier de statut — il n'y figure pas : il est
    dérivé par `Sampler` de plusieurs lectures successives du même compteur."""
    snap = probe.snapshot(paths.JOBS_DIR)
    for s in snap["statuses"]:
        uid, name, done, total = s["uid"], s["name"], s["done"], s["total"]
        rate = sampler.rate(uid, name)
        s["rate_per_s"] = round(rate, 3) if rate is not None else None
        s["rate_per_min"] = round(rate * 60, 1) if rate is not None else None
        s["eta_s"] = sampler.eta_s(uid, name, done, total)
    snap["statuses"].sort(key=lambda s: (not s.get("running"), s["uid"], s["name"]))
    return snap


@app.get("/", response_class=HTMLResponse)
def page_jobs(request: Request):
    return tpl.TemplateResponse(request, "jobs.html", {
        "page": "jobs", "data": _jobs_payload(),
        "sha": codeversion.short()})


@app.get("/ops/stream/jobs")
async def stream_jobs(request: Request):
    """Flux Server-Sent Events : le navigateur garde la connexion ouverte et la
    page se met à jour sans rechargement ni sondage HTTP répété."""
    async def gen():
        while True:
            if await request.is_disconnected():
                return
            yield "data: " + json.dumps(_jobs_payload(), default=str) + "\n\n"
            await asyncio.sleep(STREAM_EVERY)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------------ bases
@app.get("/bases", response_class=HTMLResponse)
def page_bases(request: Request):
    return tpl.TemplateResponse(request, "bases.html", {
        "page": "bases", "inv": inventory.summary(paths.DATA),
        "root": paths.DATA, "sha": codeversion.short()})


# ---------------------------------------------------------------------- scoring
def _scoring_rows():
    """Fraîcheur des scores précalculés, utilisateur par utilisateur."""
    rows = []
    for uid in paths.all_uids():
        try:
            cfg = store.load_config(uid)
            current_fp = sc.derived_fingerprint(uid, cfg)
            detail = sc.derived_key_detail(uid, cfg)
            res = freshness.compare(scorestore.db_path(uid), current_fp, detail)
        except Exception as e:
            res = {"state": "erreur", "reason": str(e), "changes": [],
                   "stored_fp": None, "current_fp": None, "computed_at": None,
                   "code_sha": None, "rows": None}
        res["uid"] = uid
        rows.append(res)
    return rows


@app.get("/scoring", response_class=HTMLResponse)
def page_scoring(request: Request):
    return tpl.TemplateResponse(request, "scoring.html", {
        "page": "scoring", "rows": _scoring_rows(),
        "order": sc.topological_order(), "sha": codeversion.short()})


# ------------------------------------------------------------------ délégation
_PERIODES_AUTORISEES = {"today", "yesterday", "7d", "30d", "all", "custom"}
_GRANULARITES_AUTORISEES = {"hour", "day"}
# Blocs exportables : la clé de l'URL (`table`) doit rester stable, elle sert
# de nom de fichier téléchargé côté navigateur.
_BLOCS_EXPORTABLES = {"totals", "by_mode", "by_model", "top_models", "top_modes",
                       "buckets", "sessions", "quota_du_jour", "part_gratuite",
                       "cooldowns", "occasions_manquees", "delegation",
                       "small_prompts", "errors", "per_day", "model_colors",
                       "period", "all"}


def _to_float(value):
    """Chaîne de query -> float, ou None si vide ou illisible."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _delegation_snapshot(period, from_ts, to_ts, granularity):
    """Snapshot bornée aux valeurs de l'API — un unique point d'entrée pour la
    page et pour les exports, sinon les deux se mettraient à diverger."""
    if period not in _PERIODES_AUTORISEES:
        period = "7d"
    if granularity is not None and granularity not in _GRANULARITES_AUTORISEES:
        granularity = None
    return delegation.snapshot_windowed(
        paths.DATA,
        preset=period,
        from_ts=_to_float(from_ts),
        to_ts=_to_float(to_ts),
        granularity=granularity,
    )


@app.get("/delegation", response_class=HTMLResponse)
def page_delegation(request: Request,
                    period: str = "7d",
                    from_ts: str | None = None,
                    to_ts: str | None = None,
                    granularity: str | None = None):
    d = _delegation_snapshot(period, from_ts, to_ts, granularity)
    return tpl.TemplateResponse(request, "delegation.html", {
        "page": "delegation", "d": d, "sha": codeversion.short()})


@app.get("/delegation/export/{table}.json")
def export_delegation(table: str,
                      period: str = "7d",
                      from_ts: str | None = None,
                      to_ts: str | None = None,
                      granularity: str | None = None):
    """Télécharge le contenu d'un bloc, ou tout, aux mêmes filtres que la page.

    Un bloc inconnu renvoie 404 plutôt qu'un fichier vide : un JSON de zéro
    clé, ouvert plus tard, laisserait croire à un bug de collecte."""
    if table not in _BLOCS_EXPORTABLES:
        return JSONResponse({"error": f"bloc inconnu: {table}"}, status_code=404)
    d = _delegation_snapshot(period, from_ts, to_ts, granularity)
    payload = d if table == "all" else {table: d.get(table), "period": d.get("period")}
    filename = f"delegation-{table}-{period}.json"
    return JSONResponse(payload, headers={
        "Content-Disposition": f'attachment; filename="{filename}"'})


# --------------------------------------------------------------------- actions
@app.get("/actions", response_class=HTMLResponse)
def page_actions(request: Request):
    return tpl.TemplateResponse(request, "actions.html", {
        "page": "actions", "events": opslog.tail(opslog.LOG_PATH, limit=100),
        "sha": codeversion.short()})


@app.get("/ops/stream/actions")
async def stream_actions(request: Request):
    """Reprend à la FIN du journal : une page qui s'ouvre montre ce qui arrive,
    pas l'historique complet, déjà rendu côté serveur."""
    try:
        offset = os.path.getsize(opslog.LOG_PATH)
    except OSError:
        offset = 0

    async def gen():
        nonlocal offset
        while True:
            if await request.is_disconnected():
                return
            events, offset = opslog.stream_new(opslog.LOG_PATH, offset)
            for ev in events:
                yield "data: " + json.dumps(ev, default=str) + "\n\n"
            await asyncio.sleep(1.0)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
