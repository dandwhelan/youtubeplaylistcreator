"""Spotify matching, Spotify-data modes, both-platform saving and the
Spotify-aware dry run."""
import pytest

import bot
import spotify_sync as sp
from conftest import FakeWriter, make_state, sp_track


@pytest.mark.parametrize('raw,artist,clean', [
    ('Metallica - Enter Sandman (Official Music Video)', 'Metallica', 'Enter Sandman'),
    ('Duality [OFFICIAL VIDEO]', 'Slipknot', 'Duality'),
    ('Chop Suey! (Remastered 2021) | 4K', 'System of a Down', 'Chop Suey!'),
    ('Song ft. Someone', 'X', 'Song'),
    ('Plain', 'X', 'Plain'),
])
def test_clean_title(raw, artist, clean):
    assert sp.clean_title(raw, artist) == clean


def test_match_requires_same_artist(spotify):
    spotify.catalog = [sp_track('Song 1', artist='Impostor'), sp_track('Song 1', artist='Band')]
    w = sp.SpotifyWriter()
    assert w.find_track('Band - Song 1 (Official Video)', 'Band') == 'spotify:track:Song_1'
    assert w.find_track('Unknown', 'Band') is None


def test_youtube_data_run_to_spotify_skips_unmatched(fake_modes, spotify):
    spotify.catalog = [sp_track('Song 1'), sp_track('Song 2')]  # no 'Song 0'
    spotify.throttle_once = True  # 429 is retried
    w = sp.SpotifyWriter()
    assert bot.run_jobs(w, make_state(['Band'], count=2, platform='spotify')) == 'done'
    adds = [c[2]['uris'] for c in spotify.calls if c[1].endswith('/items')]
    assert adds == [['spotify:track:Song_1'], ['spotify:track:Song_2']]


def test_spotify_auth_expiry_pauses_run(fake_modes, spotify, monkeypatch):
    import json
    with open(sp.TOKEN_FILE, 'w') as f:
        json.dump({'access_token': 'a', 'refresh_token': 'r', 'expires_at': 0}, f)
    from conftest import Resp
    monkeypatch.setattr(sp.requests, 'post', lambda *a, **k: Resp(400))
    st = make_state(['Band'], platform='spotify')
    st['jobs'][0]['playlist_id'] = 'SPL'
    assert bot.run_jobs(sp.SpotifyWriter(), st) == 'auth'


CATALOG = [
    sp_track('Hit', pop=90, date='2001-01-01', album='First'),
    sp_track('Hit (Live)', pop=60, date='2010-01-01', album='Live at X'),
    sp_track('Mid', pop=70, date='2005-05-05', album='Second'),
    sp_track('Old', pop=50, date='1995-01-01', album='First'),
    sp_track('New', pop=40, date='2024-03-01', atype='single'),
    sp_track('Other', artist='Someone Else', pop=99),
    sp_track('Deluxe Cut', pop=65, date='2006-01-01', album='Second (Deluxe Edition)'),
]
U = 'spotify:track:'


def test_spotify_modes(spotify):
    spotify.catalog = CATALOG
    assert sp.sp_top('Band', 3) == [U + 'Hit', U + 'Mid', U + 'Deluxe_Cut']
    assert sp.sp_one('Band') == [U + 'Hit']
    assert sp.sp_latest('Band', 2) == [U + 'New', U + 'Deluxe_Cut']
    assert sp.sp_era('Band', 5, 1990, 2002) == [U + 'Hit', U + 'Old']
    assert sp.sp_best_of_albums('Band', 5) == [U + 'Hit', U + 'Mid']
    assert sp.sp_deep('Band', 2) == [U + 'Old', U + 'New']


def test_spotify_source_run_adds_uris_directly(spotify):
    spotify.catalog = CATALOG
    st = make_state(['Band'], count=2, platform='spotify', source='spotify')
    assert bot.run_jobs(sp.SpotifyWriter(), st) == 'done'
    adds = [c[2]['uris'][0] for c in spotify.calls if c[1].endswith('/items')]
    assert adds == [U + 'Hit', U + 'Mid']
    assert not any(c[2] and 'track:"' in str(c[2].get('q', '')) for c in spotify.calls
                   if c[1] == '/search')  # no per-track matching searches


def test_both_platforms(fake_modes, spotify):
    spotify.catalog = [sp_track('Song 1')]  # Spotify only has Song 1
    yt = FakeWriter()
    w = bot.MultiWriter([sp.SpotifyWriter(), yt])
    st = make_state(['Band'], count=2, platform='both')
    assert bot.run_jobs(w, st) == 'done'
    assert st['jobs'][0]['playlist_id'] == 'SPL|PL1'
    assert yt.added == ['Band-0', 'Band-1']  # YouTube gets both
    adds = [c[2]['uris'] for c in spotify.calls if c[1].endswith('/items')]
    assert adds == [[U + 'Song_1']]
    assert any('Only partly added Band-0' in e for e in st['log_entries'])
    assert [p for p, _ in bot.playlist_links(w, 'SPL|PL1')] == ['spotify', 'youtube']


def test_both_platforms_track_rejected_everywhere_is_replaced(fake_modes, spotify):
    spotify.catalog = [sp_track('Song 1'), sp_track('Song 2')]
    yt = FakeWriter(reject={'Band-0'})
    w = bot.MultiWriter([sp.SpotifyWriter(), yt])
    bot.run_jobs(w, make_state(['Band'], count=2, platform='both'))
    assert yt.added == ['Band-1', 'Band-2']


def test_dry_run_spotify_check_replaces_misses(fake_modes, spotify):
    spotify.catalog = [sp_track('Song 1'), sp_track('Song 2')]
    w = bot.DryRunWriter(match_spotify=True, require_match=True)
    st = make_state(['Band'], count=2, platform='spotify')
    bot.run_jobs(w, st)
    assert w.tracks == ['Band-1', 'Band-2']
    assert len(w.misses) == 1
    assert not any(c[1].endswith('/items') or c[1] == '/me/playlists' for c in spotify.calls)
    assert any('1 song(s) had no Spotify match' in e for e in st['log_entries'])


def test_dry_run_both_keeps_youtube_only_songs(fake_modes, spotify):
    spotify.catalog = [sp_track('Song 1')]
    w = bot.DryRunWriter(match_spotify=True, require_match=False)
    bot.run_jobs(w, make_state(['Band'], count=2, platform='both'))
    assert w.tracks == ['Band-0', 'Band-1']
    assert len(w.misses) == 1


def test_pkce_auth_url(monkeypatch):
    monkeypatch.setenv('SPOTIFY_CLIENT_ID', 'cid')
    url, pending = sp.begin_auth('http://127.0.0.1:5000/spotify/callback')
    assert 'code_challenge_method=S256' in url and 'client_id=cid' in url
    with pytest.raises(sp.SpotifyAuthError):
        sp.finish_auth(pending, 'code', 'wrong-state')
