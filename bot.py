import os
import re
import glob
import json
import time
import requests
from collections import Counter
from ytmusicapi import YTMusic
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import get_key


def _find_client_secrets_file():
    """Find the OAuth client secrets file via glob so users can keep Google's
    default download name or rename it to `client_secret.json`."""
    matches = sorted(glob.glob('client_secret*.json'))
    return matches[0] if matches else 'client_secret.json'


# --- CONFIGURATION ---
YOUTUBE_CLIENT_SECRETS_FILE = _find_client_secrets_file()
DEFAULT_BANDS_FILE = 'bands.txt'
LOG_FILE = 'playlist_log.txt'
TOKEN_FILE = 'token.json'
PROGRESS_FILE = 'progress.json'
PLAYLIST_PRIVACY = 'private'  # default offered at the visibility prompt

# Setlist Mode needs a free key from https://api.setlist.fm — set the
# SETLIST_FM_API_KEY env var, or save it via the web UI's "API keys" panel
# (stored in settings.json; env var wins if both are set). Looked up at call
# time via config.get_key() so keys saved while the app is running apply
# immediately.

# --- API SETUP ---
SCOPES = ['https://www.googleapis.com/auth/youtube']
ytmusic = YTMusic()

# Keywords that indicate an album is a reissue/variant, not an original release
ALBUM_VARIANT_KEYWORDS = [
    'deluxe', 'bonus track', 'remix', 'remixed', 'live at', 'live in',
    'live from', 'acoustic version', 'unplugged', 'expanded',
    'special edition', 'compilation', 'greatest hits', 'best of',
    'collection', 'sessions', 'demo', 'stripped', 'instrumentals',
    'commentary', 'tour edition', 'super deluxe', 'bonus edition',
    'collector', 'platinum', 'diamond',
]

# Broad genre groups for clustering
GENRE_GROUPS = {
    'Metal': [
        'metal', 'thrash metal', 'death metal', 'black metal', 'doom metal',
        'heavy metal', 'metalcore', 'deathcore', 'grindcore', 'progressive metal',
        'nu metal', 'power metal', 'symphonic metal', 'groove metal', 'sludge metal',
        'speed metal', 'folk metal', 'gothic metal', 'industrial metal', 'djent',
        'mathcore', 'avant-garde metal', 'stoner metal', 'crossover thrash',
        'melodic death metal', 'technical death metal', 'post-metal',
    ],
    'Rock': [
        'rock', 'alternative rock', 'indie rock', 'hard rock', 'classic rock',
        'garage rock', 'stoner rock', 'psychedelic rock', 'blues rock', 'grunge',
        'progressive rock', 'post-rock', 'space rock', 'art rock', 'southern rock',
        'britpop', 'shoegaze', 'noise rock', 'math rock', 'j-rock',
    ],
    'Punk / Hardcore': [
        'punk', 'punk rock', 'pop punk', 'hardcore punk', 'post-punk',
        'skate punk', 'hardcore', 'post-hardcore', 'emo', 'screamo',
        'crust punk', 'anarcho-punk', 'street punk', 'melodic hardcore',
        'beatdown hardcore', 'straight edge', 'easycore',
    ],
    'Electronic': [
        'electronic', 'edm', 'industrial', 'drum and bass', 'dubstep',
        'techno', 'synth', 'electro', 'dance', 'trance', 'breakbeat',
        'electronicore', 'industrial rock', 'ebm', 'happy hardcore',
    ],
    'Pop / Other': [
        'pop', 'pop rock', 'power pop', 'synth-pop', 'j-pop', 'k-pop',
        'rap', 'hip hop', 'rap rock', 'rap metal', 'r&b', 'country',
        'folk', 'singer-songwriter', 'reggae', 'ska', 'funk', 'soul',
    ],
}


# =============================================================================
# YOUTUBE AUTH & PLAYLIST API
# =============================================================================

def get_youtube_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                # A Google project left in "Testing" mode issues refresh
                # tokens that die after 7 days, so a saved token from an
                # earlier festival is normally dead. Sign in again.
                print("Saved sign-in has expired - opening the browser to sign in again.")
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(YOUTUBE_CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())

    return build('youtube', 'v3', credentials=creds)


class Cancelled(Exception):
    """A progress callback asked the run to stop (web UI cancel button)."""


class QuotaExceeded(Exception):
    """The YouTube Data API daily quota is used up; nothing more can be
    added until it resets."""


QUOTA_REASONS = {'quotaExceeded', 'dailyLimitExceeded'}


def _http_error_reason(err):
    """Pull the machine-readable reason ('quotaExceeded', 'videoNotFound',
    ...) out of a googleapiclient HttpError."""
    try:
        payload = json.loads(err.content.decode('utf-8'))
        errors = payload.get('error', {}).get('errors', [])
        if errors:
            return errors[0].get('reason', '')
    except Exception:
        pass
    return ''


