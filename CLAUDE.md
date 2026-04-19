# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

A Python CLI tool that creates curated music playlists from a list of band names. User picks the target platform at run time — **YouTube Music** or **Spotify** — and one of 9 playlist generation modes (top hits, deep cuts, era-based, setlists, genre clusters, etc.). Interacts with: YouTube Data API v3, Spotify Web API, setlist.fm, MusicBrainz.

## Running the App

```bash
python bot.py
```

No build step. No test suite. Install dependencies first:

```bash
pip install ytmusicapi google-auth-oauthlib google-api-python-client requests
```

## Architecture

Code is split across:
- **`bot.py`** — CLI orchestration: menus, mode dispatch, YouTube Music mode implementations, `main()`, resume handling.
- **`spotify_client.py`** — parallel Spotify backend: OAuth, playlist ops, and all 8 track-mode implementations against the Spotify Web API.
- **`setlist_fm.py`** — shared setlist.fm helper (rate-limited GETs, "most-played-live" song-name fetch) used by both backends.
- **`poster_ocr.py`** — Gemini vision wrapper for festival-poster band extraction (Band Source option 4).

The flow is:

1. **Band source** — user picks file/manual/poster → `bands` list
2. **Target platform** — `pick_platform()` returns `'youtube'` or `'spotify'`
3. **Mode selection** — user picks a mode (1–9) and settings
4. **Auth** — `authenticate_backend(platform)` dispatches to `get_youtube_service()` or `spotify_client.authenticate()`
5. **Playlist creation** — `create_backend_playlist(platform, client, ...)`
6. **Track finding** — `call_track_mode(platform, client, choice, ...)` dispatches to YT-Music mode funcs or `SPOTIFY_TRACK_MODES[choice]`
7. **Dedup + oversample** — each mode returns extra candidates so duplicates can be replaced with alternates; `seen_tracks` set prevents the same track twice
8. **Add to playlist** — `add_backend_track(platform, client, ...)`
9. **Resume** — `progress.json` stores `platform` alongside the rest so a crashed run re-auths the right backend

### The 9 Modes

Each mode is a distinct code path within `process_bands()`:

| Mode | Name | Key Logic |
|------|------|-----------|
| 1 | Top Popular Now | Trending songs from ytmusicapi |
| 2 | Most Played Ever | Songs sorted by `get_view_count()` |
| 3 | Best of Albums | Top track per album via `filter_unique_albums()` |
| 4 | Deep Cuts | Skips the top N songs to surface lesser-known tracks |
| 5 | Latest Releases | Filters by recent release date |
| 6 | One Hit Sampler | Single song per artist |
| 7 | Era Picker | Songs filtered to a user-specified year range |
| 8 | Setlist Mode | Queries setlist.fm API; falls back to Mode 1 on failure |
| 9 | Genre Cluster | Groups bands by genre via MusicBrainz, creates one playlist per genre group |

### Album Deduplication (Mode 3)

`filter_unique_albums()` removes duplicate album variants (deluxe, remaster, bonus, live editions). It relies on `get_album_base_name()` to normalize titles and `is_original_album()` to score variants.

### Genre Clustering (Mode 9)

`get_artist_genre()` calls MusicBrainz (rate-limited). `classify_genre()` maps raw tags to 5 buckets: Metal, Rock, Punk, Electronic, Pop/Other. `build_genre_clusters()` runs this for all bands before playlist creation begins.

## Key Files

- `bot.py` — CLI orchestration and YouTube Music modes
- `spotify_client.py` — Spotify Web API backend
- `setlist_fm.py` — shared setlist.fm helper
- `poster_ocr.py` — Gemini vision wrapper for poster OCR
- `bands.txt` — input: one artist name per line
- `token.json` — YouTube OAuth token cache (not committed)
- `.spotify_cache` — Spotify OAuth token cache (not committed)
- `progress.json` — resume state (not committed)
- `playlist_log.txt` — run log

## API Credentials

- **YouTube API** (YouTube Music target): Requires `client_secret*.json` from Google Cloud Console with OAuth 2.0 credentials and the YouTube Data API v3 enabled. The filename pattern is matched via glob at startup by `_find_client_secrets_file()`.
- **Spotify Web API** (Spotify target): Requires `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` env vars. Create a free app at https://developer.spotify.com/dashboard and add `http://127.0.0.1:8765/callback` as a Redirect URI. Token cached in `.spotify_cache` by spotipy.
- **setlist.fm** (Mode 8 only): API key read from the `SETLIST_FM_API_KEY` environment variable. If unset, Mode 8 falls back to Mode 1. Calls are rate-limited via `setlist_fm._get()`.
- **MusicBrainz** (Mode 9 only): No key required; uses public API with rate limiting.
- **Gemini** (Band Source option 4 only): API key read from the `GEMINI_API_KEY` environment variable.
