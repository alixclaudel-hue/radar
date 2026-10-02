"""Connexion OAuth 2.0 au compte Spotify d'un utilisateur de Radar.

Sert à une seule chose : ajouter une piste de Reco Radar à une playlist du compte
Spotify de l'utilisateur. Les jetons sont stockés PAR utilisateur
(users/<uid>/spotify_oauth.json, droits 0600), jamais dans la config JSON ni dans un
prompt/journal. Le paramètre `state` protège contre le CSRF de connexion (un tiers
ne doit pas pouvoir relier SON compte Spotify à la session d'une victime) : il est
réutilisé tel quel depuis googleauth, car il ne dépend pas du fournisseur OAuth.
"""
import os
import json
import time
import base64
import unicodedata
import urllib.parse
from typing import Optional, List, Dict, Any

import requests

from . import paths
from .googleauth import make_state, check_state  # réutilisation directe (état CSRF)

# Configuration OAuth Spotify
SCOPES = (
    "playlist-read-private "
    "playlist-read-collaborative "
    "playlist-modify-private "
    "playlist-modify-public"
)
AUTH_ENDPOINT = "https://accounts.spotify.com/authorize"
TOKEN_ENDPOINT = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

# Marge de sécurité pour rafraîchir le token avant expiration (secondes)
TOKEN_REFRESH_BUFFER = 60
# Plafond de playlists récupérées
MAX_PLAYLISTS = 200
# Taille de page demandée à Spotify
PAGE_SIZE = 50
# Timeout réseau par défaut (secondes)
REQUEST_TIMEOUT = 15
# Mots-clés signalant une version non souhaitée (sauf si le titre les contient)
_UNWANTED = ("remix", "instrumental", "edit", "live", "dub")


def _get_client_config() -> tuple[str, str, str]:
    """Lit la configuration OAuth depuis les variables d'environnement."""
    client_id = os.getenv("RADAR_SPOTIFY_CLIENT_ID", "")
    client_secret = os.getenv("RADAR_SPOTIFY_CLIENT_SECRET", "")
    redirect_uri = os.getenv("RADAR_SPOTIFY_REDIRECT_URI", "")
    if not redirect_uri:
        domain = os.getenv("RADAR_DOMAIN", "")
        if domain:
            redirect_uri = f"https://{domain}/oauth/spotify/callback"
    return client_id, client_secret, redirect_uri


def _basic_auth_header() -> str:
    """En-tête HTTP Basic (base64 id:secret) exigé par le endpoint de jeton."""
    client_id, client_secret, _ = _get_client_config()
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _token_path(uid: str) -> str:
    """Chemin du fichier de jetons pour un utilisateur."""
    return os.path.join(paths.user_dir(uid), "spotify_oauth.json")


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


def _delete_tokens(uid: str) -> None:
    """Supprime le fichier de jetons s'il existe."""
    try:
        os.remove(_token_path(uid))
    except OSError:
        pass


def _http(method: str, url: str, **kw) -> requests.Response:
    """Appel réseau : toute panne (DNS, timeout, TLS) devient SpotifyError, pour que
    les routes n'aient qu'un type d'exception à gérer (jamais de 500 brut)."""
    try:
        return requests.request(method, url, timeout=REQUEST_TIMEOUT, **kw)
    except requests.RequestException as e:
        raise SpotifyError("Spotify injoignable, réessaie dans un instant") from e


class SpotifyError(Exception):
    """Erreur liée à l'API Spotify / OAuth."""
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