def _execute(request):
    """Run a YouTube Data API request, turning a quota failure into
    QuotaExceeded so the run stops instead of skipping every later band."""
    try:
        return request.execute()
    except HttpError as e:
        if _http_error_reason(e) in QUOTA_REASONS:
            raise QuotaExceeded(str(e)) from e
        raise


def create_playlist(youtube, name, description, privacy=PLAYLIST_PRIVACY):
    request = youtube.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {"title": name, "description": description},
            "status": {"privacyStatus": privacy}
        }
    )
    return _execute(request)['id']


def add_video_to_playlist(youtube, playlist_id, video_id):
    _execute(youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id}
            }
        }
    ))


# =============================================================================
# HELPERS
# =============================================================================

def _norm_name(name):
    """Fold a band name for comparison: case, punctuation and a leading
    'The' do not count ('P.O.D.' == 'POD', 'The Primals' == 'Primals')."""
    raw = (name or '').lower()
    folded = re.sub(r'[^a-z0-9]+', '', raw)
    if not folded:
        return raw.strip()  # non-Latin name: compare as written
    if folded.startswith('the') and len(folded) > 3:
        folded = folded[3:]
    return folded


_artist_cache = {}
match_warnings = []  # bands whose YouTube Music match is not an exact name match


def get_artist(band_name):
    """Find the artist on YouTube Music and return its artist page, or None.

    Prefers a search result whose name is the band's name. With no exact
    match the first result is still used, but it is recorded in
    `match_warnings` so the user can check that band by hand - small bands
    with common names ('Nasty', 'Drain') easily resolve to someone else."""
    if band_name in _artist_cache:
        return _artist_cache[band_name]

    results = ytmusic.search(query=band_name, filter="artists", limit=5)
    artist = None
    if results:
        want = _norm_name(band_name)
        pick = next((r for r in results if _norm_name(r.get('artist')) == want), None)
        if pick is None:
            pick = results[0]
            msg = f"'{band_name}' matched '{pick.get('artist', '?')}' (no exact name match)"
            match_warnings.append(msg)
            print(f"    Check: {msg}")
        artist = ytmusic.get_artist(pick['browseId'])

    _artist_cache[band_name] = artist
    return artist


def get_artist_songs(artist, limit=20):
    """Return up to `limit` of the artist's songs, most popular first.

    The artist page itself only carries a handful of top songs; the full
    list sits behind the 'songs' browseId as a playlist."""
    songs = artist.get('songs') or {}
    results = [s for s in songs.get('results') or [] if s.get('videoId')]
    browse_id = songs.get('browseId')
    if browse_id and len(results) < limit:
        try:
            full = ytmusic.get_playlist(browse_id, limit=limit).get('tracks') or []
            full = [s for s in full if s.get('videoId')]
            if len(full) > len(results):
                results = full
        except Exception as e:
            print(f"    Could not load the full song list ({e}); using the top songs only")
    return results[:limit]


_view_count_cache = {}
_view_failures = 0


def get_view_count(video_id):
    """Get the view count for a video via YouTube Music. Cached per video_id
    to avoid redundant lookups when the same track is ranked in multiple modes.
    A failed lookup is retried once, counted, and not cached."""
    global _view_failures
    if video_id in _view_count_cache:
        return _view_count_cache[video_id]
    for attempt in range(2):
        try:
            details = ytmusic.get_song(video_id)
            views = int(details.get('videoDetails', {}).get('viewCount', 0) or 0)
            _view_count_cache[video_id] = views
            return views
        except Exception:
            if attempt == 0:
                time.sleep(2)
    _view_failures += 1
    return 0


def is_original_album(title):
    """Check if an album title indicates an original release (not a variant)."""
    title_lower = title.lower()
    for keyword in ALBUM_VARIANT_KEYWORDS:
        if keyword in title_lower:
            return False
    return True


def get_album_base_name(title):
    """Extract the base album name, stripping variant suffixes."""
    cleaned = re.sub(
        r'\s*[\(\[].*?(deluxe|bonus|remaster|expanded|edition|anniversary|'
        r'version|special|collector|super|explicit|clean).*?[\)\]]\s*$',
        '', title, flags=re.IGNORECASE
    ).strip()
    cleaned = re.sub(
        r'\s*[-\u2013\u2014]\s*(remaster|deluxe|expanded|bonus|special).*$',
        '', cleaned, flags=re.IGNORECASE
    ).strip()
    return cleaned.lower()


def filter_unique_albums(albums):
    """Filter to unique original albums, removing variants and duplicates."""
    originals = [a for a in albums if is_original_album(a['title'])]
    variants = [a for a in albums if not is_original_album(a['title'])]

    seen = set()
    unique = []

    for album in originals:
        base = get_album_base_name(album['title'])
        if base not in seen:
            seen.add(base)
            unique.append(album)

    for album in variants:
        base = get_album_base_name(album['title'])
        if base not in seen:
            seen.add(base)
            unique.append(album)

    return unique


