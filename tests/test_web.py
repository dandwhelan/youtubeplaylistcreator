"""Flask endpoints (no real sign-ins)."""
import time

import pytest

import app as webapp
import bot


@pytest.fixture
def client():
    return webapp.app.test_client()


def wait_done(client):
    for _ in range(100):
        job = client.get('/api/job').get_json()
        if job['state'] != 'running':
            return job
        time.sleep(0.05)
    raise AssertionError('job did not finish')


def test_index_and_status(client):
    assert client.get('/').status_code == 200
    s = client.get('/api/status').get_json()
    assert {'quota', 'browser_auth', 'spotify_connected'} <= set(s)


def test_run_needs_auth(client):
    r = client.post('/api/run', json={'bands': ['A'], 'mode': '1'})
    assert r.status_code == 401
    r = client.post('/api/run', json={'bands': ['A'], 'mode': '1', 'platform': 'spotify'})
    assert r.status_code == 401 and 'Spotify' in r.get_json()['error']


def test_dry_run_job(client, fake_modes):
    r = client.post('/api/run', json={'bands': ['A', 'B'], 'mode': '1', 'count': 1,
                                      'dry_run': True})
    assert r.get_json() == {'started': True}
    job = wait_done(client)
    assert job['state'] == 'done'
    assert job['playlists'] == []
    assert any(line.startswith('Would add') for line in job['log'])


def test_browser_auth_endpoint_validates(client):
    r = client.post('/api/browser_auth', json={'headers': 'accept: */*'})
    assert r.status_code == 400


def test_spotify_start_needs_client_id(client, monkeypatch):
    assert client.post('/api/spotify/start').status_code == 400
    monkeypatch.setenv('SPOTIFY_CLIENT_ID', 'cid')
    url = client.post('/api/spotify/start').get_json()['auth_url']
    assert 'redirect_uri=http%3A%2F%2F127.0.0.1' in url


def test_settings_source_only_for_spotify():
    assert webapp._parse_settings('1', {'platform': 'spotify', 'count': 2}) == \
        ({'count': 2, 'source': 'spotify'}, None)
    assert webapp._parse_settings('1', {'platform': 'both', 'count': 2}) == ({'count': 2}, None)