def auth_url(state: str) -> str:
    """Construit l'URL d'autorisation Spotify."""
    client_id, _, redirect_uri = _get_client_config()
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        # show_dialog force l'écran de consentement (utile pour changer de compte)
        "show_dialog": "true",
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def exchange_code(uid: str, code: str) -> None:
    """Échange le code d'autorisation contre des jetons et les stocke."""
    _, _, redirect_uri = _get_client_config()
    data = {
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    resp = _http("POST", TOKEN_ENDPOINT, data=data, headers=headers)
    if resp.status_code != 200:
        _raise_spotify_error(resp)
    try:
        token_data = resp.json()
        access = token_data["access_token"]
    except (ValueError, KeyError) as e:
        raise SpotifyError("Réponse Spotify inattendue") from e

    refresh_token = token_data.get("refresh_token")
    existing = _read_tokens(uid)
    # Spotify n'envoie pas toujours de refresh_token : on garde l'ancien plutôt que
    # de rendre le compte inutilisable au prochain rafraîchissement.
    if not refresh_token and existing:
        refresh_token = existing.get("refresh_token")
    if not refresh_token:
        raise SpotifyError("Spotify n'a pas fourni de refresh_token")

    expires_in = token_data.get("expires_in", 3600)
    _write_tokens(uid, {
        "access_token": access,
        "refresh_token": refresh_token,
        "expires_at": time.time() + expires_in,
    })


def is_connected(uid: str) -> bool:
    """Vérifie si l'utilisateur a des jetons valides stockés."""
    return _read_tokens(uid) is not None


def _refresh_access_token(uid: str, refresh_token: str) -> Dict[str, Any]:
    """Rafraîchit le access_token via le refresh_token. Lève SpotifyError si échec."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    resp = _http("POST", TOKEN_ENDPOINT, data=data, headers=headers)
    if resp.status_code != 200:
        # invalid_grant : le refresh token est révoqué/expiré -> on purge pour forcer
        # une reconnexion propre au lieu de boucler sur un jeton mort.
        if resp.status_code == 400:
            try:
                err = resp.json()
                if err.get("error") == "invalid_grant":
                    _delete_tokens(uid)
            except Exception:
                pass
        _raise_spotify_error(resp)
    try:
        new_data = resp.json()
        access = new_data["access_token"]
    except (ValueError, KeyError) as e:
        raise SpotifyError("Réponse Spotify inattendue") from e
    expires_in = new_data.get("expires_in", 3600)
    return {
        "access_token": access,
        # Une réponse de refresh SANS refresh_token garde l'ancien.
        "refresh_token": new_data.get("refresh_token") or refresh_token,
        "expires_at": time.time() + expires_in,
    }


def access_token(uid: str) -> str:
    """Retourne un access_token valide, le rafraîchissant si nécessaire."""
    tokens = _read_tokens(uid)
    if not tokens:
        raise SpotifyError("Compte Spotify non connecté")

    if tokens["expires_at"] - TOKEN_REFRESH_BUFFER <= time.time():
        tokens = _refresh_access_token(uid, tokens["refresh_token"])
        _write_tokens(uid, tokens)
    return tokens["access_token"]


def _is_modifiable(item: Dict[str, Any], user_id: str) -> bool:
    """Une playlist n'est proposée que si l'utilisateur peut y écrire."""
    owner = item.get("owner") or {}
    if owner.get("id") == user_id:
        return True
    return bool(item.get("collaborative"))


def list_playlists(uid: str) -> List[Dict[str, str]]:
    """Liste les playlists Spotify MODIFIABLES de l'utilisateur (max 200)."""
    token = access_token(uid)
    headers = {"Authorization": f"Bearer {token}"}

    # /me d'abord : sans l'id du propriétaire, impossible de distinguer ses
    # playlists de celles auxquelles il est simplement abonné.
    resp = _http("GET", f"{API_BASE}/me", headers=headers)
    if resp.status_code != 200:
        _raise_spotify_error(resp)
    try:
        me = resp.json()
        user_id = me["id"]
    except (ValueError, KeyError) as e:
        raise SpotifyError("Réponse Spotify inattendue") from e

    playlists: List[Dict[str, str]] = []
    offset = 0
    while len(playlists) < MAX_PLAYLISTS:
        params = {"limit": PAGE_SIZE, "offset": offset}
        resp = _http("GET", f"{API_BASE}/me/playlists", headers=headers, params=params)
        if resp.status_code != 200:
            _raise_spotify_error(resp)
        try:
            data = resp.json()
        except ValueError as e:
            raise SpotifyError("Réponse Spotify inattendue") from e
        items = data.get("items") or []
        if not items:
            break
        for item in items:
            if not item:
                continue
            if not _is_modifiable(item, user_id):
                continue
            playlists.append({"id": item["id"], "name": item.get("name", "")})
            if len(playlists) >= MAX_PLAYLISTS:
                break
        offset += len(items)
        if not data.get("next"):
            break
    return playlists


def add_to_playlist(uid: str, playlist_id: str, track_uri: str) -> None:
    """Ajoute une piste (uri spotify:track:...) à l'une des playlists de l'utilisateur."""
    token = access_token(uid)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {"uris": [track_uri]}
    resp = _http(
        "POST", f"{API_BASE}/playlists/{playlist_id}/tracks", headers=headers, json=body,
    )
    if resp.status_code in (200, 201):
        return
    if resp.status_code == 403:
        raise SpotifyError(
            "Spotify refuse l'ajout : tu n'es pas propriétaire de cette playlist "
            "(ou elle n'est pas collaborative)"
        )
    if resp.status_code == 404:
        raise SpotifyError("Playlist Spotify introuvable")
    _raise_spotify_error(resp)


def _normalize(text: str) -> str:
    """Minuscules, sans accents ni ponctuation, espaces normalisés.

    Comparer les titres bruts échoue sur les variantes typographiques courantes
    (« Beyoncé » / « Beyonce », « Don't » / « Dont ») côté catalogue Spotify.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = stripped.lower()
    cleaned = "".join(c if (c.isalnum() or c.isspace()) else " " for c in lowered)
    return " ".join(cleaned.split())


def _title_matches(n_name: str, wanted: str) -> bool:
    """Le nom normalisé égale ou contient le titre demandé."""
    if not wanted:
        return False
    return n_name == wanted or wanted in n_name


def _artist_matches(item: Dict[str, Any], wanted: str) -> bool:
    """Au moins un artiste du résultat correspond à l'artiste demandé (mot entier)."""
    wanted_words = set(wanted.split())
    for artist in item.get("artists") or []:
        n = _normalize((artist or {}).get("name", ""))
        if not n:
            continue
        if n == wanted or wanted_words & set(n.split()):
            return True
    return False


def _has_unwanted(n_name: str, wanted_title: str) -> bool:
    """Écarte remix/instrumental/edit/live/dub, sauf si le titre demandé le contient."""
    # Par mot entier : en sous-chaîne, « edit » écarterait « Edition » et « live »
    # « Olive », donc des pistes légitimes.
    found = set(n_name.split())
    asked = set(wanted_title.split())
    return any(word in found and word not in asked for word in _UNWANTED)


def search_track_uri(uid: str, artist: str, title: str) -> Optional[str]:
    """Recherche une piste et retourne son uri (spotify:track:<id>) ou None.

    On refuse volontairement de renvoyer « un » résultat : coller la mauvaise
    piste dans la playlist de l'utilisateur coûte plus cher qu'un échec explicite.
    """
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title:
        return None

    token = access_token(uid)
    headers = {"Authorization": f"Bearer {token}"}
    if artist:
        q = f'track:"{title}" artist:"{artist}"'
    else:
        q = f'track:"{title}"'
    params = {"type": "track", "limit": 10, "q": q}
    resp = _http("GET", f"{API_BASE}/search", headers=headers, params=params)
    if resp.status_code != 200:
        _raise_spotify_error(resp)
    try:
        data = resp.json()
    except ValueError as e:
        raise SpotifyError("Réponse Spotify inattendue") from e

    items = (data.get("tracks") or {}).get("items") or []
    wanted_title = _normalize(title)
    wanted_artist = _normalize(artist)
    for item in items:
        if not item:
            continue
        n_name = _normalize(item.get("name", ""))
        if not _title_matches(n_name, wanted_title):
            continue
        if _has_unwanted(n_name, wanted_title):
            continue
        if wanted_artist and not _artist_matches(item, wanted_artist):
            continue
        track_id = item.get("id")
        if track_id:
            return f"spotify:track:{track_id}"
    return None


def disconnect(uid: str) -> None:
    """Supprime le stockage local des jetons (Spotify n'expose pas de révocation)."""
    _delete_tokens(uid)


def _raise_spotify_error(resp: requests.Response) -> None:
    """Analyse la réponse d'erreur Spotify et lève SpotifyError avec message propre.

    Spotify renvoie deux formes : {error: {status, message}} pour l'API, et
    {error: "code", error_description: "..."} pour le endpoint de jeton. On ne
    recopie jamais la requête (elle porte le secret/les jetons).
    """
    msg = "Erreur API Spotify"
    try:
        data = resp.json()
    except Exception:
        data = {}
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or msg
    elif isinstance(err, str):
        if err == "invalid_grant":
            msg = "Autorisation Spotify expirée, reconnecte ton compte"
        else:
            msg = data.get("error_description") or f"Erreur Spotify: {err}"
    raise SpotifyError(msg)