# =============================================================================
# DUPLICATE DETECTION
# =============================================================================

# =============================================================================
# RESUME / PROGRESS SUPPORT
# =============================================================================

def save_progress(data):
    """Save current progress so the run can be resumed later."""
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def load_progress():
    """Load saved progress, or return None if no usable save file exists.
    A file written by the older single-playlist format is converted."""
    if not os.path.exists(PROGRESS_FILE):
        return None
    try:
        with open(PROGRESS_FILE, 'r') as f:
            data = json.load(f)
        if 'jobs' in data:
            data['jobs'][0]['bands']  # shape check
            return data
        return {
            'choice': data['choice'],
            'settings': data['settings'],
            'privacy': PLAYLIST_PRIVACY,
            'job_index': 0,
            'log_entries': data.get('log_entries', []),
            'jobs': [{
                'name': data['settings'].get('playlist_name', 'playlist'),
                'description': '',
                'header': [],
                'playlist_id': data['playlist_id'],
                'bands': data['bands'],
                'band_index': data.get('band_index', 0),
                'partial_ids': [],
                'added_video_ids': data.get('added_video_ids', []),
            }],
        }
    except (json.JSONDecodeError, IOError, KeyError, IndexError, TypeError):
        return None


def clear_progress():
    """Remove the progress file after a successful run."""
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)


# =============================================================================
# PLAYLIST MODES
# =============================================================================

def get_top_popular(band_name, count=3):
    """Mode 1: Top trending/popular songs right now."""
    print(f"  Searching top popular tracks: {band_name}")
    artist = get_artist(band_name)
    if not artist:
        return []
    return [song['videoId'] for song in get_artist_songs(artist, count)]


def get_most_played_ever(band_name, count=3):
    """Mode 2: Most played/viewed songs of all time (sorted by view count)."""
    print(f"  Searching most played ever: {band_name}")
    artist = get_artist(band_name)
    if not artist:
        return []
    songs_with_views = []
    for song in get_artist_songs(artist, max(count, 20)):
        views = get_view_count(song['videoId'])
        songs_with_views.append((song['videoId'], views))

    songs_with_views.sort(key=lambda x: x[1], reverse=True)
    return [vid for vid, _ in songs_with_views[:count]]


def get_best_of_albums(band_name, max_albums=5):
    """Mode 3: Most played song from each album (unique albums only)."""
    print(f"  Searching best album tracks: {band_name} (max {max_albums} albums)")
    artist = get_artist(band_name)
    if not artist:
        return []
    if 'albums' not in artist or 'results' not in artist['albums']:
        return []

    albums = artist['albums']['results']
    unique_albums = filter_unique_albums(albums)[:max_albums]

    top_tracks = []
    for album in unique_albums:
        try:
            album_details = ytmusic.get_album(album['browseId'])
            tracks = album_details.get('tracks', [])
            if not tracks:
                continue

            best_track = None
            best_views = -1
            for track in tracks:
                if 'videoId' not in track or not track['videoId']:
                    continue
                views = get_view_count(track['videoId'])
                if views > best_views:
                    best_views = views
                    best_track = track['videoId']

            if best_track:
                top_tracks.append(best_track)
                print(f"    Album '{album['title']}': found top track")
        except Exception as e:
            print(f"    Error on album '{album.get('title', '?')}': {e}")

    return top_tracks


def get_deep_cuts(band_name, count=3):
    """Mode 4: Hidden gems - skip the obvious top hits."""
    print(f"  Searching deep cuts: {band_name}")
    artist = get_artist(band_name)
    if not artist:
        return []
    # Skip the ten biggest songs; for artists with a short list, skip fewer.
    songs = get_artist_songs(artist, 10 + count)
    for skip in (10, 5, 3):
        deep = [song['videoId'] for song in songs[skip:skip + count]]
        if deep:
            return deep
    return []


def get_latest_releases(band_name, count=3):
    """Mode 5: Most recent singles and songs."""
    print(f"  Searching latest releases: {band_name}")
    artist = get_artist(band_name)
    if not artist:
        return []

    if 'singles' in artist and 'results' in artist['singles']:
        singles = artist['singles']['results'][:count]
        tracks = []
        for single in singles:
            try:
                album_details = ytmusic.get_album(single['browseId'])
                single_tracks = album_details.get('tracks', [])
                if single_tracks and 'videoId' in single_tracks[0]:
                    tracks.append(single_tracks[0]['videoId'])
            except Exception:
                continue
        if tracks:
            return tracks

    return [song['videoId'] for song in get_artist_songs(artist, count)]


def get_one_hit_sampler(band_name):
    """Mode 6: Just the single #1 song per artist."""
    print(f"  Searching #1 hit: {band_name}")
    artist = get_artist(band_name)
    if not artist:
        return []
    return [song['videoId'] for song in get_artist_songs(artist, 1)]


