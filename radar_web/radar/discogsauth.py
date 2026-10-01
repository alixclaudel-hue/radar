"""Connexion OAuth 1.0a au compte Discogs d'un utilisateur de Radar.

Les secrets sont conservés par utilisateur dans des fichiers 0600. Le secret
du jeton temporaire reste distinct jusqu'à la validation du callback. Les fonctions
make_state et check_state permettent à la couche web de lier une autorisation au
seul utilisateur qui l'a initiée.
"""
import os
import json
import time
import hmac
import hashlib
import base64
import urllib.parse
import secrets
import tempfile
from typing import Optional, Dict, Any

import requests

from . import paths, websession

# Configuration OAuth Discogs
REQUEST_TOKEN_ENDPOINT = "https://api.discogs.com/oauth/request_token"
ACCESS_TOKEN_ENDPOINT = "https://api.discogs.com/oauth/access_token"
IDENTITY_ENDPOINT = "https://api.discogs.com/oauth/identity"
AUTHORIZE_ENDPOINT = "https://www.discogs.com/oauth/authorize"

TOKEN_FILENAME = "discogs_oauth.json"
PENDING_FILENAME = "discogs_oauth_pending.json"

# Durée de validité du state (nonce) en secondes
STATE_TTL = 600
# Timeout réseau par défaut (secondes)
REQUEST_TIMEOUT = 15


class DiscogsError(Exception):
    """Erreur liée à l'API Discogs ou au stockage OAuth."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _get_client_config() -> tuple[str, str, str]:
    """Lit la configuration OAuth depuis les variables d'environnement."""
    consumer_key = os.getenv("RADAR_DISCOGS_CONSUMER_KEY", "")
    consumer_secret = os.getenv("RADAR_DISCOGS_CONSUMER_SECRET", "")
    callback = os.getenv("RADAR_DISCOGS_REDIRECT_URI", "")
    if not callback:
        domain = os.getenv("RADAR_DOMAIN", "")
        if domain:
            callback = f"https://{domain}/oauth/discogs/callback"
    return consumer_key, consumer_secret, callback


