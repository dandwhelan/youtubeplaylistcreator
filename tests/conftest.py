"""Shared fixtures. Every test runs offline in its own temp directory, so
progress.json / quota.json / tokens never touch the real project folder."""
import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot  # noqa: E402
import spotify_sync  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot, 'RETRY_DELAYS', (0, 0))
    for var in ('YOUTUBE_DAILY_QUOTA', 'SPOTIFY_CLIENT_ID', 'SETLIST_FM_API_KEY'):
        monkeypatch.delenv(var, raising=False)
    bot._track_titles.clear()
    yield tmp_path


@pytest.fixture
def fake_modes(monkeypatch):
    """Every band yields candidates '<band>-0' ... '<band>-8' with titles."""
    def call(choice, settings, band):
        ids = [f"{band}-{i}" for i in range(9)]
        for i, v in enumerate(ids):
            bot._track_titles[v] = f"{band} - Song {i} (Official Video)"
        return ids
    monkeypatch.setattr(bot, 'call_track_mode', call)


def make_state(bands, count=2, platform='youtube', source=None, choice='1'):
    settings = {'count': count}
    if source:
        settings['source'] = source
    return {'platform': platform, 'choice': choice, 'settings': settings,
            'privacy': 'private', 'job_index': 0, 'log_entries': [],
            'jobs': [bot.new_job('Test', 'desc', list(bands), [])]}


class FakeWriter:
    """Records adds; optionally raises `exc` on the Nth add."""
    name = 'fake'
    uses_quota = False
    dry_run = False
    platform = 'youtube'
    playlist_url = staticmethod(lambda pid: f"https://example/{pid}")

    def __init__(self, fail_at=None, exc=None, reject=()):
        self.n = 0
        self.fail_at, self.exc, self.reject = fail_at, exc, set(reject)
        self.added = []
        self.created = 0

    def create_playlist(self, name, description, privacy):
        self.created += 1
        return 'PL1'

    def add_video(self, playlist_id, video_id, band=None):
        self.n += 1
        if self.n == self.fail_at:
            raise self.exc
        if video_id in self.reject:
            raise bot.AddFailed('unavailable')
        self.added.append(video_id)


class Resp:
    def __init__(self, code, data=None, headers=None):
        self.status_code = code
        self._data = data or {}
        self.text = json.dumps(self._data)
        self.headers = headers or {}

    def json(self):
        return self._data


def sp_track(name, artist='Band', pop=50, date='2000-01-01', album=None, atype='album'):
    return {'name': name, 'uri': f'spotify:track:{name.replace(" ", "_")}', 'popularity': pop,
            'artists': [{'name': artist}],
            'album': {'name': album or f'{name} LP', 'album_type': atype, 'release_date': date}}


class FakeSpotify:
    """Minimal stand-in for api.spotify.com. Search matches catalog tracks
    whose title appears in a track:"..." query, or all of the artist's
    tracks for an artist-only query; honours year:A-B and offset."""

    def __init__(self, catalog):
        self.catalog = catalog
        self.calls = []
        self.throttle_once = False

    def __call__(self, method, url, **kw):
        path = url.replace(spotify_sync.API, '')
        self.calls.append((method, path, kw.get('params') or kw.get('json')))
        if self.throttle_once:
            self.throttle_once = False
            return Resp(429, headers={'Retry-After': '0'})
        if path == '/me/playlists':
            return Resp(201, {'id': 'SPL'})
        if path.endswith('/items'):
            return Resp(201, {'snapshot_id': 'x'})
        if path == '/search':
            q = kw['params']['q']
            items = self.catalog
            if 'track:"' in q:
                want = q.split('track:"')[1].split('"')[0].lower()
                items = [i for i in items if i['name'].lower() == want]
            if 'year:' in q:
                y1, y2 = map(int, q.split('year:')[1].split()[0].split('-'))
                items = [i for i in items if y1 <= int(i['album']['release_date'][:4]) <= y2]
            off = kw['params'].get('offset', 0)
            return Resp(200, {'tracks': {'items': items[off:off + 10]}})
        return Resp(404)


@pytest.fixture
def spotify(monkeypatch):
    """Signed-in Spotify with a fake API. Returns the FakeSpotify to fill."""
    with open(spotify_sync.TOKEN_FILE, 'w') as f:
        json.dump({'access_token': 'a', 'refresh_token': 'r',
                   'expires_at': time.time() + 3600}, f)
    fake = FakeSpotify([])
    monkeypatch.setattr(spotify_sync.requests, 'request', fake)
    return fake