def get_era_tracks(band_name, count=3, start_year=None, end_year=None):
    """Mode 7: Songs from a specific era/year range."""
    print(f"  Searching {start_year}-{end_year} era tracks: {band_name}")
    artist = get_artist(band_name)
    if not artist:
        return []

    # Collect albums and singles that fall within the year range
    era_releases = []
    for section in ('albums', 'singles'):
        if section in artist and 'results' in artist[section]:
            for release in artist[section]['results']:
                try:
                    year = int(release.get('year', 0))
                    if start_year <= year <= end_year:
                        era_releases.append(release)
                except (ValueError, TypeError):
                    continue

    if not era_releases:
        print(f"    No releases found in {start_year}-{end_year}")
        return []

    # Get tracks from era releases and rank by views
    all_tracks = []
    for release in era_releases[:10]:
        try:
            details = ytmusic.get_album(release['browseId'])
            for track in details.get('tracks', []):
                if 'videoId' in track and track['videoId']:
                    views = get_view_count(track['videoId'])
                    all_tracks.append((track['videoId'], views))
        except Exception:
            continue

    all_tracks.sort(key=lambda x: x[1], reverse=True)
    return [vid for vid, _ in all_tracks[:count]]


_last_setlist_request = 0.0


def _setlist_get(url, headers, params):
    """GET against setlist.fm, enforcing a minimum 1s gap between calls so
    we stay under the public rate limit regardless of how many bands we iterate."""
    global _last_setlist_request
    elapsed = time.time() - _last_setlist_request
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    _last_setlist_request = time.time()
    return resp


def get_setlist_tracks(band_name, count=3):
    """Mode 8: Most commonly played live songs (via setlist.fm API)."""
    print(f"  Searching setlist data: {band_name}")

    api_key = get_key('SETLIST_FM_API_KEY')
    if not api_key:
        print("    SETLIST_FM_API_KEY not set - falling back to top popular")
        return get_top_popular(band_name, count)

    headers = {
        'Accept': 'application/json',
        'x-api-key': api_key,
    }

    # Search for the artist on setlist.fm
    try:
        resp = _setlist_get(
            'https://api.setlist.fm/rest/1.0/search/artists',
            headers=headers,
            params={'artistName': band_name, 'sort': 'relevance'},
        )
        if resp.status_code != 200:
            print(f"    setlist.fm search failed ({resp.status_code}), falling back")
            return get_top_popular(band_name, count)

        artists = resp.json().get('artist', [])
        if not artists:
            print(f"    Not found on setlist.fm, falling back")
            return get_top_popular(band_name, count)

        artist_mbid = artists[0]['mbid']
    except Exception as e:
        print(f"    setlist.fm error: {e}, falling back")
        return get_top_popular(band_name, count)

    # Fetch recent setlists
    try:
        resp = _setlist_get(
            f'https://api.setlist.fm/rest/1.0/artist/{artist_mbid}/setlists',
            headers=headers,
            params={'p': 1},
        )
        if resp.status_code != 200:
            return get_top_popular(band_name, count)

        setlists = resp.json().get('setlist', [])
    except Exception:
        return get_top_popular(band_name, count)

    # Count song occurrences across the last 10 shows
    song_counts = Counter()
    for setlist in setlists[:10]:
        for s in setlist.get('sets', {}).get('set', []):
            for song in s.get('song', []):
                # Walk-on tapes and covers are played every night but are
                # not the band's own songs.
                if song.get('tape') or song.get('cover'):
                    continue
                name = song.get('name', '')
                if name:
                    song_counts[name] += 1

    if not song_counts:
        return get_top_popular(band_name, count)

    # Find the most commonly played songs on YouTube Music
    top_song_names = [name for name, _ in song_counts.most_common(count)]
    video_ids = []
    want = _norm_name(band_name)
    for song_name in top_song_names:
        results = ytmusic.search(f"{band_name} {song_name}", filter="songs", limit=5)
        # Only take a result credited to this band, not another act's
        # song of the same name.
        pick = next(
            (r for r in results or [] if r.get('videoId') and any(
                _norm_name(a.get('name')) == want for a in r.get('artists') or [])),
            None)
        if pick:
            video_ids.append(pick['videoId'])

    return video_ids if video_ids else get_top_popular(band_name, count)


# =============================================================================
# GENRE CLUSTERING
# =============================================================================

