# Roadmap

Tracked work for the YouTube Playlist Creator. Check off items as they land.

## Bug / Technical Debt

- [ ] **Mode 4 "Deep Cuts" is weak** — current logic slices index 5–7 of `artist['songs']['results']`, which only surfaces slightly-less-popular hits. Should pull full discography (album tracks), rank by view count, and skip the top N truly popular.
- [ ] **Progress save is O(n²)** — `save_progress` writes the full `bands` list, full log, and entire `added_video_ids` after every band. Swap for an append-only log, or only write the diff.
- [ ] **No tests** — add smoke tests for each mode using recorded API fixtures (VCR.py / responses).
- [ ] **Quota visibility** — YouTube Data API v3 has per-day quotas; surface current usage and warn before exhaustion.
- [ ] **CLAUDE.md note about setlist.fm key is stale** — says hardcoded; it's actually env-var driven.

## Feature Backlog

### Festival / Event Sourcing
- [ ] **Festival lineup scraper** — pull Download, Wacken, Coachella, Bonnaroo, Glastonbury, Reading/Leeds lineups directly from official sites / Music Festival Wizard / Songkick into `bands.txt`.
- [ ] **Poster OCR mode** — accept an image path or URL, run OCR (Tesseract via `pytesseract`, Google Cloud Vision, or Anthropic vision API), fuzzy-match extracted text against MusicBrainz, and produce a cleaned band list.
- [ ] **Songkick integration** — "bands playing near me in the next 90 days" as a mode.
- [ ] **Bandsintown API** — personal-calendar-driven band list using a user's tracked artists.

### Playlist Intelligence
- [ ] **Smart ordering** — energy-curve sort (BPM / loudness) so playlists flow well, instead of band-list order.
- [ ] **Similar-artists expander** — for each seed band, add the top N related artists via YTMusic's `get_related_artists`.
- [ ] **Collaboration mode** — only include tracks featuring two or more artists from the input list.
- [ ] **Discovery ratio** — mix X% known hits with Y% deep cuts per artist.
- [ ] **Length targeting** — build an exactly-N-hour playlist instead of N-per-artist.

### Input / UX
- [ ] **Spotify import** — Spotify playlist URL → YouTube Music playlist.
- [ ] **Last.fm scrobble import** — top artists over a user-specified window.
- [ ] **Wikipedia discography parser** — for Mode 3 (Best of Albums), parse Wikipedia discography tables for a canonical studio-album list (more reliable than YTMusic's variant-laden list).
- [ ] **Dry-run / preview mode** — print the tracklist that *would* be added, without calling the YouTube Data API (saves daily quota during testing).
- [ ] **Config file (`config.yaml`)** — supply mode, counts, year range, playlist name via config for scripted re-runs.

### Output / Sharing
- [ ] **Multi-platform export** — also create Spotify / Apple Music / Tidal playlists from the same run.
- [ ] **CSV export** of the final tracklist (artist, title, album, year, duration, video ID).
- [ ] **Markdown report** with album art, track durations, and release years.
