import os
import re
from ytmusicapi import YTMusic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- CONFIGURATION ---
YOUTUBE_CLIENT_SECRETS_FILE = 'client_secret_1089582393796-e9mev9i2a4l4pcbbqvpj0prhg9p2t8kr.apps.googleusercontent.com.json'
BANDS_FILE = 'bands.txt'
LOG_FILE = 'playlist_log.txt'
TOKEN_FILE = 'token.json'
PLAYLIST_PRIVACY = 'public'

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


# --- HELPERS ---

def find_artist(band_name):
    """Search for an artist and return their browse ID."""
    search_results = ytmusic.search(query=band_name, filter="artists", limit=1)
    if not search_results:
        return None
    return search_results[0]['browseId']


def get_view_count(video_id):
    """Get the view count for a video via YouTube Music."""
    try:
        details = ytmusic.get_song(video_id)
        return int(details.get('videoDetails', {}).get('viewCount', 0))
    except Exception:
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
    # Remove content in parentheses/brackets that contains variant keywords
    cleaned = re.sub(
        r'\s*[\(\[].*?(deluxe|bonus|remaster|expanded|edition|anniversary|'
        r'version|special|collector|super|explicit|clean).*?[\)\]]\s*$',
        '', title, flags=re.IGNORECASE
    ).strip()
    # Remove trailing " - Remastered" style suffixes
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

    # Prefer original releases
    for album in originals:
        base = get_album_base_name(album['title'])
        if base not in seen:
            seen.add(base)
            unique.append(album)

    # Add back variants only if their base name hasn't been seen
    for album in variants:
        base = get_album_base_name(album['title'])
        if base not in seen:
            seen.add(base)
            unique.append(album)

    return unique


# --- PLAYLIST MODES ---

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

    # Get view counts and sort by them
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

            # Find the track with the most views in this album
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
    # Skip the top 5 popular songs, grab the deeper tracks
    deep = [song['videoId'] for song in songs[5:5 + count] if 'videoId' in song]

    # Fall back if the artist doesn't have enough songs
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

    # Try singles first (these are typically the most recent releases)
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

    # Fall back to top songs
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


# --- MENU AND MAIN ---

MODES = {
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
}


def show_menu():
    """Display playlist mode selection menu and return the user's choice."""
    print("\n=== YouTube Playlist Creator ===")
    print("Choose your playlist mode:\n")
    for key, mode in MODES.items():
        print(f"  {key}. {mode['name']}")
        print(f"     {mode['description']}\n")

    while True:
        choice = input("Enter choice (1-6): ").strip()
        if choice in MODES:
            return choice
        print("Invalid choice. Please enter 1-6.")


def get_mode_settings(choice):
    """Prompt for any additional settings needed for the chosen mode."""
    settings = {}

    if choice == '3':  # Best of Albums
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

    if choice in ('1', '2', '4', '5'):
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

    mode_name = MODES[choice]['name']
    default_name = f"Download Festival 2026 - {mode_name}"
    custom_name = input(f"Playlist name (default: {default_name}): ").strip()
    settings['playlist_name'] = custom_name or default_name

    return settings


def main():
    if not os.path.exists(BANDS_FILE):
        print(f"Error: Missing {BANDS_FILE}")
        return
    if not os.path.exists(YOUTUBE_CLIENT_SECRETS_FILE):
        print(f"Error: Missing {YOUTUBE_CLIENT_SECRETS_FILE}")
        return

    choice = show_menu()
    mode = MODES[choice]
    settings = get_mode_settings(choice)

    print(f"\nMode: {mode['name']}")
    print(f"Playlist: {settings['playlist_name']}")

    youtube = get_youtube_service()
    description = f"{mode['description']}. Generated by YouTube Playlist Creator."
    playlist_id = create_playlist(youtube, settings['playlist_name'], description)
    playlist_url = f"https://music.youtube.com/playlist?list={playlist_id}"
    print(f"Playlist created: {playlist_url}\n")

    with open(BANDS_FILE, 'r', encoding='utf-8') as f:
        bands = [line.strip() for line in f if line.strip()]

    log_entries = [
        f"Mode: {mode['name']}",
        f"Playlist: {settings['playlist_name']}",
        f"Playlist URL: {playlist_url}\n",
    ]

    for i, band in enumerate(bands, 1):
        print(f"[{i}/{len(bands)}] {band}")
        try:
            if choice == '3':
                video_ids = mode['func'](band, max_albums=settings.get('max_albums', 5))
            elif choice == '6':
                video_ids = mode['func'](band)
            else:
                video_ids = mode['func'](band, count=settings.get('count', 3))

            if not video_ids:
                log_entries.append(f"No tracks found for {band}")
                print(f"  No tracks found")
                continue

            for v_id in video_ids:
                add_video_to_playlist(youtube, playlist_id, v_id)
                log_entries.append(f"Added video {v_id} for {band}")

            print(f"  Added {len(video_ids)} track(s)")

        except Exception as e:
            log_entries.append(f"Error on {band}: {e}")
            print(f"  Error: {e}")

    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        f.write("\n".join(log_entries))

    print(f"\nDone! {len(bands)} bands processed.")
    print(f"Playlist: {playlist_url}")
    print(f"Log saved to {LOG_FILE}")


if __name__ == '__main__':
    main()
