"""Spotify Web API backend — parallel to the YouTube Music modes in bot.py.

Exposes the same function surface as the YT Music mode helpers so `bot.py`
can dispatch on the chosen platform without caring which API is underneath.

API surface (all take a `client` as the first arg):
    authenticate()                           -> client
    create_playlist(client, name, desc)      -> playlist_id
    add_track(client, playlist_id, track_id) -> None
    playlist_url(playlist_id)                -> str

    get_top_popular(client, band, count)
    get_most_played_ever(client, band, count)
    get_best_of_albums(client, band, max_albums)
    get_deep_cuts(client, band, count)
    get_latest_releases(client, band, count)
    get_one_hit_sampler(client, band)
    get_era_tracks(client, band, count, start_year, end_year)
    get_setlist_tracks(client, band, count)
"""
import os

from setlist_fm import get_most_played_live


SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID', '')
SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET', '')
SPOTIFY_REDIRECT_URI = os.environ.get(
    'SPOTIFY_REDIRECT_URI', 'http://127.0.0.1:8765/callback'
)
SPOTIFY_CACHE_PATH = '.spotify_cache'
SPOTIFY_SCOPES = 'playlist-modify-public playlist-modify-private'


# =============================================================================
# AUTH & PLAYLIST OPERATIONS
# =============================================================================

def authenticate():
    """Interactive OAuth flow; returns an authenticated Spotipy client."""
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
    except ImportError as e:
        raise RuntimeError(
            "The 'spotipy' package is not installed. Run: "
            "pip install spotipy  (or: pip install -r requirements.txt)"
        ) from e

    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise RuntimeError(
            "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set. "
            "Create a free app at https://developer.spotify.com/dashboard, "
            "add 'http://127.0.0.1:8765/callback' to its Redirect URIs, "
            "then set the env vars and re-run."
        )

    auth_manager = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPES,
        cache_path=SPOTIFY_CACHE_PATH,
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def create_playlist(client, name, description, public=True):
    user_id = client.current_user()['id']
    result = client.user_playlist_create(
        user=user_id,
        name=name,
        public=public,
        description=description,
    )
    return result['id']


def add_track(client, playlist_id, track_id):
    client.playlist_add_items(playlist_id, [f'spotify:track:{track_id}'])


def playlist_url(playlist_id):
    return f'https://open.spotify.com/playlist/{playlist_id}'


# =============================================================================
# SEARCH HELPERS
# =============================================================================

def _find_artist_id(client, band_name):
    """Return the Spotify artist ID for the best search match, or None."""
    result = client.search(q=band_name, type='artist', limit=1)
    items = result.get('artists', {}).get('items', [])
    return items[0]['id'] if items else None


def _fetch_full_tracks(client, track_ids):
    """Spotify's /tracks endpoint takes up to 50 IDs per call and returns full
    track objects (including popularity, which album_tracks doesn't give us)."""
    full = []
    for i in range(0, len(track_ids), 50):
        batch = client.tracks(track_ids[i:i + 50])['tracks']
        full.extend(t for t in batch if t)
    return full


def _dedupe_albums(albums):
    """Spotify often returns regional variants of the same album (different
    markets, explicit/clean, deluxe re-releases). Dedupe by lowercased name."""
    seen = set()
    unique = []
    for album in albums:
        key = album['name'].lower().strip()
        if key in seen:
            continue
        seen.add(key)
        unique.append(album)
    return unique


# =============================================================================
# MODE FUNCTIONS
# =============================================================================

def get_top_popular(client, band_name, count=3):
    """Mode 1: Spotify's artist top-tracks is already popularity-ranked."""
    print(f"  Searching top popular tracks: {band_name}")
    artist_id = _find_artist_id(client, band_name)
    if not artist_id:
        return []
    tracks = client.artist_top_tracks(artist_id)['tracks']
    return [t['id'] for t in tracks[:count] if t.get('id')]


def get_most_played_ever(client, band_name, count=3):
    """Mode 2: same data source as Mode 1 on Spotify — top-tracks is already
    sorted by all-time popularity score. Aliased for menu consistency."""
    print(f"  Searching most played ever: {band_name}")
    return get_top_popular(client, band_name, count)


def get_best_of_albums(client, band_name, max_albums=5):
    """Mode 3: Most popular track from each unique album."""
    print(f"  Searching best album tracks: {band_name} (max {max_albums} albums)")
    artist_id = _find_artist_id(client, band_name)
    if not artist_id:
        return []

    albums = client.artist_albums(
        artist_id, album_type='album', limit=50
    )['items']
    unique_albums = _dedupe_albums(albums)[:max_albums]

    top_tracks = []
    for album in unique_albums:
        try:
            album_tracks = client.album_tracks(album['id'])['items']
            ids = [t['id'] for t in album_tracks if t.get('id')]
            if not ids:
                continue
            full = _fetch_full_tracks(client, ids)
            if not full:
                continue
            full.sort(key=lambda t: t.get('popularity', 0), reverse=True)
            top_tracks.append(full[0]['id'])
            print(f"    Album '{album['name']}': found top track")
        except Exception as e:
            print(f"    Error on album '{album.get('name', '?')}': {e}")
    return top_tracks


