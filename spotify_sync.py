"""Spotify output for the playlist creator.

Tracks are still picked by the normal modes (YouTube Music data); this module
signs in to Spotify, matches each picked track to a Spotify track by artist +
title, and adds it to a Spotify playlist.

Auth is Authorization Code + PKCE, so only a Client ID is needed (no client
secret). Spotify requires loopback IP redirect URIs (127.0.0.1, not
localhost). Tokens are cached in spotify_token.json.

Written against the post-February-2026 Web API: playlist items live under
/items (falls back to the old /tracks path), search returns at most 10
results, and Development Mode apps need a Premium account owner.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from config import get_key

TOKEN_FILE = 'spotify_token.json'
SCOPES = 'playlist-modify-private playlist-modify-public'
AUTH_URL = 'https://accounts.spotify.com/authorize'
TOKEN_URL = 'https://accounts.spotify.com/api/token'
API = 'https://api.spotify.com/v1'
CLI_PORT = 8888
CLI_REDIRECT = f'http://127.0.0.1:{CLI_PORT}/callback'


def web_redirect(port=None):
    port = port or int(os.environ.get('PORT', 5000))
    return f'http://127.0.0.1:{port}/spotify/callback'


class SpotifyAuthError(Exception):
    pass


class SpotifyQuota(Exception):
    pass


class SpotifyForbidden(Exception):
    """Spotify refused the account (not allowed on the developer app,
    no Premium, or missing permissions)."""


def _json(r, what):
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"Spotify sent an unexpected reply when trying to {what} "
                           f"(HTTP {r.status_code}): {r.text[:200]}") from None


def _explain_403(r):
    text = (r.text or '').strip()[:200]
    return ("Spotify refused (403): " + (text or 'no reason given') + ". Usual fixes: "
            "(1) the Spotify account that owns the developer app needs Premium; "
            "(2) in the developer dashboard, open your app > Settings > User Management "
            "and add the email of the Spotify account you signed in with; "
            "(3) click Disconnect then Connect Spotify again so it asks for playlist permission.")


def client_id():
    return get_key('SPOTIFY_CLIENT_ID')


# --- PKCE sign-in -----------------------------------------------------------

def begin_auth(redirect_uri):
    """Return (auth_url, pending) where pending must be passed to finish_auth."""
    cid = client_id()
    if not cid:
        raise SpotifyAuthError('No Spotify Client ID saved yet.')
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + '?' + urllib.parse.urlencode({
        'client_id': cid, 'response_type': 'code', 'redirect_uri': redirect_uri,
        'scope': SCOPES, 'state': state, 'code_challenge_method': 'S256',
        'code_challenge': challenge,
    })
    return url, {'verifier': verifier, 'state': state, 'redirect_uri': redirect_uri}


def finish_auth(pending, code, state):
    if state != pending['state']:
        raise SpotifyAuthError('Sign-in state mismatch; start again.')
    r = requests.post(TOKEN_URL, data={
        'grant_type': 'authorization_code', 'code': code,
        'redirect_uri': pending['redirect_uri'], 'client_id': client_id(),
        'code_verifier': pending['verifier'],
    }, timeout=30)
    if r.status_code != 200:
        raise SpotifyAuthError(f'Spotify refused the sign-in: {r.text[:200]}')
    _save_token(r.json())


def _save_token(tok, old=None):
    tok = dict(tok)
    if old and not tok.get('refresh_token'):
        tok['refresh_token'] = old.get('refresh_token')
    tok['expires_at'] = time.time() + int(tok.get('expires_in', 3600)) - 60
    with open(TOKEN_FILE, 'w') as f:
        json.dump(tok, f)


def _load_token():
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def is_connected():
    tok = _load_token()
    return bool(tok and tok.get('refresh_token'))


def access_token():
    tok = _load_token()
    if not tok:
        raise SpotifyAuthError('Not signed in to Spotify.')
    if time.time() < tok.get('expires_at', 0):
        return tok['access_token']
    r = requests.post(TOKEN_URL, data={
        'grant_type': 'refresh_token', 'refresh_token': tok.get('refresh_token'),
        'client_id': client_id(),
    }, timeout=30)
    if r.status_code != 200:
        raise SpotifyAuthError('Spotify sign-in expired; connect Spotify again.')
    _save_token(r.json(), tok)
    return _load_token()['access_token']


def cli_login():
    """Sign in from the terminal: opens the browser and catches the redirect
    on http://127.0.0.1:8888/callback."""
    url, pending = begin_auth(CLI_REDIRECT)
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<h2>Spotify connected. You can close this tab.</h2>')

        def log_message(self, *a):
            pass

    server = HTTPServer(('127.0.0.1', CLI_PORT), Handler)
    print(f"Opening Spotify sign-in. If the browser doesn't open, visit:\n{url}")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    server.handle_request()
    server.server_close()
    if 'error' in result:
        raise SpotifyAuthError(f"Spotify sign-in failed: {result['error']}")
    finish_auth(pending, result.get('code'), result.get('state'))


