"""Écriture sur playlist YouTube (RECOS RADAR) — OAuth2 par utilisateur.

Même SDK que le script perso existant de l'utilisateur (google-auth-oauthlib +
google-api-python-client, cf. `add_to_playlist.py`) : on ne réinvente pas le
flux de refresh/erreurs qu'il a déjà validé, seule la façon d'obtenir le jeton
change — flux "web application" (redirect HTTPS fixe vers l'appli, déjà en
place sur https://radar.hubclaudel.fr) plutôt que "installed app"
(`run_local_server`, qui suppose un navigateur sur la même machine que le
process Python — impossible depuis un serveur). Jeton stocké par utilisateur
sous `paths.user_dir(uid)`, jamais un `token.pickle` global.

Prérequis côté Google Cloud Console — À FAIRE (pas automatisable) : un client
OAuth de type **Web application** (le client_secret.json existant est
probablement de type Desktop, qui n'accepte pas de redirect_uri HTTPS custom)
avec `https://<domaine>/oauth/youtube/callback` en URI de redirection
autorisée, puis `YOUTUBE_OAUTH_CLIENT_ID`/`YOUTUBE_OAUTH_CLIENT_SECRET` dans le
`.env` du VPS (secrets d'appli, comme YOUTUBE_API_KEY — jamais par-utilisateur)."""
import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import paths

SCOPES = ["https://www.googleapis.com/auth/youtube"]
_QUOTA_REASONS = {"quotaexceeded", "dailylimitexceeded", "ratelimitexceeded"}


class YouTubeAuthError(RuntimeError):
    """Jamais connecté, ou refresh échoué (invalid_grant, cf. script perso) —
    reconnexion nécessaire, jamais une simple retentative."""


class YouTubeQuotaExhausted(RuntimeError):
    pass


def client_config(redirect_uri):
    cid = os.environ.get("YOUTUBE_OAUTH_CLIENT_ID", "")
    csec = os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET", "")
    if not cid or not csec:
        raise YouTubeAuthError("YOUTUBE_OAUTH_CLIENT_ID/YOUTUBE_OAUTH_CLIENT_SECRET absents "
                               "de l'environnement (secrets d'appli, .env du VPS).")
    return {"web": {"client_id": cid, "client_secret": csec,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri]}}


def authorization_url(redirect_uri):
    """URL de consentement Google + state + code_verifier PKCE — les deux à
    revalider/réutiliser au retour (cf. route callback). `Flow.authorization_url()`
    génère le code_verifier et n'envoie que le code_challenge (dérivé) à Google ;
    il faut donc le transporter nous-mêmes jusqu'à l'échange (google-auth-oauthlib
    ne le fait pas pour nous entre deux objets Flow distincts, un par requête HTTP
    ici) — sinon Google répond invalid_grant: Missing code verifier."""
    flow = Flow.from_client_config(client_config(redirect_uri), scopes=SCOPES, redirect_uri=redirect_uri)
    url, state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true")
    return url, state, flow.code_verifier


def exchange_code(redirect_uri, code, code_verifier):
    """Échange le code d'autorisation contre des identifiants — à appeler depuis
    la route de callback avec le MÊME code_verifier que celui généré par
    authorization_url() (cf. sa docstring), puis `save_credentials(uid, creds)`."""
    flow = Flow.from_client_config(client_config(redirect_uri), scopes=SCOPES,
                                   redirect_uri=redirect_uri, code_verifier=code_verifier)
    flow.fetch_token(code=code)
    return flow.credentials


def _creds_path(uid):
    return paths.user_paths(uid).youtube_oauth


def _creds_to_dict(creds):
    return {"token": creds.token, "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri, "client_id": creds.client_id,
            "client_secret": creds.client_secret, "scopes": creds.scopes}


def save_credentials(uid, creds):
    p = _creds_path(uid)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_creds_to_dict(creds), f)
    os.replace(tmp, p)


def load_credentials(uid):
    try:
        with open(_creds_path(uid), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not d.get("refresh_token"):
        return None
    return Credentials(token=d.get("token"), refresh_token=d["refresh_token"],
                       token_uri=d.get("token_uri"), client_id=d.get("client_id"),
                       client_secret=d.get("client_secret"), scopes=d.get("scopes"))


def disconnect(uid):
    try:
        os.remove(_creds_path(uid))
    except OSError:
        pass


def is_connected(uid):
    return load_credentials(uid) is not None


def get_client(uid):
    """Client googleapiclient authentifié pour `uid`, refresh transparent si le
    jeton d'accès est expiré (comme le script perso). Lève YouTubeAuthError si
    jamais connecté ou si le refresh échoue (jeton révoqué côté Google) — au
    job appelant de traiter ça comme "reconnecte YouTube", jamais en boucle de
    retentatives."""
    creds = load_credentials(uid)
    if not creds:
        raise YouTubeAuthError("YouTube non connecté.")
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise YouTubeAuthError("YouTube non connecté (jeton invalide, pas de refresh token).")
        try:
            creds.refresh(Request())
        except Exception as e:
            raise YouTubeAuthError(f"Jeton YouTube expiré ou révoqué, reconnexion nécessaire "
                                   f"({type(e).__name__}).") from e
        save_credentials(uid, creds)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _is_quota_error(e):
    if e.status_code != 403:
        return False
    details = e.error_details
    reasons = ({(d.get("reason") or "").lower() for d in details}
               if isinstance(details, list) else set())
    return bool(reasons & _QUOTA_REASONS) or "quota" in (e.reason or "").lower()


def get_or_create_playlist(client, name):
    """id de la playlist `name` du compte connecté, créée (privée) si absente —
    même logique que le script perso (get_or_create_playlist)."""
    req = client.playlists().list(part="snippet", mine=True, maxResults=50)
    while req is not None:
        resp = req.execute()
        for item in resp.get("items", []):
            if item["snippet"]["title"].strip().lower() == name.strip().lower():
                return item["id"]
        req = client.playlists().list_next(req, resp)
    resp = client.playlists().insert(
        part="snippet,status",
        body={"snippet": {"title": name, "description": "Playlist générée par Radar"},
              "status": {"privacyStatus": "private"}}).execute()
    return resp["id"]


def existing_video_ids(client, playlist_id):
    """{videoId: playlistItemId} des vidéos actuellement dans la playlist —
    playlistItemId nécessaire pour un `remove_item` ciblé (lot 3)."""
    out = {}
    req = client.playlistItems().list(part="contentDetails", playlistId=playlist_id, maxResults=50)
    while req is not None:
        try:
            resp = req.execute()
        except HttpError as e:
            if e.status_code == 404:
                return out          # playlist tout juste créée, pas encore indexée côté Google
            raise
        for item in resp.get("items", []):
            out[item["contentDetails"]["videoId"]] = item["id"]
        req = client.playlistItems().list_next(req, resp)
    return out


def add_video(client, playlist_id, video_id):
    try:
        client.playlistItems().insert(
            part="snippet",
            body={"snippet": {"playlistId": playlist_id,
                              "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()
    except HttpError as e:
        if _is_quota_error(e):
            raise YouTubeQuotaExhausted("Quota YouTube (écriture) épuisé pour aujourd'hui.") from e
        raise


def remove_item(client, playlist_item_id):
    client.playlistItems().delete(id=playlist_item_id).execute()
