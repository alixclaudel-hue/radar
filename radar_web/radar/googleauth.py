"""Connexion OAuth 2.0 au compte Google/YouTube d'un utilisateur de Radar.

Sert à une seule chose : ajouter une piste de Reco Radar à une playlist du compte
YouTube de l'utilisateur (l'API IFrame ne fait que lire). Les jetons sont stockés
PAR utilisateur (users/<uid>/google_oauth.json, droits 0600), jamais dans la
config JSON ni dans un prompt/journal. Le paramètre `state` est signé avec le
secret de session et lié à l'uid : sans lui, un tiers pourrait faire relier SON
compte Google à la session d'une victime (CSRF de connexion).
"""
import os
import json
import time
import hmac
import hashlib
import base64
import urllib.parse
import secrets
from typing import Optional, List, Dict, Any

import requests

from . import paths, websession

# Configuration OAuth Google
SCOPE = "https://www.googleapis.com/auth/youtube"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
PLAYLISTS_ENDPOINT = "https://www.googleapis.com/youtube/v3/playlists"
PLAYLIST_ITEMS_ENDPOINT = "https://www.googleapis.com/youtube/v3/playlistItems"

# Durée de validité du state (nonce) en secondes
STATE_TTL = 600
# Marge de sécurité pour rafraîchir le token avant expiration (secondes)
TOKEN_REFRESH_BUFFER = 60
# Plafond de playlists récupérées
MAX_PLAYLISTS = 200
# Timeout réseau par défaut (secondes)
REQUEST_TIMEOUT = 15


def _get_client_config() -> tuple[str, str, str]:
    """Lit la configuration OAuth depuis les variables d'environnement."""
    client_id = os.getenv("RADAR_GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("RADAR_GOOGLE_CLIENT_SECRET", "")
    redirect_uri = os.getenv("RADAR_GOOGLE_REDIRECT_URI", "")
    if not redirect_uri:
        domain = os.getenv("RADAR_DOMAIN", "")
        if domain:
            redirect_uri = f"https://{domain}/oauth/google/callback"
    return client_id, client_secret, redirect_uri


def _token_path(uid: str) -> str:
    """Chemin du fichier de jetons pour un utilisateur."""
    return os.path.join(paths.user_dir(uid), "google_oauth.json")


def _read_tokens(uid: str) -> Optional[Dict[str, Any]]:
    """Lit les jetons stockés. Retourne None si absent ou invalide."""
    path = _token_path(uid)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Validation basique
        if not all(k in data for k in ("access_token", "refresh_token", "expires_at")):
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _write_tokens(uid: str, data: Dict[str, Any]) -> None:
    """Écrit les jetons de façon atomique avec droits 0o600."""
    path = _token_path(uid)
    tmp_path = path + ".tmp"
    # 0o600 dès la création : un chmod APRÈS l'écriture laisserait le jeton lisible
    # par les autres comptes système pendant un instant.
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp_path, path)


def _http(method: str, url: str, **kw) -> requests.Response:
    """Appel réseau : toute panne (DNS, timeout, TLS) devient GoogleError, pour que
    les routes n'aient qu'un type d'exception à gérer (jamais de 500 brut)."""
    try:
        return requests.request(method, url, timeout=REQUEST_TIMEOUT, **kw)
    except requests.RequestException as e:
        raise GoogleError("Google injoignable, réessaie dans un instant") from e


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


class GoogleError(Exception):
    """Erreur liée à l'API Google / OAuth."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def configured() -> bool:
    """Vérifie si la configuration OAuth est complète."""
    client_id, client_secret, redirect_uri = _get_client_config()
    return bool(client_id and client_secret and redirect_uri)


def redirect_uri() -> str:
    """Retourne l'URI de redirection configurée."""
    _, _, redirect_uri = _get_client_config()
    return redirect_uri


def make_state(uid: str) -> str:
    """Génère un state CSRF signé (nonce.exp.sig) lié à l'UID."""
    nonce = secrets.token_bytes(16)
    exp = int(time.time()) + STATE_TTL
    # Payload signé : uid.nonce_b64.exp
    nonce_b64 = _b64url_encode(nonce)
    payload = f"{uid}.{nonce_b64}.{exp}"
    sig = hmac.new(websession.SESSION_SECRET, payload.encode(), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload}.{sig_b64}"


def check_state(state: str, uid: str) -> bool:
    """Vérifie la signature, l'expiration et l'UID du state. Ne lève jamais."""
    try:
        parts = state.split(".")
        if len(parts) != 4:
            return False
        state_uid, nonce_b64, exp_str, sig_b64 = parts
        if state_uid != uid:
            return False
        exp = int(exp_str)
        if exp < time.time():
            return False
        payload = f"{state_uid}.{nonce_b64}.{exp_str}"
        expected_sig = hmac.new(websession.SESSION_SECRET, payload.encode(), hashlib.sha256).digest()
        expected_sig_b64 = _b64url_encode(expected_sig)
        return hmac.compare_digest(sig_b64, expected_sig_b64)
    except Exception:
        return False


def auth_url(state: str) -> str:
    """Construit l'URL d'autorisation Google."""
    client_id, _, redirect_uri = _get_client_config()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def exchange_code(uid: str, code: str) -> None:
    """Échange le code d'autorisation contre des jetons et les stocke."""
    client_id, client_secret, redirect_uri = _get_client_config()
    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    resp = _http("POST", TOKEN_ENDPOINT, data=data)
    if resp.status_code != 200:
        _raise_google_error(resp)
    try:
        token_data = resp.json()
        access = token_data["access_token"]
    except (ValueError, KeyError) as e:
        raise GoogleError("Réponse Google inattendue") from e

    existing = _read_tokens(uid)
    refresh_token = token_data.get("refresh_token")
    # Google n'envoie refresh_token que la première fois (prompt=consent force mais on garde l'ancien par sécurité)
    if not refresh_token and existing:
        refresh_token = existing.get("refresh_token")
    if not refresh_token:
        raise GoogleError("Google n'a pas fourni de refresh_token")
    
    expires_in = token_data.get("expires_in", 3600)
    store = {
        "access_token": access,
        "refresh_token": refresh_token,
        "expires_at": time.time() + expires_in,
    }
    _write_tokens(uid, store)


