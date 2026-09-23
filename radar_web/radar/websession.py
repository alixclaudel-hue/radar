"""Session d'authentification partagée par les applications web du projet.

Extrait de `radar_web/app.py` quand une seconde application (`radar_ops`, le
tableau de bord de diagnostic) a eu besoin du même mécanisme : dupliquer une
signature de session, c'est se condamner à corriger deux fois le jour où l'une
des deux se révèle faible.

Le jeton est `uid.exp.signature`, signé en HMAC-SHA256 avec un secret commun.
La signature intègre `_pw_epoch(uid)` — un extrait du hash du mot de passe —
si bien qu'un changement de mot de passe invalide toutes les sessions en cours :
c'est la seule voie de révocation.
"""
import hashlib
import hmac
import os
import secrets
import time

from . import accounts, paths

AUTH_TTL = 5 * 3600
COOKIE = "radar_auth"


def _read_or_create_secret():
    """Secret de signature : variable d'environnement si elle existe, sinon un
    fichier de `DATA` créé une fois. Le fichier est partagé par les deux
    applications via le même volume, donc une session reste valide de l'une à
    l'autre sans configuration supplémentaire."""
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


SESSION_SECRET = _read_or_create_secret()


def dev_mode():
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


def sign(uid, exp):
    return hmac.new(SESSION_SECRET, f"{uid}|{exp}|{_pw_epoch(uid)}".encode(),
                    hashlib.sha256).hexdigest()[:32]


def make_token(uid, exp):
    exp = int(exp)
    return f"{uid}.{exp}.{sign(uid, exp)}"


def parse_token(tok):
    try:
        uid, exp, sig = (tok or "").split(".")
        exp = int(exp)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, sign(uid, exp)) or time.time() >= exp:
        return None
    return uid


def is_https(request):
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def set_session(resp, uid, request):
    secure = os.environ.get("RADAR_SECURE_COOKIE") == "1" or is_https(request)
    resp.set_cookie(COOKIE, make_token(uid, time.time() + AUTH_TTL), max_age=AUTH_TTL,
                    httponly=True, samesite="lax", secure=secure)


def req_uid(request):
    if dev_mode():
        return paths.DEFAULT_UID
    uid = parse_token(request.cookies.get(COOKIE, ""))
    return uid if uid and accounts.get(uid) else None


def bad_origin(request):
    """Défense en profondeur CSRF : le cookie est déjà SameSite=lax et toutes les
    mutations sont des POST, mais on refuse en plus une origine étrangère."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return False
    origin = request.headers.get("origin")
    return bool(origin) and origin.rstrip("/") != str(request.base_url).rstrip("/")
