#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, TypedDict
from urllib.parse import quote, urlsplit

import requests


API_BASE = 'https://bandcamp.com/api/subsonic/rest'
FAN_COLLECTION_URL = 'https://bandcamp.com/api/fancollection/1/collection_items'
TIMEOUT_SECONDS = 25
CHALLENGE_DETAIL = 'IP filtrée (Fastly) : relancer depuis une IP résidentielle'
MAX_DETAIL_LENGTH = 1500

ResultStatus = Literal['PASS', 'FAIL', 'BLOCKED', 'SKIP']
Classification = Literal['ok', 'challenge', 'http_error', 'bad_json']


class ControlResult(TypedDict):
    name: str
    status: ResultStatus
    detail: str


@dataclass(frozen=True)
class Credentials:
    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass
class ProbeState:
    stream_song_id: Optional[str] = None


def classify(response_text: str, content_type: str, status: int) -> Classification:
    media_type = content_type.partition(';')[0].strip().lower()
    beginning = response_text.lstrip('\ufeff \t\r\n').lower()
    if media_type == 'text/html' or beginning.startswith('<!doctype') or beginning.startswith('<html'):
        return 'challenge'
    if status < 200 or status >= 300:
        return 'http_error'
    try:
        json.loads(response_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return 'bad_json'
    return 'ok'


def clean_detail(value: object, limit: int = MAX_DETAIL_LENGTH) -> str:
    text = ''.join(character if character.isprintable() else ' ' for character in str(value))
    text = ' '.join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + '…'


def make_result(name: str, status: ResultStatus, detail: str) -> ControlResult:
    return {'name': name, 'status': status, 'detail': clean_detail(detail)}


def blocked_result(name: str, detail: str) -> ControlResult:
    return make_result(name, 'BLOCKED', f'{detail} ; {CHALLENGE_DETAIL}')


def subsonic_parameters(credentials: Credentials, **extra: object) -> Dict[str, str]:
    salt = secrets.token_hex(16)
    token = hashlib.md5((credentials.password + salt).encode('utf-8')).hexdigest()
    parameters = {
        'u': credentials.username,
        't': token,
        's': salt,
        'v': '1.16.1',
        'c': 'CrateRadar',
        'f': 'json',
    }
    parameters.update({key: str(value) for key, value in extra.items()})
    return parameters


def subsonic_get(
    session: requests.Session,
    credentials: Credentials,
    method: str,
    **request_options: Any,
) -> requests.Response:
    return session.get(
        f'{API_BASE}/{method}',
        params=subsonic_parameters(credentials),
        timeout=TIMEOUT_SECONDS,
        **request_options,
    )


def subsonic_root(payload: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    root = payload.get('subsonic-response')
    return root if isinstance(root, dict) else None


def subsonic_error(root: Dict[str, Any]) -> Optional[str]:
    if root.get('status') == 'ok':
        return None
    error = root.get('error')
    if isinstance(error, dict):
        code = error.get('code')
        if isinstance(code, (int, float)) and not isinstance(code, bool):
            return f'reponse Subsonic en erreur (code {int(code)})'
    return 'reponse Subsonic invalide ou en erreur'


def direct_items(container: Any, key: str) -> List[Dict[str, Any]]:
    if not isinstance(container, dict):
        return []
    value = container.get(key, [])
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def identifier(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    value = item.get('id')
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        candidate = str(value).strip()
        if candidate and len(candidate) <= 2048 and all(character.isprintable() for character in candidate):
            return candidate
    return None


def child_items(container: Any) -> List[Dict[str, Any]]:
    return direct_items(container, 'child')


def node_type(item: Dict[str, Any]) -> Optional[str]:
    value = item.get('type')
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {'song', 'album', 'artist'}:
            return normalized
    if 'songCount' in item:
        return 'album'
    if 'albumCount' in item:
        return 'artist'
    if 'albumId' in item and 'isDir' not in item:
        return 'song'
    return None


def matching_children(container: Any, kind: str) -> List[Dict[str, Any]]:
    return [item for item in child_items(container) if node_type(item) == kind]


def count_field(root: Dict[str, Any], name: str) -> int:
    value = root.get(name)
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value.strip()))
        except ValueError:
            return 0
    return 0


def search_type_count(root: Dict[str, Any], search: Dict[str, Any], kind: str) -> int:
    declared = count_field(root, f'{kind}Count')
    observed = len(direct_items(search, kind)) + len(matching_children(search, kind))
    return max(declared, observed)


def first_song_id(search: Dict[str, Any]) -> Optional[str]:
    songs = direct_items(search, 'song') + matching_children(search, 'song')
    for song in songs:
        song_id = identifier(song)
        if song_id is not None:
            return song_id
    return None


def first_album_id(value: Any) -> Optional[str]:
    if isinstance(value, list):
        for item in value:
            album_id = first_album_id(item)
            if album_id is not None:
                return album_id
        return None
    if not isinstance(value, dict):
        return None
    albums = direct_items(value, 'album')
    for album in albums:
        album_id = identifier(album)
        if album_id is not None:
            return album_id
    for child in child_items(value):
        if node_type(child) == 'album':
            album_id = identifier(child)
            if album_id is not None:
                return album_id
        album_id = first_album_id(child)
        if album_id is not None:
            return album_id
    return None


def first_song_from_album(album: Any) -> Optional[str]:
    if isinstance(album, list):
        for item in album:
            song_id = first_song_from_album(item)
            if song_id is not None:
                return song_id
        return None
    if not isinstance(album, dict):
        return None
    songs = direct_items(album, 'song') + matching_children(album, 'song')
    for song in songs:
        song_id = identifier(song)
        if song_id is not None:
            return song_id
    return None


def response_prefix(response: requests.Response, limit: int) -> str:
    iterator = response.iter_content(chunk_size=limit)
    chunk = next(iterator, b'')
    if not isinstance(chunk, bytes):
        chunk = bytes(chunk)
    encoding = response.encoding or 'utf-8'
    return chunk[:limit].decode(encoding, errors='replace')


def api_response_detail(response: requests.Response) -> str:
    content_type = response.headers.get('Content-Type', 'absent')
    return f'HTTP {response.status_code}; content-type: {content_type}'


def exception_detail(prefix: str, error: Exception) -> str:
    return f'{prefix} ({type(error).__name__})'


def control_ping(session: requests.Session, credentials: Optional[Credentials]) -> ControlResult:
    name = '1 ping'
    if credentials is None:
        return make_result(name, 'SKIP', 'Identifiants Subsonic absents dans l’environnement.')
    try:
        response = subsonic_get(session, credentials, 'ping')
        outcome = classify(response.text, response.headers.get('Content-Type', ''), response.status_code)
        if outcome == 'challenge':
            return blocked_result(name, 'ping a reçu une page HTML de défi.')
        if outcome == 'http_error':
            return make_result(name, 'FAIL', api_response_detail(response))
        if outcome == 'bad_json':
            return make_result(name, 'FAIL', 'Réponse JSON invalide ou absente.')
        root = subsonic_root(json.loads(response.text))
        if root is None:
            return make_result(name, 'FAIL', 'Enveloppe subsonic-response absente.')
        error = subsonic_error(root)
        if error is not None:
            return make_result(name, 'FAIL', error)
        return make_result(name, 'PASS', 'API Subsonic accessible et authentifiée.')
    except Exception as error:
        return make_result(name, 'FAIL', exception_detail('Erreur pendant ping', error))


def control_album_list(session: requests.Session, credentials: Optional[Credentials]) -> ControlResult:
    name = '2 getAlbumList2'
    if credentials is None:
        return make_result(name, 'SKIP', 'Identifiants Subsonic absents dans l’environnement.')
    try:
        response = subsonic_get(
            session,
            credentials,
            'getAlbumList2',
            params=subsonic_parameters(credentials, size=5, type='alphabeticalByName'),
        )
        outcome = classify(response.text, response.headers.get('Content-Type', ''), response.status_code)
        if outcome == 'challenge':
            return blocked_result(name, 'getAlbumList2 a reçu une page HTML de défi.')
        if outcome == 'http_error':
            return make_result(name, 'FAIL', api_response_detail(response))
        if outcome == 'bad_json':
            return make_result(name, 'FAIL', 'Réponse JSON invalide ou absente.')
        root = subsonic_root(json.loads(response.text))
        if root is None:
            return make_result(name, 'FAIL', 'Enveloppe subsonic-response absente.')
        error = subsonic_error(root)
        if error is not None:
            return make_result(name, 'FAIL', error)
        albums = direct_items(root.get('albumList'), 'album')
        return make_result(name, 'PASS', f'Liste des achats accessible : {len(albums)} album(s) renvoyé(s).')
    except Exception as error:
        return make_result(name, 'FAIL', exception_detail('Erreur pendant getAlbumList2', error))


def control_search_owned(
    session: requests.Session,
    credentials: Optional[Credentials],
    query: Optional[str],
    state: ProbeState,
) -> ControlResult:
    name = '3 search3 --owned'
    state.stream_song_id = None
    if credentials is None:
        return make_result(name, 'SKIP', 'Identifiants Subsonic absents dans l’environnement.')
    if query is None:
        return make_result(name, 'SKIP', 'Option --owned non fournie.')
    try:
        response = subsonic_get(
            session,
            credentials,
            'search3',
            params=subsonic_parameters(credentials, query=query),
        )
        outcome = classify(response.text, response.headers.get('Content-Type', ''), response.status_code)
        if outcome == 'challenge':
            return blocked_result(name, 'search3 --owned a reçu une page HTML de défi.')
        if outcome == 'http_error':
            return make_result(name, 'FAIL', api_response_detail(response))
        if outcome == 'bad_json':
            return make_result(name, 'FAIL', 'Réponse JSON invalide ou absente.')
        root = subsonic_root(json.loads(response.text))
        if root is None:
            return make_result(name, 'FAIL', 'Enveloppe subsonic-response absente.')
        error = subsonic_error(root)
        if error is not None:
            return make_result(name, 'FAIL', error)
        search = root.get('searchResult3')
        if not isinstance(search, dict):
            search = {}
        result_count = (
            search_type_count(root, search, 'song')
            + search_type_count(root, search, 'album')
            + search_type_count(root, search, 'artist')
        )
        if result_count < 1:
            return make_result(name, 'FAIL', 'Aucun résultat pour le contenu possédé indiqué.')

        state.stream_song_id = first_song_id(search)
        resolution_note = ''
        if state.stream_song_id is None:
            album_id = first_album_id(search)
            if album_id is not None:
                album_response = subsonic_get(
                    session,
                    credentials,
                    'getAlbum',
                    params=subsonic_parameters(credentials, id=album_id),
                )
                album_outcome = classify(
                    album_response.text,
                    album_response.headers.get('Content-Type', ''),
                    album_response.status_code,
                )
                if album_outcome == 'challenge':
                    return blocked_result(name, 'Résultat trouvé, mais getAlbum a reçu une page HTML de défi.')
                if album_outcome != 'ok':
                    resolution_note = f'; album non résolu ({api_response_detail(album_response)})'
                else:
                    album_root = subsonic_root(json.loads(album_response.text))
                    if album_root is None:
                        resolution_note = '; album non résolu (enveloppe absente)'
                    else:
                        album_error = subsonic_error(album_root)
                        if album_error is not None:
                            resolution_note = f'; album non résolu ({album_error})'
                        else:
                            state.stream_song_id = first_song_from_album(album_root.get('album'))
            else:
                resolution_note = '; aucun album exploitable pour stream'

        detail = f'{result_count} résultat(s) trouvé(s).'
        if state.stream_song_id is not None:
            detail += ' Un identifiant de morceau est disponible pour stream.'
        else:
            detail += resolution_note or ' Aucun morceau directement exploitable.'
        return make_result(name, 'PASS', detail)
    except Exception as error:
        return make_result(name, 'FAIL', exception_detail('Erreur pendant search3 --owned', error))


def control_search_unowned(
    session: requests.Session,
    credentials: Optional[Credentials],
    query: str,
) -> ControlResult:
    name = '4 search3 --unowned'
    if credentials is None:
        return make_result(name, 'SKIP', 'Identifiants Subsonic absents dans l’environnement.')
    try:
        response = subsonic_get(
            session,
            credentials,
            'search3',
            params=subsonic_parameters(credentials, query=query),
        )
        outcome = classify(response.text, response.headers.get('Content-Type', ''), response.status_code)
        if outcome == 'challenge':
            return blocked_result(name, 'search3 --unowned a reçu une page HTML de défi.')
        if outcome == 'http_error':
            return make_result(name, 'FAIL', api_response_detail(response))
        if outcome == 'bad_json':
            return make_result(name, 'FAIL', 'Réponse JSON invalide ou absente.')
        root = subsonic_root(json.loads(response.text))
        if root is None:
            return make_result(name, 'FAIL', 'Enveloppe subsonic-response absente.')
        error = subsonic_error(root)
        if error is not None:
            return make_result(name, 'FAIL', error)
        search = root.get('searchResult3')
        if not isinstance(search, dict):
            search = {}
        songs = search_type_count(root, search, 'song')
        albums = search_type_count(root, search, 'album')
        if songs + albums > 0:
            return make_result(
                name,
                'PASS',
                f'Catalogue Bandcamp couvert : songCount={songs}, albumCount={albums}.',
            )
        return make_result(name, 'FAIL', 'recherche limitée à la bibliothèque achetée')
    except Exception as error:
        return make_result(name, 'FAIL', exception_detail('Erreur pendant search3 --unowned', error))


def audio_content_type(content_type: str) -> bool:
    return content_type.partition(';')[0].strip().lower().startswith('audio/')


def audio_location(location: str) -> bool:
    if not location:
        return False
    parsed = urlsplit(location)
    path = parsed.path.lower()
    suffixes = ('.mp3', '.m4a', '.aac', '.flac', '.ogg', '.oga', '.opus', '.wav', '.aif', '.aiff', '.alac')
    if path.endswith(suffixes):
        return True
    query = parsed.query.lower()
    return any(token in query for token in ('format=mp3', 'format=flac', 'format=m4a', 'audio='))


def stream_detail(response: requests.Response) -> str:
    location = response.headers.get('Location', 'absent')
    content_type = response.headers.get('Content-Type', 'absent')
    return f'HTTP {response.status_code}; Location: {location}; content-type: {content_type}'


def control_stream(
    session: requests.Session,
    credentials: Optional[Credentials],
    state: ProbeState,
) -> ControlResult:
    name = '5 stream'
    if credentials is None:
        return make_result(name, 'SKIP', 'Identifiants Subsonic absents dans l’environnement.')
    if state.stream_song_id is None:
        return make_result(name, 'SKIP', 'Aucun identifiant de morceau fourni par le contrôle search3 --owned.')
    response: Optional[requests.Response] = None
    try:
        response = subsonic_get(
            session,
            credentials,
            'stream',
            params=subsonic_parameters(credentials, id=state.stream_song_id),
            headers={'Range': 'bytes=0-1023'},
            allow_redirects=False,
            stream=True,
        )
        location = response.headers.get('Location', '')
        content_type = response.headers.get('Content-Type', '')
        sample = response_prefix(response, 4096)

        if response.status_code == 302 and (audio_content_type(content_type) or audio_location(location)):
            return make_result(name, 'PASS', stream_detail(response))

        outcome = classify(sample, content_type, response.status_code)
        if outcome == 'challenge':
            return blocked_result(name, 'stream a reçu une page HTML de défi.')
        if response.status_code in {200, 206}:
            return make_result(name, 'PASS', stream_detail(response))
        if response.status_code == 302:
            return make_result(name, 'FAIL', stream_detail(response) + ' ; redirection non/audio.')
        return make_result(name, 'FAIL', stream_detail(response))
    except Exception as error:
        return make_result(name, 'FAIL', exception_detail('Erreur pendant stream', error))
    finally:
        if response is not None:
            response.close()


def starred_count(starred: Any) -> int:
    if isinstance(starred, list):
        return sum(1 for item in starred if isinstance(item, dict))
    if not isinstance(starred, dict):
        return 0
    if 'child' in starred:
        return len(child_items(starred))
    total = 0
    found = False
    for kind in ('song', 'album', 'artist'):
        if kind in starred:
            found = True
            total += len(direct_items(starred, kind))
    return total if found else 0


def control_starred(session: requests.Session, credentials: Optional[Credentials]) -> ControlResult:
    name = '6 getStarred2'
    if credentials is None:
        return make_result(name, 'SKIP', 'Identifiants Subsonic absents dans l’environnement.')
    try:
        response = subsonic_get(session, credentials, 'getStarred2')
        outcome = classify(response.text, response.headers.get('Content-Type', ''), response.status_code)
        if outcome == 'challenge':
            return blocked_result(name, 'getStarred2 a reçu une page HTML de défi.')
        if outcome == 'http_error':
            return make_result(name, 'FAIL', api_response_detail(response))
        if outcome == 'bad_json':
            return make_result(name, 'FAIL', 'Réponse JSON invalide ou absente.')
        root = subsonic_root(json.loads(response.text))
        if root is None:
            return make_result(name, 'FAIL', 'Enveloppe subsonic-response absente.')
        error = subsonic_error(root)
        if error is not None:
            return make_result(name, 'FAIL', error)
        count = starred_count(root.get('starred2'))
        return make_result(name, 'PASS', f'{count} élément(s) ; wishlist exposée.')
    except Exception as error:
        return make_result(name, 'FAIL', exception_detail('Erreur pendant getStarred2', error))


def profile_html_challenge(text: str, content_type: str, status: int) -> bool:
    if content_type.partition(';')[0].strip().lower() != 'text/html':
        return False
    if status in {403, 429, 503}:
        return True
    lowered = text.lower()
    # Bandcamp est derrière Fastly : sa page de blocage s'intitule « Client
    # Challenge » (assets sous /_fs-ch-…), sans aucun marqueur Cloudflare — le
    # contrôle 7 la prenait pour un vrai profil (faux PASS constaté le 01/10).
    markers = (
        'client challenge',
        '/_fs-ch-',
        'challenge-platform',
        'cf-chl-',
        'just a moment',
        'attention required! | cloudflare',
        'checking your browser',
        'verify you are human',
        'access denied',
        'you have been blocked',
        'error 1015',
        'enable javascript and cookies to continue',
    )
    return any(marker in lowered for marker in markers)


def control_profile(session: requests.Session, profile: Optional[str]) -> ControlResult:
    name = '7 profil public / fancollection'
    if profile is None:
        return make_result(name, 'SKIP', 'Option --profile non fournie.')
    profile_response: Optional[requests.Response] = None
    collection_response: Optional[requests.Response] = None
    try:
        profile_response = session.get(
            f'https://bandcamp.com/{quote(profile, safe="")}',
            timeout=TIMEOUT_SECONDS,
            stream=True,
        )
        profile_text = response_prefix(profile_response, 65536)
        profile_type = profile_response.headers.get('Content-Type', '')

        collection_response = session.post(
            FAN_COLLECTION_URL,
            json={},
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
        )
        collection_text = response_prefix(collection_response, 65536)
        collection_type = collection_response.headers.get('Content-Type', '')
        detail = (
            f'GET profil: HTTP {profile_response.status_code}, content-type: {profile_type}; '
            f'POST collection_items: HTTP {collection_response.status_code}, '
            f'content-type: {collection_type}; fan_id absent, accessibilité seule.'
        )

        get_challenge = profile_html_challenge(
            profile_text,
            profile_type,
            profile_response.status_code,
        )
        post_classification = classify(
            collection_text,
            collection_type,
            collection_response.status_code,
        )
        if get_challenge or post_classification == 'challenge':
            endpoint = 'GET profil' if get_challenge else 'POST collection_items'
            return blocked_result(name, f'{endpoint} a reçu une page HTML de défi ; {detail}')
        return make_result(name, 'PASS', detail)
    except Exception as error:
        return make_result(name, 'FAIL', exception_detail('Erreur pendant le test profil/fancollection', error))
    finally:
        if profile_response is not None:
            profile_response.close()
        if collection_response is not None:
            collection_response.close()


def profile_argument(value: str) -> str:
    profile = value.strip()
    if profile.startswith('@'):
        profile = profile[1:]
    if (
        not profile
        or profile in {'.', '..'}
        or '/' in profile
        or '\\' in profile
        or '://' in profile
        or '?' in profile
        or '#' in profile
    ):
        raise argparse.ArgumentTypeError('Le profil doit être un nom de compte Bandcamp, sans URL.')
    return profile


def optional_text(value: str) -> str:
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError('Le texte ne peut pas être vide.')
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Sonde les capacités Subsonic et fancollection de Bandcamp sans exposer les identifiants.'
    )
    parser.add_argument(
        '--unowned',
        required=True,
        type=optional_text,
        metavar='ARTISTE - TITRE',
        help='Piste搜 que l’utilisateur ne possède pas, utilisée pour tester la couverture du catalogue.',
    )
    parser.add_argument(
        '--owned',
        type=optional_text,
        metavar='TEXTE',
        help='Fragment d’un album possédé pour trouver un morceau streamable.',
    )
    parser.add_argument(
        '--profile',
        type=profile_argument,
        metavar='NOM',
        help='Compte Bandcamp public supplémentaire à tester.',
    )
    parser.add_argument('--json', action='store_true', help='Affiche les résultats JSON.')
    return parser