def get_artist_genre(band_name):
    """Look up genre tags for an artist via MusicBrainz."""
    try:
        resp = requests.get(
            'https://musicbrainz.org/ws/2/artist/',
            params={'query': band_name, 'fmt': 'json', 'limit': 1},
            headers={'User-Agent': 'YouTubePlaylistCreator/1.0'},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        artists = resp.json().get('artists', [])
        if not artists:
            return []
        tags = artists[0].get('tags', [])
        tags.sort(key=lambda t: t.get('count', 0), reverse=True)
        return [t['name'].lower() for t in tags[:5]]
    except Exception:
        return []


def classify_genre(tags):
    """Map a list of MusicBrainz tags to one of the broad genre groups."""
    if not tags:
        return 'Other'

    scores = {group: 0 for group in GENRE_GROUPS}
    for tag in tags:
        for group, keywords in GENRE_GROUPS.items():
            for keyword in keywords:
                # Exact tag match counts most; otherwise the keyword must
                # appear as whole words ('metal' in 'melodic death metal'),
                # never as a fragment of another word.
                if tag == keyword:
                    scores[group] += 3
                elif re.search(r'(?<![a-z])' + re.escape(keyword) + r'(?![a-z])', tag):
                    scores[group] += 1

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else 'Other'


def build_genre_clusters(bands):
    """Group bands by genre using MusicBrainz lookups."""
    print("\nLooking up genres (this takes a couple of minutes)...")
    clusters = {}
    for i, band in enumerate(bands, 1):
        print(f"  [{i}/{len(bands)}] {band}...", end=" ", flush=True)
        tags = get_artist_genre(band)
        genre = classify_genre(tags)
        print(genre)
        clusters.setdefault(genre, []).append(band)
        time.sleep(1)  # MusicBrainz rate limit: 1 req/sec

    return clusters


# =============================================================================
# MODES REGISTRY
# =============================================================================

TRACK_MODES = {
    '1': {
        'name': 'Top Popular Now',
        'description': 'Top trending songs per artist (current popularity)',
        'func': get_top_popular,
    },
    '2': {
        'name': 'Most Played Ever',
        'description': 'All-time most viewed songs per artist (sorted by view count)',
        'func': get_most_played_ever,
    },
    '3': {
        'name': 'Best of Albums',
        'description': 'Most played song from each album (unique original albums only)',
        'func': get_best_of_albums,
    },
    '4': {
        'name': 'Deep Cuts',
        'description': 'Hidden gems - skip the obvious hits, find the deeper tracks',
        'func': get_deep_cuts,
    },
    '5': {
        'name': 'Latest Releases',
        'description': 'Most recent singles and songs per artist',
        'func': get_latest_releases,
    },
    '6': {
        'name': 'One Hit Sampler',
        'description': 'Just the #1 song per artist for a quick overview playlist',
        'func': get_one_hit_sampler,
    },
    '7': {
        'name': 'Era Picker',
        'description': 'Songs from a specific year range (e.g. only 90s tracks)',
        'func': get_era_tracks,
    },
    '8': {
        'name': 'Setlist Mode',
        'description': 'Most commonly played live songs (requires SETLIST_FM_API_KEY)',
        'func': get_setlist_tracks,
    },
}

ALL_MODES = dict(TRACK_MODES)
ALL_MODES['9'] = {
    'name': 'Genre Cluster',
    'description': 'Auto-group bands by genre and create a separate playlist per genre',
}


# =============================================================================
# MENU, SETTINGS, BANDS SOURCE
# =============================================================================

def show_menu():
    """Display playlist mode selection menu."""
    print("\n=== YouTube Playlist Creator ===")
    print("Choose your playlist mode:\n")
    for key, mode in ALL_MODES.items():
        label = mode['name']
        if key == '8' and not get_key('SETLIST_FM_API_KEY'):
            label += '  [needs SETLIST_FM_API_KEY env var]'
        print(f"  {key}. {label}")
        print(f"     {mode['description']}\n")

    while True:
        choice = input("Enter choice (1-9): ").strip()
        if choice in ALL_MODES:
            return choice
        print("Invalid choice. Please enter 1-9.")


def get_mode_settings(choice):
    """Prompt for additional settings needed for the chosen mode."""
    settings = {}

    if choice == '3':
        while True:
            try:
                val = input("How many albums per artist? (default 5): ").strip() or '5'
                max_albums = int(val)
                if max_albums < 1:
                    print("Must be at least 1.")
                    continue
                settings['max_albums'] = max_albums
                break
            except ValueError:
                print("Please enter a number.")

    if choice == '7':
        while True:
            try:
                val = input("Start year (e.g. 1990): ").strip()
                settings['start_year'] = int(val)
                val = input("End year (e.g. 1999): ").strip()
                settings['end_year'] = int(val)
                if settings['start_year'] > settings['end_year']:
                    print("Start year must be before end year.")
                    continue
                break
            except ValueError:
                print("Please enter a valid year.")

    if choice in ('1', '2', '4', '5', '7', '8'):
        while True:
            try:
                val = input("How many songs per artist? (default 3): ").strip() or '3'
                count = int(val)
                if count < 1:
                    print("Must be at least 1.")
                    continue
                settings['count'] = count
                break
            except ValueError:
                print("Please enter a number.")

    return settings


def get_playlist_name(choice, settings):
    """Ask for a custom playlist name with a sensible default."""
    mode_name = ALL_MODES[choice]['name']
    suffix = ''
    if choice == '7':
        suffix = f" ({settings.get('start_year', '?')}-{settings.get('end_year', '?')})"
    default_name = f"Festival Playlist - {mode_name}{suffix}"
    custom = input(f"Playlist name (default: {default_name}): ").strip()
    return custom or default_name


def get_privacy():
    """Ask who can see the new playlist(s)."""
    options = {'1': 'private', '2': 'unlisted', '3': 'public'}
    default = next(k for k, v in options.items() if v == PLAYLIST_PRIVACY)
    while True:
        answer = input(
            f"Visibility: [1] private  [2] unlisted  [3] public (default {default}): "
        ).strip() or default
        if answer in options:
            return options[answer]
        print("Please enter 1, 2 or 3.")


def _bands_from_poster():
    """Option 4: extract a band list from a festival poster via Gemini vision."""
    source = input("Path or URL to the poster image: ").strip()
    if not source:
        print("No source given.")
        return []

    try:
        from poster_ocr import extract_bands_from_poster
    except ImportError as e:
        print(f"Error: {e}")
        return []

    print("Extracting bands from poster (usually 5-15 seconds)...")
    try:
        bands = extract_bands_from_poster(source)
    except Exception as e:
        print(f"Poster OCR failed: {e}")
        return []

    if not bands:
        print("No bands detected on the poster.")
        return []

    print(f"\nFound {len(bands)} bands:")
    for name in bands:
        print(f"  - {name}")

    print("\nUse this list? [y] yes  [s] save to bands.txt and exit to edit  [n] cancel")
    while True:
        answer = input("> ").strip().lower()
        if answer == 'y':
            return bands
        if answer == 's':
            if os.path.exists(DEFAULT_BANDS_FILE):
                sure = input(f"{DEFAULT_BANDS_FILE} exists. Overwrite it? [y/n]: ")
                if sure.strip().lower() != 'y':
                    print("Not saved. Enter y, s, or n.")
                    continue
            with open(DEFAULT_BANDS_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(bands) + '\n')
            print(f"Saved to {DEFAULT_BANDS_FILE}. Edit it and re-run the script.")
            return []
        if answer == 'n':
            print("Cancelled.")
            return []
        print("Please enter y, s, or n.")


def get_bands():
    """Let the user choose where the band list comes from."""
    print("\n=== Band Source ===")
    print(f"  1. Use {DEFAULT_BANDS_FILE}")
    print("  2. Use a different file")
    print("  3. Enter bands manually")
    print("  4. Scan a festival poster (OCR via Gemini)")

    while True:
        choice = input("Enter choice (1-4): ").strip()
        if choice in ('1', '2', '3', '4'):
            break
        print("Invalid choice.")

    if choice == '1':
        if not os.path.exists(DEFAULT_BANDS_FILE):
            print(f"Error: {DEFAULT_BANDS_FILE} not found.")
            return []
        with open(DEFAULT_BANDS_FILE, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]

    if choice == '2':
        path = input("Path to bands file: ").strip()
        if not os.path.exists(path):
            print(f"Error: {path} not found.")
            return []
        with open(path, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]

    if choice == '4':
        return _bands_from_poster()

    # Manual entry
    print("Enter band names one per line. Type 'done' when finished:")
    bands = []
    while True:
        name = input("> ").strip()
        if name.lower() == 'done':
            break
        if name:
            bands.append(name)
    return bands


# =============================================================================
# BAND PROCESSING
# =============================================================================

MODES_WITH_COUNT = {'1', '2', '4', '5', '7', '8'}


def call_track_mode(choice, settings, band_name):
    """Call the appropriate track-finding function with the right arguments.

    For count-based modes we oversample so duplicates can be replaced with
    the next-best alternate instead of leaving the slot empty."""
    func = TRACK_MODES[choice]['func']

    if choice == '3':
        return func(band_name, max_albums=settings.get('max_albums', 5))
    if choice == '6':
        return func(band_name)

    target = settings.get('count', 3)
    oversample = max(target * 3, target + 5)

    if choice == '7':
        return func(band_name, count=oversample,
                    start_year=settings.get('start_year'),
                    end_year=settings.get('end_year'))
    # Modes 1, 2, 4, 5, 8
    return func(band_name, count=oversample)


def process_bands(youtube, job, choice, settings, state, progress_callback=None):
    """Add tracks for every remaining band of one playlist job.

    `job` carries its own position (`band_index`, `partial_ids`) and is saved
    to the progress file after every single track, so a stop at any point
    (quota, Ctrl+C, crash) resumes at the same band without re-adding tracks.
    QuotaExceeded is allowed to propagate: the caller stops the whole run.
    `progress_callback(done, total, band)` is called after each band; if it
    returns False the run stops there by raising Cancelled.
    Returns the number of duplicates skipped.
    """
    bands = job['bands']
    playlist_id = job['playlist_id']
    log_entries = state['log_entries']
    seen_videos = set(job['added_video_ids'])
    partial = job.setdefault('partial_ids', [])
    dupes_skipped = 0

    while job['band_index'] < len(bands):
        i = job['band_index']
        band = bands[i]
        print(f"[{i + 1}/{len(bands)}] {band}")

        try:
            video_ids = call_track_mode(choice, settings, band)
        except Exception as e:
            log_entries.append(f"Error on {band}: {e}")
            print(f"  Error: {e}")
            video_ids = None

        if video_ids is not None and not video_ids:
            log_entries.append(f"No tracks found for {band}")
            print("  No tracks found")
        elif video_ids:
            # For count-based modes, keep adding until we hit the target
            # number of unique tracks — dupes shouldn't shrink the slot.
            target = (settings.get('count', 3) if choice in MODES_WITH_COUNT
                      else len(video_ids))
            added = len(partial)  # tracks added before an interrupted run
            dupes_in_band = 0
            failed = 0
            for v_id in video_ids:
                if added >= target:
                    break
                if v_id in partial:
                    continue
                if v_id in seen_videos:
                    log_entries.append(f"Skipped duplicate {v_id} for {band}")
                    dupes_in_band += 1
                    continue
                try:
                    add_video_to_playlist(youtube, playlist_id, v_id)
                except QuotaExceeded:
                    dupes_skipped += dupes_in_band
                    raise
                except HttpError as e:
                    # One unavailable video must not cost the band its
                    # remaining tracks: note it and try the next one.
                    reason = _http_error_reason(e) or str(e)
                    log_entries.append(f"Could not add {v_id} for {band}: {reason}")
                    failed += 1
                    continue
                seen_videos.add(v_id)
                partial.append(v_id)
                job['added_video_ids'].append(v_id)
                log_entries.append(f"Added video {v_id} for {band}")
                added += 1
                save_progress(state)
            dupes_skipped += dupes_in_band

            summary = f"  Added {added} track(s)"
            if dupes_in_band:
                summary += f" ({dupes_in_band} duplicate(s) skipped, replaced with alternates)"
            if failed:
                summary += f" ({failed} could not be added)"
            if added < target and choice in MODES_WITH_COUNT:
                summary += f" — only {added}/{target} unique available"
                log_entries.append(f"Only {added}/{target} unique tracks for {band}")
            print(summary)

        job['band_index'] = i + 1
        job['partial_ids'] = partial = []
        save_progress(state)

        if progress_callback and progress_callback(i + 1, len(bands), band) is False:
            state['dupes'] = state.get('dupes', 0) + dupes_skipped
            raise Cancelled()

    return dupes_skipped


def write_log(state):
    lines = list(state['log_entries'])
    if match_warnings:
        lines += ["", "Artist matches to check by hand:"] + match_warnings
    if _view_failures:
        lines += ["", f"View count lookups that failed: {_view_failures} "
                      "(rankings for those tracks may be off)"]
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))