def get_deep_cuts(client, band_name, count=3):
    """Mode 4: rank all album tracks by popularity descending, then skip the
    top 5 to surface genuinely deeper cuts instead of just slightly-less hits."""
    print(f"  Searching deep cuts: {band_name}")
    artist_id = _find_artist_id(client, band_name)
    if not artist_id:
        return []

    albums = client.artist_albums(
        artist_id, album_type='album', limit=20
    )['items']
    unique_albums = _dedupe_albums(albums)

    all_ids = []
    for album in unique_albums[:10]:
        try:
            items = client.album_tracks(album['id'])['items']
            all_ids.extend(t['id'] for t in items if t.get('id'))
        except Exception:
            continue

    if not all_ids:
        return []

    full = _fetch_full_tracks(client, all_ids)
    full.sort(key=lambda t: t.get('popularity', 0), reverse=True)

    deep = full[5:5 + count]
    if not deep:
        deep = full[3:3 + count]
    return [t['id'] for t in deep]


def get_latest_releases(client, band_name, count=3):
    """Mode 5: most recent singles/albums first; grab the lead track."""
    print(f"  Searching latest releases: {band_name}")
    artist_id = _find_artist_id(client, band_name)
    if not artist_id:
        return []

    releases = client.artist_albums(
        artist_id, album_type='single,album', limit=50
    )['items']
    releases.sort(key=lambda a: a.get('release_date', ''), reverse=True)

    track_ids = []
    for release in releases:
        if len(track_ids) >= count:
            break
        try:
            items = client.album_tracks(release['id'])['items']
            if items and items[0].get('id'):
                track_ids.append(items[0]['id'])
        except Exception:
            continue
    return track_ids


def get_one_hit_sampler(client, band_name):
    """Mode 6: just the single #1 track."""
    print(f"  Searching #1 hit: {band_name}")
    return get_top_popular(client, band_name, 1)


def get_era_tracks(client, band_name, count=3, start_year=None, end_year=None):
    """Mode 7: tracks from releases within [start_year, end_year], ranked by
    popularity so we surface the defining songs of that era."""
    print(f"  Searching {start_year}-{end_year} era tracks: {band_name}")
    artist_id = _find_artist_id(client, band_name)
    if not artist_id:
        return []

    releases = client.artist_albums(
        artist_id, album_type='single,album', limit=50
    )['items']
    era_releases = []
    for release in releases:
        date = release.get('release_date') or ''
        try:
            year = int(date[:4]) if date else 0
        except ValueError:
            year = 0
        if start_year is not None and end_year is not None:
            if start_year <= year <= end_year:
                era_releases.append(release)

    if not era_releases:
        print(f"    No releases found in {start_year}-{end_year}")
        return []

    all_ids = []
    for release in era_releases[:10]:
        try:
            items = client.album_tracks(release['id'])['items']
            all_ids.extend(t['id'] for t in items if t.get('id'))
        except Exception:
            continue

    if not all_ids:
        return []

    full = _fetch_full_tracks(client, all_ids)
    full.sort(key=lambda t: t.get('popularity', 0), reverse=True)
    return [t['id'] for t in full[:count]]


def _search_track(client, band_name, song_name):
    """Find a specific song by a specific artist on Spotify. Falls back to a
    looser query if the strict artist:/track: form returns nothing."""
    strict = f'artist:"{band_name}" track:"{song_name}"'
    result = client.search(q=strict, type='track', limit=1)
    items = result.get('tracks', {}).get('items', [])
    if not items:
        loose = f'{band_name} {song_name}'
        result = client.search(q=loose, type='track', limit=1)
        items = result.get('tracks', {}).get('items', [])
    return items[0]['id'] if items else None


def get_setlist_tracks(client, band_name, count=3):
    """Mode 8: most commonly played live songs (via setlist.fm), searched on
    Spotify. Falls back to Mode 1 if setlist.fm is unconfigured or has no data."""
    print(f"  Searching setlist data: {band_name}")

    song_names = get_most_played_live(band_name, count)
    if not song_names:
        print("    setlist.fm unavailable or no data — falling back to top popular")
        return get_top_popular(client, band_name, count)

    track_ids = []
    for song_name in song_names:
        try:
            track_id = _search_track(client, band_name, song_name)
            if track_id:
                track_ids.append(track_id)
        except Exception:
            continue

    return track_ids if track_ids else get_top_popular(client, band_name, count)
