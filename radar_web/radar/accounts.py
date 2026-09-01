"""Comptes applicatifs (Option A, étape 2b).

`accounts.json` à la racine de <DATA> :
    { "<uid>": {"username", "pw", "created_at", "invited_by"} }

Mot de passe haché avec `hashlib.scrypt` (stdlib — pas de dépendance).
Le compte propriétaire est créé au 1er démarrage depuis `APP_PASSWORD` (bootstrap).
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import datetime, timezone

from . import paths, store

ACCOUNTS_PATH = os.path.join(paths.DATA, "accounts.json")
INVITES_PATH = os.path.join(paths.DATA, "invites.json")
_SCRYPT = dict(n=2 ** 14, r=8, p=1)          # ~50-100 ms
_DKLEN = 32
MIN_PW_LEN = 10
INVITE_TTL_S = 48 * 3600

# create()/consume_invite() font lire-modifier-écrire : sérialise-les pour ne pas
# perdre un compte ni consommer deux fois la même invitation.
_WRITE_LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_pw(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt((password or "").encode(), salt=salt, dklen=_DKLEN, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${dk.hex()}"


def verify_pw(password, stored):
    try:
        algo, n, r, p, salt_hex, dk_hex = (stored or "").split("$")
        assert algo == "scrypt"
        dk = hashlib.scrypt((password or "").encode(), salt=bytes.fromhex(salt_hex),
                            dklen=len(dk_hex) // 2, n=int(n), r=int(r), p=int(p))
    except Exception:
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


def load_accounts():
    return store.load(ACCOUNTS_PATH, {}) or {}


def save_accounts(d):
    store.save(ACCOUNTS_PATH, d)


def count():
    return len(load_accounts())


def get(uid):
    return load_accounts().get(uid)


def by_username(username):
    u = (username or "").strip().lower()
    for uid, a in load_accounts().items():
        if a.get("username", "").strip().lower() == u:
            return uid, a
    return None, None


# empreinte fixe pour égaliser le temps quand l'identifiant est inconnu
_DUMMY = hash_pw("x" * 12)


def verify(username, password):
    """Renvoie l'uid si (identifiant, mot de passe) valides, sinon None."""
    uid, a = by_username(username)
    if not a:
        verify_pw(password, _DUMMY)          # anti-timing
        return None
    return uid if verify_pw(password, a.get("pw", "")) else None


def _seed_user_dir(uid):
    """Crée users/<uid>/ avec une config par défaut."""
    paths.user_dir(uid)
    store.save_config(store.load_config(uid), uid)


def create(username, password, invited_by=None, is_owner=False, min_len=MIN_PW_LEN):
    with _WRITE_LOCK:
        return _create_locked(username, password, invited_by, is_owner, min_len)


def _create_locked(username, password, invited_by, is_owner, min_len):
    accts = load_accounts()
    username = (username or "").strip()
    if not username or len(password or "") < min_len:
        raise ValueError(f"identifiant vide ou mot de passe < {min_len} caractères")
    if by_username(username)[0]:
        raise ValueError("identifiant déjà pris")
    uid = paths.DEFAULT_UID if (is_owner and not accts) else secrets.token_hex(6)
    accts[uid] = {"username": username, "pw": hash_pw(password),
                  "created_at": _now(), "invited_by": invited_by}
    save_accounts(accts)
    _seed_user_dir(uid)
    return uid


def bootstrap():
    """1er démarrage : si aucun compte et APP_PASSWORD défini, crée le propriétaire.
    Ne doit jamais faire planter le démarrage."""
    if count():
        return
    pw = os.environ.get("APP_PASSWORD")
    if not pw:
        return
    try:
        create(os.environ.get("OWNER_USERNAME", "owner"), pw, is_owner=True, min_len=1)
    except Exception as e:                       # noqa: BLE001
        import sys
        print(f"[radar] bootstrap du compte owner impossible : {e}", file=sys.stderr)


# ---------------------------------------------------------------- invitations
def load_invites():
    return store.load(INVITES_PATH, {}) or {}


def create_invite(by_uid):
    with _WRITE_LOCK:
        inv = load_invites()
        token = secrets.token_urlsafe(16)
        inv[token] = {"created_by": by_uid, "created_at": _now(), "used_by": None,
                      "expires_at": time.time() + INVITE_TTL_S}
        store.save(INVITES_PATH, inv)
        return token


def _invite_live(e):
    if not e or e.get("used_by"):
        return False
    exp = e.get("expires_at")
    return not (exp and time.time() >= exp)


def invite_ok(token):
    return _invite_live(load_invites().get(token or ""))


def consume_invite(token, username, password):
    with _WRITE_LOCK:
        inv = load_invites()
        e = inv.get(token or "")
        if not _invite_live(e):
            raise ValueError("invitation invalide, expirée ou déjà utilisée")
        uid = _create_locked(username, password, e.get("created_by"), False, MIN_PW_LEN)
        e["used_by"] = uid
        store.save(INVITES_PATH, inv)
        return uid