def run_jobs(youtube, state, progress_callback=None):
    """Create and fill every playlist in `state['jobs']`, starting wherever
    the saved state says.

    Returns 'done' when everything finished, 'quota' when the daily quota
    ran out, or 'stopped' on Ctrl+C / a cancelling `progress_callback`. In
    the last two cases progress is kept so the run can be resumed.
    `state['dupes']` holds the running count of duplicates skipped."""
    choice = state['choice']
    settings = state['settings']
    log_entries = state['log_entries']
    jobs = state['jobs']
    stopped = None
    result = 'done'

    try:
        while state['job_index'] < len(jobs):
            job = jobs[state['job_index']]
            if not job.get('playlist_id'):
                job['playlist_id'] = create_playlist(
                    youtube, job['name'], job['description'],
                    state.get('privacy', PLAYLIST_PRIVACY))
                url = f"https://music.youtube.com/playlist?list={job['playlist_id']}"
                print(f"\nPlaylist '{job['name']}' created: {url}\n")
                log_entries.extend(job.get('header', []))
                log_entries.append(f"Playlist URL: {url}\n")
                save_progress(state)
            else:
                url = f"https://music.youtube.com/playlist?list={job['playlist_id']}"
                print(f"\nContinuing playlist '{job['name']}': {url}\n")
            if progress_callback and progress_callback(
                    job['band_index'], len(job['bands']), '') is False:
                raise Cancelled()

            state['dupes'] = state.get('dupes', 0) + process_bands(
                youtube, job, choice, settings, state, progress_callback)
            log_entries.append("")
            state['job_index'] += 1
            save_progress(state)
    except QuotaExceeded:
        stopped = ("YouTube's daily API quota is used up (about 199 track adds "
                   "per day on a default project). It resets at midnight "
                   "Pacific time.")
        result = 'quota'
    except (KeyboardInterrupt, Cancelled):
        stopped = "Stopped by you."
        result = 'stopped'

    write_log(state)

    if stopped:
        save_progress(state)
        job = jobs[state['job_index']]
        print(f"\n{stopped}")
        print(f"Progress saved at band {job['band_index'] + 1}/{len(job['bands'])} "
              f"of '{job['name']}'. Run the script again and answer 'y' to resume.")
        print(f"Log so far saved to {LOG_FILE}")
        return result

    clear_progress()
    total_bands = sum(len(j['bands']) for j in jobs)
    print(f"\nDone! {total_bands} bands processed. {state.get('dupes', 0)} duplicate(s) skipped.")
    for job in jobs:
        print(f"  {job['name']}: https://music.youtube.com/playlist?list={job['playlist_id']}")
    if match_warnings:
        print(f"\n{len(match_warnings)} band(s) had no exact artist name match - "
              f"check them in {LOG_FILE}.")
    if _view_failures:
        print(f"{_view_failures} view count lookup(s) failed; rankings for those "
              "tracks may be off.")
    print(f"Log saved to {LOG_FILE}")
    return 'done'