def render_table(results: List[ControlResult]) -> None:
    headers = ('CONTRÔLE', 'STATUT', 'DÉTAIL')
    rows = [
        (result['name'], result['status'], clean_detail(result['detail']))
        for result in results
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def format_row(row: tuple[str, str, str]) -> str:
        return '  '.join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()

    print(format_row(headers))
    print('-+-'.join('-' * width for width in widths))
    for row in rows:
        print(format_row(row))


def credentials_from_environment() -> Optional[Credentials]:
    username = os.environ.get('RADAR_SUBSONIC_USER')
    password = os.environ.get('RADAR_SUBSONIC_PASS')
    if username is None or password is None or not username or not password:
        return None
    return Credentials(username=username, password=password)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    credentials = credentials_from_environment()
    state = ProbeState()

    session = requests.Session()
    session.headers.update(
        {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/131.0.0.0 Safari/537.36'
            ),
            'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
        }
    )

    with session:
        results = [
            control_ping(session, credentials),
            control_album_list(session, credentials),
            control_search_owned(session, credentials, args.owned, state),
            control_search_unowned(session, credentials, args.unowned),
            control_stream(session, credentials, state),
            control_starred(session, credentials),
            control_profile(session, args.profile),
        ]

    if args.json:
        json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write('\n')
    else:
        render_table(results)

    return 1 if any(result['status'] in {'FAIL', 'BLOCKED'} for result in results) else 0


if __name__ == '__main__':
    sys.exit(main())