def is_connected(uid: str) -> bool:
    """Vérifie si l'utilisateur a des jetons valides stockés."""
    return _read_tokens(uid) is not None


def _refresh_access_token(uid: str, refresh_token: str) -> Dict[str, Any]:
    """Rafraîchit le access_token via le refresh_token. Lève GoogleError si échec."""
    client_id, client_secret, _ = _get_client_config()
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    resp = _http("POST", TOKEN_ENDPOINT, data=data)
    if resp.status_code != 200:
        # Si invalid_grant, le refresh token est révoqué/expiré -> déconnexion
        if resp.status_code == 400:
            try:
                err = resp.json()
                if err.get("error") == "invalid_grant":
                    _delete_tokens(uid)
            except Exception:
                pass
        _raise_google_error(resp)
    new_data = resp.json()
    expires_in = new_data.get("expires_in", 3600)
    return {
        "access_token": new_data["access_token"],
        "refresh_token": refresh_token,  # inchangé
        "expires_at": time.time() + expires_in,
    }


def _delete_tokens(uid: str) -> None:
    """Supprime le fichier de jetons s'il existe."""
    try:
        os.remove(_token_path(uid))
    except OSError:
        pass


def access_token(uid: str) -> str:
    """Retourne un access_token valide, le rafraîchissant si nécessaire."""
    tokens = _read_tokens(uid)
    if not tokens:
        raise GoogleError("Compte Google non connecté")
    
    if tokens["expires_at"] - TOKEN_REFRESH_BUFFER <= time.time():
        try:
            tokens = _refresh_access_token(uid, tokens["refresh_token"])
            _write_tokens(uid, tokens)
        except GoogleError:
            raise
        except Exception as e:
            raise GoogleError("Échec rafraîchissement token") from e
    return tokens["access_token"]


def search_public_playlist(uid: str, query: str) -> Optional[Dict[str, str]]:
    """Cherche une playlist YouTube PUBLIQUE par son nom exact (API search.list, type=playlist) avec le token OAuth de uid. Renvoie {'id':..., 'title':..., 'url': 'https://www.youtube.com/playlist?list='+id} pour le MEILLEUR résultat, ou None si aucun résultat. Lève GoogleError sur erreur HTTP (réutilise _raise_google_error)."""
    token = access_token(uid)
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "part": "snippet",
        "type": "playlist",
        "q": query,
        "maxResults": 1,
    }
    resp = _http("GET", "https://www.googleapis.com/youtube/v3/search", headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        _raise_google_error(resp)
    data = resp.json()
    items = data.get("items", [])
    if not items:
        return None
    item = items[0]
    playlist_id = item["id"]["playlistId"]
    title = item["snippet"]["title"]
    return {
        "id": playlist_id,
        "title": title,
        "url": f"https://www.youtube.com/playlist?list={playlist_id}",
    }


def list_playlists(uid: str) -> List[Dict[str, str]]:
    """Liste les playlists YouTube de l'utilisateur (max 200)."""
    token = access_token(uid)
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "part": "snippet",
        "mine": "true",
        "maxResults": 50,
    }
    playlists = []
    page_token = None
    while len(playlists) < MAX_PLAYLISTS:
        if page_token:
            params["pageToken"] = page_token
        resp = _http("GET", PLAYLISTS_ENDPOINT, headers=headers, params=params)
        if resp.status_code != 200:
            _raise_google_error(resp)
        data = resp.json()
        for item in data.get("items", []):
            playlists.append({"id": item["id"], "title": item["snippet"]["title"]})
            if len(playlists) >= MAX_PLAYLISTS:
                break
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return playlists


def add_to_playlist(uid: str, playlist_id: str, video_id: str) -> None:
    """Ajoute une vidéo à une playlist."""
    token = access_token(uid)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id,
            },
        }
    }
    resp = _http(
        "POST", f"{PLAYLIST_ITEMS_ENDPOINT}?part=snippet", headers=headers, json=body,
    )
    if resp.status_code not in (200, 201):
        _raise_google_error(resp)


def disconnect(uid: str) -> None:
    """Révoque le token (best-effort) et supprime le stockage local."""
    tokens = _read_tokens(uid)
    if tokens:
        try:
            # Révocation best-effort : on ignore les erreurs réseau/HTTP
            # le refresh_token, pas l'access_token : Google révoque alors les deux
            _http("POST", REVOKE_ENDPOINT, data={"token": tokens["refresh_token"]})
        except Exception:
            pass
    _delete_tokens(uid)


def _raise_google_error(resp: requests.Response) -> None:
    """Analyse la réponse d'erreur Google et lève GoogleError avec message propre."""
    msg = "Erreur API Google"
    try:
        data = resp.json()
        err = data.get("error", {})
        # Message lisible depuis error.message ou error.errors[0].reason
        if isinstance(err, dict):
            msg = err.get("message", msg)
            errors = err.get("errors")
            if isinstance(errors, list) and errors:
                reason = errors[0].get("reason")
                if reason == "quotaExceeded":
                    msg = "Quota YouTube du jour atteint"
                elif reason:
                    msg = f"Erreur YouTube: {reason}"
    except Exception:
        pass
    # On ne logue pas le corps de la réponse (peut contenir des tokens)
    raise GoogleError(msg)
