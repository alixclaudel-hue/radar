#!/usr/bin/env python3
import argparse
import hashlib
import os
import sys
from typing import Any, Dict

import requests

BASE_URL = 'https://bandcamp.com/api/subsonic/rest'
DEFAULT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    ),
}
_AUTH_PARAMS: Dict[str, str] = {}


class ProbeRequestError(RuntimeError):
    pass


def build_auth_params(user: str, password: str) -> Dict[str, str]:
    salt = os.urandom(8).hex()
    token = hashlib.md5((password + salt).encode('utf-8')).hexdigest()
    return {
        'u': user,
        't': token,
        's': salt,
        'v': '1.16.1',
        'c': 'CrateRadar',
        'f': 'json',
    }


def call(methode: str, **params: Any) -> requests.Response:
    extra_headers = params.pop('headers', {})
    stream = params.pop('stream', False)

    request_headers = dict(DEFAULT_HEADERS)
    if extra_headers:
        request_headers.update(extra_headers)

    try:
        response = requests.get(
            BASE_URL,
            params={**params, **_AUTH_PARAMS, 'm': methode},
            headers=request_headers,
            timeout=25,
            allow_redirects=False,
            stream=stream,
        )
        body = response.text[:700]
    except requests.RequestException as exc:
        raise ProbeRequestError(
            f'GET {methode} impossible ({type(exc).__name__})'
        ) from None

    content_type = response.headers.get('Content-Type', '')
    location = response.headers.get('Location', '')
    print(
        f'== {methode} HTTP {response.status_code} '
        f'content-type {content_type} location {location}'
    )
    print(body)
    print()
    return response


def main() -> int:
    global _AUTH_PARAMS

    parser = argparse.ArgumentParser(
        description='Sonde Bandcamp Subsonic en lecture seule.'
    )
    parser.add_argument(
        '--owned',
        metavar='TEXTE',
        required=True,
        help='requête search3 pour les éléments possédés',
    )
    parser.add_argument(
        '--unowned',
        metavar='TEXTE',
        required=True,
        help='requête search3 pour les éléments non possédés',
    )
    args = parser.parse_args()

    user = os.environ.get('RADAR_SUBSONIC_USER')
    password = os.environ.get('RADAR_SUBSONIC_PASS')

    missing = []
    if not user:
        missing.append('RADAR_SUBSONIC_USER')
    if not password:
        missing.append('RADAR_SUBSONIC_PASS')

    if missing:
        print(
            'Erreur : variables d’environnement requises mais absentes ou vides : '
            + ', '.join(missing),
            file=sys.stderr,
        )
        return 2

    assert user is not None and password is not None
    _AUTH_PARAMS = build_auth_params(user, password)

    album_list_response = call(
        'getAlbumList2',
        type='alphabeticalByName',
        size=3,
    )
    call('search3', query=args.owned)
    call('search3', query=args.unowned)
    call('getStarred2')
    call('getStarred')

    try:
        album_list_payload = album_list_response.json()
        album_id = (
            album_list_payload['subsonic-response']['albumList2']['album'][0]['id']
        )

        album_response = call('getAlbum', id=album_id)
        album_payload = album_response.json()
        song_id = album_payload['subsonic-response']['album']['song'][0]['id']

        stream_response = call(
            'stream',
            id=song_id,
            headers={'Range': 'bytes=0-1023'},
            stream=True,
        )
        stream_content_type = stream_response.headers.get('Content-Type', '')
        stream_location = stream_response.headers.get('Location', '')
        print(
            f'== stream HTTP {stream_response.status_code} '
            f'content-type {stream_content_type} '
            f'location {stream_location[:80]}'
        )
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}')
        return 1

    return 0


if __name__ == '__main__':
    try:
        exit_code = main()
    except ProbeRequestError as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)
