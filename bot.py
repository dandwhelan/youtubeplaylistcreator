import os
import re
import glob
import json
import time
import requests
from ytmusicapi import YTMusic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import spotify_client
from setlist_fm import SETLIST_FM_API_KEY, get_most_played_live


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
PLAYLIST_PRIVACY = 'public'

# Setlist Mode (Mode 8) uses the SETLIST_FM_API_KEY env var, read in setlist_fm.py

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
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(YOUTUBE_CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())

    return build('youtube', 'v3', credentials=creds)


def create_playlist(youtube, name, description):
    request = youtube.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {"title": name, "description": description},
            "status": {"privacyStatus": PLAYLIST_PRIVACY}
        }
    )
    return request.execute()['id']


def add_video_to_playlist(youtube, playlist_id, video_id):
    youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id}
            }
        }
    ).execute()


# =============================================================================
# HELPERS
# =============================================================================

def find_artist(band_name):
    """Search for an artist and return their browse ID."""
    search_results = ytmusic.search(query=band_name, filter="artists", limit=1)
    if not search_results:
        return None
    return search_results[0]['browseId']


_view_count_cache = {}


def get_view_count(video_id):
    """Get the view count for a video via YouTube Music. Cached per video_id
    to avoid redundant lookups when the same track is ranked in multiple modes."""
    if video_id in _view_count_cache:
        return _view_count_cache[video_id]
    try:
        details = ytmusic.get_song(video_id)
        views = int(details.get('videoDetails', {}).get('viewCount', 0))
    except Exception:
        views = 0
    _view_count_cache[video_id] = views
    return views


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
# RESUME / PROGRESS SUPPORT
# =============================================================================

