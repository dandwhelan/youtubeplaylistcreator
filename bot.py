import os
from ytmusicapi import YTMusic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- CONFIGURATION ---
YOUTUBE_CLIENT_SECRETS_FILE = 'client_secret_1089582393796-e9mev9i2a4l4pcbbqvpj0prhg9p2t8kr.apps.googleusercontent.com.json'
PLAYLIST_NAME = 'Download Festival 2026 Hype'
PLAYLIST_DESCRIPTION = 'Top 3 tracks for every band on the lineup. Generated automatically by Python.'
PLAYLIST_PRIVACY = 'public'
BANDS_FILE = 'bands.txt'
LOG_FILE = 'playlist_log.txt'
TOKEN_FILE = 'token.json'

# --- API SETUP ---
SCOPES = ['https://www.googleapis.com/auth/youtube']
ytmusic = YTMusic()

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

def get_top_tracks(band_name):
    print(f"Searching YouTube Music for top tracks: {band_name}")
    search_results = ytmusic.search(query=band_name, filter="artists", limit=1)
    
    if not search_results:
        return []
        
    artist_browse_id = search_results[0]['browseId']
    artist_details = ytmusic.get_artist(artist_browse_id)
    
    if 'songs' in artist_details and 'results' in artist_details['songs']:
        # Grab the top 3 IDs from their official Top Songs list
        return [song['videoId'] for song in artist_details['songs']['results'][:3] if 'videoId' in song]
        
    return []

def create_playlist(youtube):
    request = youtube.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {"title": PLAYLIST_NAME, "description": PLAYLIST_DESCRIPTION},
            "status": {"privacyStatus": PLAYLIST_PRIVACY}
        }
    )
    return request.execute()['id']

def add_video_to_playlist(youtube, playlist_id, video_id):
    youtube.playlistItems().insert(
        part="snippet",
        body={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
    ).execute()

def main():
    if not os.path.exists(BANDS_FILE) or not os.path.exists(YOUTUBE_CLIENT_SECRETS_FILE):
        print("Error: Missing files.")
        return

    youtube = get_youtube_service()
    playlist_id = create_playlist(youtube)
    playlist_url = f"https://music.youtube.com/playlist?list={playlist_id}"
    print(f"Playlist created: {playlist_url}")
    
    with open(BANDS_FILE, 'r', encoding='utf-8') as f:
        bands = [line.strip() for line in f if line.strip()]

    log_entries = [f"Playlist URL: {playlist_url}\n"]

    for band in bands:
        try:
            video_ids = get_top_tracks(band)
            if not video_ids:
                log_entries.append(f"No tracks found for {band}")
                continue
                
            for v_id in video_ids:
                add_video_to_playlist(youtube, playlist_id, v_id)
                log_entries.append(f"Added video ID {v_id} for {band}")
                
        except Exception as e:
            log_entries.append(f"Error on {band}: {e}")

    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        f.write("\n".join(log_entries))
    print("Done!")

if __name__ == '__main__':
    main()
