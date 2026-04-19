# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

A Python CLI tool that creates curated YouTube Music playlists from a list of band names. It supports 9 playlist generation modes (top hits, deep cuts, era-based, setlists, genre clusters, etc.) and interacts with three external APIs: YouTube Data API v3, setlist.fm, and MusicBrainz.

## Running the App

```bash
python bot.py
```

No build step. No test suite. Install dependencies first:

```bash
pip install ytmusicapi google-auth-oauthlib google-api-python-client requests
```

## Architecture

The entire application lives in `bot.py` (monolithic, ~32KB). The flow is:

1. **Auth** — `get_youtube_service()` does OAuth2 via Google, caches tokens in `token.json`
2. **Input** — user picks a mode (1–9) and settings; band list loaded from `bands.txt`
3. **Search** — `find_artist()` + `ytmusicapi` find tracks per band
4. **Dedup** — global `added_video_ids` set prevents duplicate songs across the playlist
5. **Add to playlist** — YouTube Data API v3 inserts video IDs
6. **Resume** — `save_progress()` / `load_progress()` persist state to `progress.json` so a crashed run can be resumed

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

- `bot.py` — entire application
- `bands.txt` — input: one artist name per line
- `token.json` — OAuth token cache (not committed)
- `progress.json` — resume state (not committed)
- `playlist_log.txt` — run log

## API Credentials

- **YouTube API**: Requires `client_secret_*.json` from Google Cloud Console with OAuth 2.0 credentials and the YouTube Data API v3 enabled. The filename pattern is matched via glob at startup.
- **setlist.fm** (Mode 8 only): API key read from the `SETLIST_FM_API_KEY` environment variable. If unset, Mode 8 falls back to Mode 1. Calls are rate-limited via `_setlist_get()`.
- **MusicBrainz** (Mode 9 only): No key required; uses public API with rate limiting.