def save_progress(data):
    """Save current progress so the run can be resumed later."""
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def load_progress():
    """Load saved progress, or return None if no save file exists."""
    if not os.path.exists(PROGRESS_FILE):
        return None
    try:
        with open(PROGRESS_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
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
    browse_id = find_artist(band_name)
    if not browse_id:
        return []

    artist = ytmusic.get_artist(browse_id)
    if 'songs' in artist and 'results' in artist['songs']:
        return [song['videoId'] for song in artist['songs']['results'][:count]
                if 'videoId' in song]
    return []


def get_most_played_ever(band_name, count=3):
    """Mode 2: Most played/viewed songs of all time (sorted by view count)."""
    print(f"  Searching most played ever: {band_name}")
    browse_id = find_artist(band_name)
    if not browse_id:
        return []

    artist = ytmusic.get_artist(browse_id)
    if 'songs' not in artist or 'results' not in artist['songs']:
        return []

    songs = artist['songs']['results']
    songs_with_views = []
    for song in songs:
        if 'videoId' not in song:
            continue
        views = get_view_count(song['videoId'])
        songs_with_views.append((song['videoId'], views))

    songs_with_views.sort(key=lambda x: x[1], reverse=True)
    return [vid for vid, _ in songs_with_views[:count]]


def get_best_of_albums(band_name, max_albums=5):
    """Mode 3: Most played song from each album (unique albums only)."""
    print(f"  Searching best album tracks: {band_name} (max {max_albums} albums)")
    browse_id = find_artist(band_name)
    if not browse_id:
        return []

    artist = ytmusic.get_artist(browse_id)
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
    browse_id = find_artist(band_name)
    if not browse_id:
        return []

    artist = ytmusic.get_artist(browse_id)
    if 'songs' not in artist or 'results' not in artist['songs']:
        return []

    songs = artist['songs']['results']
    deep = [song['videoId'] for song in songs[5:5 + count] if 'videoId' in song]
    if not deep:
        deep = [song['videoId'] for song in songs[3:3 + count] if 'videoId' in song]
    return deep


def get_latest_releases(band_name, count=3):
    """Mode 5: Most recent singles and songs."""
    print(f"  Searching latest releases: {band_name}")
    browse_id = find_artist(band_name)
    if not browse_id:
        return []

    artist = ytmusic.get_artist(browse_id)

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

    if 'songs' in artist and 'results' in artist['songs']:
        return [song['videoId'] for song in artist['songs']['results'][:count]
                if 'videoId' in song]
    return []


def get_one_hit_sampler(band_name):
    """Mode 6: Just the single #1 song per artist."""
    print(f"  Searching #1 hit: {band_name}")
    browse_id = find_artist(band_name)
    if not browse_id:
        return []

    artist = ytmusic.get_artist(browse_id)
    if 'songs' in artist and 'results' in artist['songs']:
        for song in artist['songs']['results'][:1]:
            if 'videoId' in song:
                return [song['videoId']]
    return []


def get_era_tracks(band_name, count=3, start_year=None, end_year=None):
    """Mode 7: Songs from a specific era/year range."""
    print(f"  Searching {start_year}-{end_year} era tracks: {band_name}")
    browse_id = find_artist(band_name)
    if not browse_id:
        return []

    artist = ytmusic.get_artist(browse_id)

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


def get_setlist_tracks(band_name, count=3):
    """Mode 8: most commonly played live songs (via setlist.fm), searched on
    YouTube Music. Falls back to Mode 1 if setlist.fm is unconfigured or empty."""
    print(f"  Searching setlist data: {band_name}")

    song_names = get_most_played_live(band_name, count)
    if not song_names:
        print("    setlist.fm unavailable or no data — falling back to top popular")
        return get_top_popular(band_name, count)

    video_ids = []
    for song_name in song_names:
        results = ytmusic.search(f"{band_name} {song_name}", filter="songs", limit=1)
        if results and 'videoId' in results[0]:
            video_ids.append(results[0]['videoId'])

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
                if keyword in tag or tag in keyword:
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
    print("\n=== Playlist Mode ===")
    print("Choose your playlist mode:\n")
    for key, mode in ALL_MODES.items():
        label = mode['name']
        if key == '8' and not SETLIST_FM_API_KEY:
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
    default_name = f"Download Festival 2026 - {mode_name}{suffix}"
    custom = input(f"Playlist name (default: {default_name}): ").strip()
    return custom or default_name


def get_bands():
    """Let the user choose where the band list comes from."""
    print("\n=== Band Source ===")
    print(f"  1. Use {DEFAULT_BANDS_FILE}")
    print("  2. Use a different file")
    print("  3. Enter bands manually")

    while True:
        choice = input("Enter choice (1-3): ").strip()
        if choice in ('1', '2', '3'):
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
# BACKEND DISPATCH
# =============================================================================

SPOTIFY_TRACK_MODES = {
    '1': spotify_client.get_top_popular,
    '2': spotify_client.get_most_played_ever,
    '3': spotify_client.get_best_of_albums,
    '4': spotify_client.get_deep_cuts,
    '5': spotify_client.get_latest_releases,
    '6': spotify_client.get_one_hit_sampler,
    '7': spotify_client.get_era_tracks,
    '8': spotify_client.get_setlist_tracks,
}


def pick_platform():
    """Let the user choose where their playlist is created."""
    print("\n=== Target Platform ===")
    print("  1. YouTube Music")
    print("  2. Spotify")
    while True:
        choice = input("Enter choice (1-2): ").strip()
        if choice == '1':
            return 'youtube'
        if choice == '2':
            return 'spotify'
        print("Invalid choice.")


def authenticate_backend(platform):
    """Do the OAuth dance for the selected backend and return its client."""
    if platform == 'youtube':
        return get_youtube_service()
    return spotify_client.authenticate()


def create_backend_playlist(platform, client, name, description):
    if platform == 'youtube':
        return create_playlist(client, name, description)
    return spotify_client.create_playlist(client, name, description)


def add_backend_track(platform, client, playlist_id, track_id, seen_tracks):
    """Add a track to the playlist only if it hasn't been added already.
    Mutates `seen_tracks` on success. Returns True if added, False if duplicate."""
    if track_id in seen_tracks:
        return False
    if platform == 'youtube':
        add_video_to_playlist(client, playlist_id, track_id)
    else:
        spotify_client.add_track(client, playlist_id, track_id)
    seen_tracks.add(track_id)
    return True


def backend_playlist_url(platform, playlist_id):
    if platform == 'youtube':
        return f"https://music.youtube.com/playlist?list={playlist_id}"
    return spotify_client.playlist_url(playlist_id)


MODES_WITH_COUNT = {'1', '2', '4', '5', '7', '8'}


def call_track_mode(platform, client, choice, settings, band_name):
    """Dispatch to the right track-finding function for the platform.

    For count-based modes we oversample so duplicates can be replaced with
    the next-best alternate instead of leaving the slot empty."""
    if platform == 'youtube':
        func = TRACK_MODES[choice]['func']
        yt_args = True
    else:
        func = SPOTIFY_TRACK_MODES[choice]
        yt_args = False

    # YouTube mode funcs: (band, ...); Spotify mode funcs: (client, band, ...)
    def call(**kwargs):
        if yt_args:
            return func(band_name, **kwargs)
        return func(client, band_name, **kwargs)

    if choice == '3':
        return call(max_albums=settings.get('max_albums', 5))
    if choice == '6':
        return call()

    target = settings.get('count', 3)
    oversample = max(target * 3, target + 5)

    if choice == '7':
        return call(count=oversample,
                    start_year=settings.get('start_year'),
                    end_year=settings.get('end_year'))
    # Modes 1, 2, 4, 5, 8
    return call(count=oversample)


# =============================================================================
# BAND PROCESSING
# =============================================================================

def process_bands(platform, client, playlist_id, bands, choice, settings,
                  log_entries, seen_tracks, start_index=0):
    """Process a list of bands, adding tracks to the playlist.

    `seen_tracks` is a set of already-added track IDs; mutated in place.
    Supports resuming from start_index. Saves progress after each band.
    Returns the number of duplicates skipped.
    """
    dupes_skipped = 0

    for i in range(start_index, len(bands)):
        band = bands[i]
        print(f"[{i + 1}/{len(bands)}] {band}")
        try:
            track_ids = call_track_mode(platform, client, choice, settings, band)

            if not track_ids:
                log_entries.append(f"No tracks found for {band}")
                print("  No tracks found")
            else:
                # For count-based modes, keep adding until we hit the target
                # number of unique tracks — dupes shouldn't shrink the slot.
                target = settings.get('count', 3) if choice in MODES_WITH_COUNT else len(track_ids)
                added = 0
                dupes_in_band = 0
                for track_id in track_ids:
                    if added >= target:
                        break
                    if add_backend_track(platform, client, playlist_id, track_id, seen_tracks):
                        log_entries.append(f"Added track {track_id} for {band}")
                        added += 1
                    else:
                        log_entries.append(f"Skipped duplicate {track_id} for {band}")
                        dupes_in_band += 1
                dupes_skipped += dupes_in_band

                summary = f"  Added {added} track(s)"
                if dupes_in_band:
                    summary += f" ({dupes_in_band} duplicate(s) skipped, replaced with alternates)"
                if added < target:
                    summary += f" — only {added}/{target} unique available"
                    log_entries.append(f"Only {added}/{target} unique tracks for {band}")
                print(summary)

        except Exception as e:
            log_entries.append(f"Error on {band}: {e}")
            print(f"  Error: {e}")

        # Save progress after each band
        save_progress({
            'platform': platform,
            'playlist_id': playlist_id,
            'choice': choice,
            'settings': settings,
            'band_index': i + 1,
            'bands': bands,
            'seen_tracks': list(seen_tracks),
            'log_entries': log_entries,
        })

    return dupes_skipped


# =============================================================================
# MAIN
# =============================================================================

def main():
    # --- Check for resume ---
    progress = load_progress()
    if progress:
        total = len(progress.get('bands', []))
        idx = progress.get('band_index', 0)
        platform = progress.get('platform', 'youtube')
        print(f"\nFound saved progress: band {idx}/{total} on {platform.title()}")
        answer = input("Resume previous session? [y/n]: ").strip().lower()
        if answer == 'y':
            if platform == 'youtube' and not os.path.exists(YOUTUBE_CLIENT_SECRETS_FILE):
                print("Error: no client_secret*.json file found. Cannot resume.")
                return
            try:
                client = authenticate_backend(platform)
            except RuntimeError as e:
                print(f"Auth failed: {e}")
                return

            playlist_id = progress['playlist_id']
            choice = progress['choice']
            settings = progress['settings']
            bands = progress['bands']
            log_entries = progress['log_entries']
            seen_tracks = set(
                progress.get('seen_tracks')
                or progress.get('added_video_ids')
                or []
            )
            url = backend_playlist_url(platform, playlist_id)

            print(f"Resuming from band {idx + 1}/{total}")
            print(f"Playlist: {url}\n")

            dupes = process_bands(platform, client, playlist_id, bands, choice,
                                  settings, log_entries, seen_tracks,
                                  start_index=idx)

            clear_progress()
            with open(LOG_FILE, 'w', encoding='utf-8') as f:
                f.write("\n".join(log_entries))
            print(f"\nDone! {total} bands processed. {dupes} duplicate(s) skipped.")
            print(f"Playlist: {url}")
            print(f"Log saved to {LOG_FILE}")
            return
        else:
            clear_progress()

    # --- Fresh run ---
    bands = get_bands()
    if not bands:
        print("No bands to process.")
        return

    platform = pick_platform()

    if platform == 'youtube' and not os.path.exists(YOUTUBE_CLIENT_SECRETS_FILE):
        print("Error: no client_secret*.json file found in the current directory.")
        print("Download it from Google Cloud Console (see README) and place it here.")
        return

    choice = show_menu()
    settings = get_mode_settings(choice)

    # --- Genre Cluster mode ---
    if choice == '9':
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

        # Show groupings
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

        try:
            client = authenticate_backend(platform)
        except RuntimeError as e:
            print(f"Auth failed: {e}")
            return

        all_log_entries = []
        total_dupes = 0

        for genre, genre_bands in sorted(clusters.items()):
            playlist_name = input(
                f"Playlist name for {genre} (default: Download 2026 - {genre}): "
            ).strip() or f"Download 2026 - {genre}"

            mode_desc = TRACK_MODES[track_choice]['description']
            description = f"{genre} bands - {mode_desc}. Generated by Playlist Creator."
            playlist_id = create_backend_playlist(platform, client, playlist_name, description)
            url = backend_playlist_url(platform, playlist_id)
            print(f"\nPlaylist '{playlist_name}' created: {url}")

            log_entries = [
                f"Genre: {genre}",
                f"Platform: {platform}",
                f"Playlist: {playlist_name}",
                f"Track mode: {TRACK_MODES[track_choice]['name']}",
                f"Playlist URL: {url}\n",
            ]

            # Reset duplicate tracking per genre playlist
            seen_tracks = set()
            dupes = process_bands(platform, client, playlist_id, genre_bands,
                                  track_choice, track_settings, log_entries,
                                  seen_tracks)
            total_dupes += dupes
            all_log_entries.extend(log_entries)
            all_log_entries.append("")

        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.write("\n".join(all_log_entries))
        print(f"\nAll genre playlists created! {total_dupes} total duplicate(s) skipped.")
        print(f"Log saved to {LOG_FILE}")
        return

    # --- Standard single-playlist mode ---
    playlist_name = get_playlist_name(choice, settings)
    settings['playlist_name'] = playlist_name

    print(f"\nPlatform: {platform.title()}")
    print(f"Mode: {ALL_MODES[choice]['name']}")
    print(f"Playlist: {playlist_name}")

    try:
        client = authenticate_backend(platform)
    except RuntimeError as e:
        print(f"Auth failed: {e}")
        return

    description = f"{ALL_MODES[choice]['description']}. Generated by Playlist Creator."
    playlist_id = create_backend_playlist(platform, client, playlist_name, description)
    url = backend_playlist_url(platform, playlist_id)
    print(f"Playlist created: {url}\n")

    log_entries = [
        f"Platform: {platform}",
        f"Mode: {ALL_MODES[choice]['name']}",
        f"Playlist: {playlist_name}",
        f"Playlist URL: {url}\n",
    ]

    seen_tracks = set()
    dupes = process_bands(platform, client, playlist_id, bands, choice, settings,
                          log_entries, seen_tracks)

    clear_progress()
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        f.write("\n".join(log_entries))

    print(f"\nDone! {len(bands)} bands processed. {dupes} duplicate(s) skipped.")
    print(f"Playlist: {url}")
    print(f"Log saved to {LOG_FILE}")


if __name__ == '__main__':
    main()