def new_job(name, description, bands, header):
    return {
        'name': name,
        'description': description,
        'bands': bands,
        'header': header,
        'playlist_id': None,
        'band_index': 0,
        'partial_ids': [],
        'added_video_ids': [],
    }


# =============================================================================
# MAIN
# =============================================================================

def main():
    if not os.path.exists(YOUTUBE_CLIENT_SECRETS_FILE):
        print(f"Error: no client_secret*.json file found in the current directory.")
        print("Download it from Google Cloud Console (see README) and place it here.")
        return

    # --- Check for resume ---
    progress = load_progress()
    if progress and progress['job_index'] >= len(progress['jobs']):
        clear_progress()
        progress = None
    if progress:
        job = progress['jobs'][progress['job_index']]
        print(f"\nFound saved progress: playlist {progress['job_index'] + 1}/"
              f"{len(progress['jobs'])} ('{job['name']}'), "
              f"band {job['band_index']}/{len(job['bands'])} done")
        answer = input("Resume previous session? [y/n]: ").strip().lower()
        if answer == 'y':
            run_jobs(get_youtube_service(), progress)
            return
        clear_progress()

    # --- Fresh run ---
    bands = get_bands()
    if not bands:
        print("No bands to process.")
        return

    choice = show_menu()
    settings = get_mode_settings(choice)

    if choice == '9':
        # --- Genre Cluster mode: one playlist job per genre ---
        print("\nWhich track mode should each genre playlist use?")
        for key, mode in TRACK_MODES.items():
            print(f"  {key}. {mode['name']}")
        while True:
            track_choice = input("Enter choice (1-8): ").strip()
            if track_choice in TRACK_MODES:
                break
            print("Invalid choice.")
        track_settings = get_mode_settings(track_choice)

        clusters = build_genre_clusters(bands)

        print("\n=== Genre Groups ===")
        for genre, genre_bands in sorted(clusters.items()):
            preview = ', '.join(genre_bands[:5])
            if len(genre_bands) > 5:
                preview += f', ... (+{len(genre_bands) - 5} more)'
            print(f"  {genre} ({len(genre_bands)} bands): {preview}")

        confirm = input("\nCreate a playlist for each group? [y/n]: ").strip().lower()
        if confirm != 'y':
            print("Cancelled.")
            return

        mode_name = TRACK_MODES[track_choice]['name']
        mode_desc = TRACK_MODES[track_choice]['description']
        jobs = []
        for genre, genre_bands in sorted(clusters.items()):
            default_name = f"Festival Playlist - {genre}"
            name = input(
                f"Playlist name for {genre} (default: {default_name}): "
            ).strip() or default_name
            jobs.append(new_job(
                name,
                f"{genre} bands - {mode_desc}. Generated by YouTube Playlist Creator.",
                genre_bands,
                [f"Genre: {genre}", f"Playlist: {name}", f"Track mode: {mode_name}"],
            ))
        choice, settings = track_choice, track_settings
    else:
        # --- Standard single-playlist mode ---
        playlist_name = get_playlist_name(choice, settings)
        print(f"\nMode: {ALL_MODES[choice]['name']}")
        print(f"Playlist: {playlist_name}")
        jobs = [new_job(
            playlist_name,
            f"{ALL_MODES[choice]['description']}. Generated by YouTube Playlist Creator.",
            bands,
            [f"Mode: {ALL_MODES[choice]['name']}", f"Playlist: {playlist_name}"],
        )]

    state = {
        'choice': choice,
        'settings': settings,
        'privacy': get_privacy(),
        'jobs': jobs,
        'job_index': 0,
        'log_entries': [],
    }

    total_tracks = sum(len(j['bands']) for j in jobs) * (
        settings.get('count', 3) if choice in MODES_WITH_COUNT
        else settings.get('max_albums', 1))
    if total_tracks > 190:
        print(f"\nNote: about {total_tracks} tracks planned. A default Google project "
              "allows about 199 adds per day, so this will pause when the quota "
              "runs out and can be resumed the next day.")

    run_jobs(get_youtube_service(), state)


if __name__ == '__main__':
    main()