def _is_https_url(value: str) -> bool:
    """Vérifie qu'une URI de redirection est absolue, HTTPS et exploitable."""
    if not value:
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
        return (
            parsed.scheme.lower() == "https"
            and bool(parsed.netloc)
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except (UnicodeError, ValueError):
        return False


def _require_client_config() -> tuple[str, str, str]:
    """Retourne une configuration complète sans exposer les secrets."""
    consumer_key, consumer_secret, callback = _get_client_config()
    if not consumer_key or not consumer_secret or not _is_https_url(callback):
        raise DiscogsError("Connexion Discogs non configurée")
    return consumer_key, consumer_secret, callback


def _token_path(uid: str) -> str:
    """Chemin du fichier de jetons d'un utilisateur."""
    return os.path.join(paths.user_dir(uid), TOKEN_FILENAME)


def _pending_path(uid: str) -> str:
    """Chemin du fichier contenant la demande OAuth en attente."""
    return os.path.join(paths.user_dir(uid), PENDING_FILENAME)


def _write_private_json(path: str, data: Dict[str, Any]) -> None:
    """Écrit un JSON atomiquement avec des droits 0600 garantis."""
    fd: Optional[int] = None
    tmp_path = ""
    try:
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=directory,
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
        )
        # 0600 est appliqué avant toute écriture afin qu'aucun autre compte système
        # ne puisse lire le secret pendant la construction du fichier.
        os.fchmod(fd, 0o600)
        file_object = os.fdopen(fd, "w", encoding="utf-8")
        fd = None
        with file_object as file:
            json.dump(data, file, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise DiscogsError("Échec du stockage OAuth Discogs") from None


def _read_private_json(path: str) -> Optional[Dict[str, Any]]:
    """Lit un JSON privé et retourne None s'il est absent ou invalide."""
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_tokens(uid: str) -> Optional[Dict[str, Any]]:
    """Lit les jetons définitifs s'ils sont tous présents."""
    data = _read_private_json(_token_path(uid))
    if data is None:
        return None
    required = ("access_token", "access_secret", "username")
    if any(
        not isinstance(data.get(key), str) or not data[key]
        for key in required
    ):
        return None
    return data


def _read_pending(uid: str) -> Optional[Dict[str, Any]]:
    """Lit la demande OAuth en attente si elle est complète."""
    data = _read_private_json(_pending_path(uid))
    if data is None:
        return None
    required = ("oauth_token", "oauth_token_secret")
    if any(
        not isinstance(data.get(key), str) or not data[key]
        for key in required
    ):
        return None
    return data


def _delete_private_file(path: str) -> None:
    """Supprime un fichier privé sans faire échouer le flux appelant."""
    try:
        os.remove(path)
    except OSError:
        pass


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def configured() -> bool:
    """Vérifie si la configuration OAuth Discogs est complète."""
    consumer_key, consumer_secret, callback = _get_client_config()
    return bool(
        consumer_key
        and consumer_secret
        and _is_https_url(callback)
    )


def redirect_uri() -> str:
    """Retourne l'URI de redirection configurée."""
    _, _, callback = _get_client_config()
    return callback


def make_state(uid: str) -> str:
    """Génère un state CSRF signé et lié à l'uid."""
    nonce = secrets.token_bytes(16)
    exp = int(time.time()) + STATE_TTL
    nonce_b64 = _b64url_encode(nonce)
    # L'uid est inclus dans la charge signée afin qu'un state ne puisse pas être
    # rejoué pour le compte d'un autre utilisateur.
    payload = f"{uid}.{nonce_b64}.{exp}"
    signature = hmac.new(
        websession.SESSION_SECRET,
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return f"{payload}.{_b64url_encode(signature)}"


def check_state(state: str, uid: str) -> bool:
    """Vérifie la signature, l'expiration et l'uid sans lever d'exception."""
    try:
        parts = state.split(".")
        if len(parts) != 4:
            return False
        state_uid, nonce_b64, exp_text, signature_b64 = parts
        if state_uid != uid:
            return False
        exp = int(exp_text)
        if exp < time.time():
            return False
        payload = f"{state_uid}.{nonce_b64}.{exp_text}"
        expected_signature = hmac.new(
            websession.SESSION_SECRET,
            payload.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        expected_b64 = _b64url_encode(expected_signature)
        return hmac.compare_digest(signature_b64, expected_b64)
    except Exception:
        return False


def _oauth_escape(value: str) -> str:
    """Encode une valeur ou une composante PLAINTEXT selon RFC 3986."""
    try:
        return urllib.parse.quote(value, safe="")
    except (TypeError, UnicodeError):
        raise DiscogsError("Valeur OAuth Discogs invalide") from None


def _plaintext_signature(consumer_secret: str, token_secret: str) -> str:
    return f"{_oauth_escape(consumer_secret)}&{_oauth_escape(token_secret)}"


def _build_oauth_header(parameters: Dict[str, str]) -> str:
    """Sérialise les paramètres OAuth en percent-encoding."""
    encoded = ", ".join(
        f'{name}="{_oauth_escape(value)}"'
        for name, value in parameters.items()
    )
    return f"OAuth {encoded}"


def _oauth_nonce_and_timestamp() -> tuple[str, str]:
    return secrets.token_urlsafe(24), str(int(time.time()))


def _http(method: str, url: str, **kwargs: Any) -> requests.Response:
    """Effectue un appel réseau en convertissant les pannes en erreur générique."""
    # Une redirection pourrait exposer un en-tête OAuth à un endpoint non HTTPS.
    kwargs.setdefault("allow_redirects", False)
    try:
        return requests.request(
            method,
            url,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
    except requests.RequestException:
        pass
    raise DiscogsError(
        "Discogs injoignable, réessaie dans un instant"
    ) from None


def _parse_oauth_form(response: requests.Response) -> Dict[str, str]:
    """Analyse une réponse form Encoded sans inclure son corps dans l'erreur."""
    try:
        pairs = urllib.parse.parse_qsl(
            response.text,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=16,
        )
        values: Dict[str, str] = {}
        for key, value in pairs:
            if key in values:
                raise ValueError
            values[key] = value
        return values
    except Exception:
        raise DiscogsError("Réponse OAuth Discogs inattendue") from None


def _required_oauth_value(values: Dict[str, str], name: str) -> str:
    value = values.get(name)
    if not value:
        raise DiscogsError("Réponse OAuth Discogs inattendue")
    return value


def start(uid: str) -> str:
    """Démarre OAuth 1.0a et retourne l'URL d'autorisation Discogs."""
    consumer_key, consumer_secret, callback = _require_client_config()
    nonce, timestamp = _oauth_nonce_and_timestamp()
    signature = _plaintext_signature(consumer_secret, "")
    header = _build_oauth_header(
        {
            "oauth_consumer_key": consumer_key,
            "oauth_nonce": nonce,
            "oauth_signature_method": "PLAINTEXT",
            "oauth_timestamp": timestamp,
            "oauth_callback": callback,
            "oauth_signature": signature,
        }
    )
    response = _http(
        "POST",
        REQUEST_TOKEN_ENDPOINT,
        headers={"Authorization": header},
    )
    if response.status_code != 200:
        raise DiscogsError("Discogs a refusé la demande de jeton")

    values = _parse_oauth_form(response)
    oauth_token = _required_oauth_value(values, "oauth_token")
    oauth_token_secret = _required_oauth_value(
        values,
        "oauth_token_secret",
    )
    _write_private_json(
        _pending_path(uid),
        {
            "oauth_token": oauth_token,
            "oauth_token_secret": oauth_token_secret,
        },
    )
    query = urllib.parse.urlencode({"oauth_token": oauth_token})
    return f"{AUTHORIZE_ENDPOINT}?{query}"


def complete(
    uid: str,
    oauth_token: str,
    oauth_verifier: str,
) -> bool:
    """Termine OAuth et retourne False si le callback ne correspond pas au pending."""
    pending = _read_pending(uid)
    if pending is None:
        return False
    if (
        not isinstance(oauth_token, str)
        or not oauth_token
        or not isinstance(oauth_verifier, str)
        or not oauth_verifier
    ):
        return False

    try:
        token_matches = hmac.compare_digest(
            oauth_token.encode("utf-8"),
            pending["oauth_token"].encode("utf-8"),
        )
    except (UnicodeError, KeyError, AttributeError):
        return False
    if not token_matches:
        return False

    consumer_key, consumer_secret, _ = _require_client_config()
    request_secret = pending["oauth_token_secret"]
    nonce, timestamp = _oauth_nonce_and_timestamp()
    signature = _plaintext_signature(consumer_secret, request_secret)
    header = _build_oauth_header(
        {
            "oauth_consumer_key": consumer_key,
            "oauth_nonce": nonce,
            "oauth_signature_method": "PLAINTEXT",
            "oauth_timestamp": timestamp,
            "oauth_token": oauth_token,
            "oauth_verifier": oauth_verifier,
            "oauth_signature": signature,
        }
    )
    response = _http(
        "POST",
        ACCESS_TOKEN_ENDPOINT,
        headers={"Authorization": header},
    )
    if response.status_code != 200:
        raise DiscogsError("Discogs a refusé l'échange du jeton")

    values = _parse_oauth_form(response)
    access_token = _required_oauth_value(values, "oauth_token")
    access_secret = _required_oauth_value(values, "oauth_token_secret")

    identity_response = _http(
        "GET",
        IDENTITY_ENDPOINT,
        headers={
            "Authorization": authorization_header(
                (access_token, access_secret)
            )
        },
    )
    if identity_response.status_code != 200:
        raise DiscogsError("Impossible de récupérer l'identité Discogs")

    try:
        identity = identity_response.json()
    except Exception:
        raise DiscogsError("Réponse d'identité Discogs inattendue") from None
    if not isinstance(identity, dict):
        raise DiscogsError("Réponse d'identité Discogs inattendue")
    username = identity.get("username")
    if not isinstance(username, str) or not username:
        raise DiscogsError("Réponse d'identité Discogs inattendue")

    _write_private_json(
        _token_path(uid),
        {
            "access_token": access_token,
            "access_secret": access_secret,
            "username": username,
        },
    )
    _delete_private_file(_pending_path(uid))
    return True


def is_connected(uid: str) -> bool:
    """Vérifie si des jetons Discogs complets sont stockés localement."""
    return _read_tokens(uid) is not None


def credential(uid: str) -> Optional[str]:
    """Retourne le credential Discogs au format oauth:<token>:<secret>."""
    tokens = _read_tokens(uid)
    if tokens is None:
        return None
    return f"oauth:{tokens['access_token']}:{tokens['access_secret']}"


def disconnect(uid: str) -> None:
    """Supprime les jetons définitifs et la demande OAuth en attente."""
    _delete_private_file(_token_path(uid))
    _delete_private_file(_pending_path(uid))


def authorization_header(
    token_secret_pair: tuple[str, str],
) -> str:
    """Construit un en-tête OAuth PLAINTEXT pour un appel à l'API Discogs."""
    if not isinstance(token_secret_pair, (tuple, list)):
        raise DiscogsError("Jeton Discogs invalide")
    try:
        token, token_secret = token_secret_pair
    except (TypeError, ValueError):
        raise DiscogsError("Jeton Discogs invalide") from None
    if (
        not isinstance(token, str)
        or not token
        or not isinstance(token_secret, str)
        or not token_secret
    ):
        raise DiscogsError("Jeton Discogs invalide")

    consumer_key, consumer_secret, _ = _require_client_config()
    nonce, timestamp = _oauth_nonce_and_timestamp()
    return _build_oauth_header(
        {
            "oauth_consumer_key": consumer_key,
            "oauth_nonce": nonce,
            "oauth_signature_method": "PLAINTEXT",
            "oauth_timestamp": timestamp,
            "oauth_token": token,
            "oauth_signature": _plaintext_signature(
                consumer_secret,
                token_secret,
            ),
        }
    )