# --- API calls --------------------------------------------------------------

def _request(method, path, **kwargs):
    """Call the Web API with retries for 429/5xx/network errors."""
    from bot import AuthExpired, TemporaryFailure, QuotaExceeded
    last = None
    for attempt in range(5):
        try:
            token = access_token()
        except SpotifyAuthError as e:
            raise AuthExpired(str(e)) from e
        try:
            r = requests.request(method, API + path, timeout=30,
                                 headers={'Authorization': f'Bearer {token}'}, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last = type(e).__name__
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 401:
            # token may have just expired; force a refresh once
            tok = _load_token() or {}
            if attempt == 0 and tok:
                tok['expires_at'] = 0
                with open(TOKEN_FILE, 'w') as f:
                    json.dump(tok, f)
                continue
            raise AuthExpired('Spotify sign-in no longer works; connect Spotify again.')
        if r.status_code == 429:
            if 'QUOTA_EXCEEDED' in r.text:
                raise QuotaExceeded("Spotify's daily API quota for your account is used up.")
            wait = int(r.headers.get('Retry-After', 5))
            if wait > 120:
                raise QuotaExceeded(f'Spotify asked to wait {wait}s before more requests.')
            print(f'    Spotify rate limit; waiting {wait}s...')
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            last = f'HTTP {r.status_code}'
            time.sleep(2 * (attempt + 1))
            continue
        return r
    raise TemporaryFailure(f'Spotify kept failing: {last}')


_JUNK = re.compile(
    r'\s*[\(\[][^\)\]]*(official|video|audio|lyric|visualizer|visualiser|remaster'
    r'|hd|hq|4k|explicit|music video|live|version)[^\)\]]*[\)\]]', re.I)


def clean_title(title, artist=''):
    """'Band - Song (Official Video) [4K]' -> 'Song'."""
    t = _JUNK.sub('', title or '')
    if artist and ' - ' in t:
        left, right = t.split(' - ', 1)
        if _norm(left) == _norm(artist):
            t = right
    t = re.sub(r'\s*\|.*$', '', t)               # 'Song | Live at X'
    t = re.sub(r'\s+(ft\.?|feat\.?)\s.*$', '', t, flags=re.I)
    return t.strip(' -') or (title or '')


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


class SpotifyWriter:
    """Playlist writer (same interface as bot.DataApiWriter) for Spotify."""
    name = 'Spotify'
    uses_quota = False
    dry_run = False
    platform = 'spotify'

    def __init__(self):
        if not is_connected():
            raise SpotifyAuthError('Not signed in to Spotify.')
        self._uris = {}  # playlist_id -> set of URIs added this session

    @staticmethod
    def playlist_url(playlist_id):
        return f'https://open.spotify.com/playlist/{playlist_id}'

    def create_playlist(self, name, description, privacy):
        body = {'name': name[:100], 'description': description[:300],
                'public': privacy == 'public'}
        r = _request('POST', '/me/playlists', json=body)
        if r.status_code in (404, 410):  # older API shape
            me = _json(r=_request('GET', '/me'), what='read your Spotify profile')
            r = _request('POST', f"/users/{me['id']}/playlists", json=body)
        if r.status_code == 403:
            raise SpotifyForbidden(_explain_403(r))
        if r.status_code not in (200, 201):
            raise RuntimeError(f'Spotify could not create the playlist '
                               f'(HTTP {r.status_code}): {r.text[:200]}')
        return _json(r, 'create the playlist')['id']

    def find_track(self, title, artist):
        """Best Spotify URI for title/artist, or None."""
        item = self.match_track(title, artist)
        return item['uri'] if item else None

    def match_track(self, title, artist):
        """Best Spotify track dict for title/artist, or None."""
        want_title = clean_title(title, artist)
        queries = [f'track:"{want_title}" artist:"{artist}"', f'{artist} {want_title}']
        for q in queries:
            r = _request('GET', '/search', params={'q': q, 'type': 'track', 'limit': 10})
            if r.status_code != 200:
                continue
            items = (r.json().get('tracks') or {}).get('items') or []
            for it in items:
                artists = [_norm(a.get('name')) for a in it.get('artists', [])]
                if _norm(artist) not in artists:
                    continue
                st, wt = _norm(it.get('name')), _norm(want_title)
                if st == wt or st.startswith(wt) or wt.startswith(st):
                    return it
        return None

    def add_video(self, playlist_id, video_id, band=None):
        from bot import AddFailed, track_title
        if str(video_id).startswith('spotify:track:'):
            uri = video_id  # picked with Spotify data: no matching needed
            return self._add_uri(playlist_id, uri)
        title = track_title(video_id)
        if not title:
            raise AddFailed('could not read the track title')
        uri = self.find_track(title, band or '')
        if not uri:
            raise AddFailed(f'no Spotify match for "{clean_title(title, band)}"')
        self._add_uri(playlist_id, uri)

    def _add_uri(self, playlist_id, uri):
        from bot import AddFailed
        seen = self._uris.setdefault(playlist_id, set())
        if uri in seen:
            raise AddFailed('same Spotify track already added')
        r = _request('POST', f'/playlists/{playlist_id}/items', json={'uris': [uri]})
        if r.status_code in (404, 410):
            r = _request('POST', f'/playlists/{playlist_id}/tracks', json={'uris': [uri]})
        if r.status_code == 403:
            raise SpotifyForbidden(_explain_403(r))
        if r.status_code not in (200, 201):
            raise AddFailed(f'Spotify refused the track: {r.text[:150]}')
        seen.add(uri)


# --- Spotify-native track modes ---------------------------------------------
# Used when "Pick songs using: Spotify" is chosen. Built only on /search (the
# artist top-tracks/albums endpoints are gone for Development Mode apps since
# February 2026). Search results come back roughly by popularity; when the
# `popularity` field is present it is used to sort. Each function returns
# Spotify track URIs, which SpotifyWriter adds directly (no matching step).

def _remember(items):
    from bot import _track_titles
    for it in items:
        _track_titles.setdefault(it['uri'], it.get('name', ''))


def _search(q, type_='track', pages=3):
    out = []
    for page in range(pages):
        r = _request('GET', '/search', params={'q': q, 'type': type_, 'limit': 10,
                                                'offset': page * 10})
        if r.status_code != 200:
            break
        items = (r.json().get(type_ + 's') or {}).get('items') or []
        out.extend(i for i in items if i)
        if len(items) < 10:
            break
    return out


def artist_tracks(band, extra='', pages=3):
    """The band's own tracks, most popular first, one version per song."""
    want = _norm(band)
    items = _search(f'artist:"{band}" {extra}'.strip(), pages=pages)
    items = [i for i in items
             if want in [_norm(a.get('name')) for a in i.get('artists', [])]]
    if any('popularity' in i for i in items):
        items.sort(key=lambda i: -(i.get('popularity') or 0))
    seen, unique = set(), []
    for i in items:
        key = _norm(clean_title(i.get('name', ''), band))
        if key and key not in seen:
            seen.add(key)
            unique.append(i)
    _remember(unique)
    return unique


def _year(item):
    try:
        return int((item.get('album') or {}).get('release_date', '')[:4])
    except ValueError:
        return 0


def sp_top(band, count=3):
    print(f"  Spotify: top tracks for {band}")
    return [i['uri'] for i in artist_tracks(band)[:count]]


def sp_one(band):
    return sp_top(band, 1)


def sp_deep(band, count=3):
    print(f"  Spotify: deep cuts for {band}")
    tracks = artist_tracks(band, pages=5)
    for skip in (10, 5, 3):
        deep = tracks[skip:skip + count]
        if deep:
            return [i['uri'] for i in deep]
    return []


def sp_latest(band, count=3):
    print(f"  Spotify: latest releases for {band}")
    tracks = artist_tracks(band, pages=5)
    tracks.sort(key=lambda i: (i.get('album') or {}).get('release_date', ''), reverse=True)
    return [i['uri'] for i in tracks[:count]]


def sp_era(band, count=3, start_year=None, end_year=None):
    print(f"  Spotify: {start_year}-{end_year} tracks for {band}")
    extra = f'year:{start_year}-{end_year}' if start_year and end_year else ''
    tracks = artist_tracks(band, extra, pages=3)
    if start_year and end_year:
        tracks = [i for i in tracks if start_year <= _year(i) <= end_year]
    return [i['uri'] for i in tracks[:count]]


def sp_best_of_albums(band, max_albums=5):
    """Most popular track from each original studio album."""
    from bot import is_original_album, get_album_base_name
    print(f"  Spotify: best of albums for {band}")
    picked, albums = [], set()
    for i in artist_tracks(band, pages=5):
        album = i.get('album') or {}
        if album.get('album_type') != 'album' or not is_original_album(album.get('name', '')):
            continue
        base = get_album_base_name(album.get('name', ''))
        if base in albums:
            continue
        albums.add(base)
        picked.append(i['uri'])
        if len(picked) >= max_albums:
            break
    return picked


def sp_setlist(band, count=3):
    from bot import setlist_song_names
    names = setlist_song_names(band, count)
    if not names:
        return sp_top(band, count)
    want, uris = _norm(band), []
    for name in names:
        for i in _search(f'track:"{name}" artist:"{band}"', pages=1):
            if want in [_norm(a.get('name')) for a in i.get('artists', [])]:
                _remember([i])
                uris.append(i['uri'])
                break
    return uris or sp_top(band, count)


SPOTIFY_MODES = {
    '1': sp_top, '2': sp_top, '3': sp_best_of_albums, '4': sp_deep,
    '5': sp_latest, '6': sp_one, '7': sp_era, '8': sp_setlist,
}
