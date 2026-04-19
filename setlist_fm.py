"""setlist.fm API helpers — fetch the songs a band plays live.

Shared by both the YouTube Music and Spotify backends so we don't duplicate
the rate-limiting and artist-lookup logic.
"""
import os
import time
from collections import Counter

import requests


SETLIST_FM_API_KEY = os.environ.get('SETLIST_FM_API_KEY', '')

_last_request = 0.0


def _get(url, headers, params):
    """GET against setlist.fm, enforcing a minimum 1s gap between calls so
    we stay under the public rate limit regardless of how many bands we iterate."""
    global _last_request
    elapsed = time.time() - _last_request
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    _last_request = time.time()
    return resp


def get_most_played_live(band_name, count):
    """Return the top `count` most-commonly-played live song names for `band_name`,
    or None if setlist.fm is unconfigured/unavailable/has no data for the band.

    Backend code is expected to take these song names and resolve them to
    streaming-service track IDs via its own search API.
    """
    if not SETLIST_FM_API_KEY:
        return None

    headers = {
        'Accept': 'application/json',
        'x-api-key': SETLIST_FM_API_KEY,
    }

    try:
        resp = _get(
            'https://api.setlist.fm/rest/1.0/search/artists',
            headers=headers,
            params={'artistName': band_name, 'sort': 'relevance'},
        )
        if resp.status_code != 200:
            return None
        artists = resp.json().get('artist', [])
        if not artists:
            return None
        artist_mbid = artists[0]['mbid']

        resp = _get(
            f'https://api.setlist.fm/rest/1.0/artist/{artist_mbid}/setlists',
            headers=headers,
            params={'p': 1},
        )
        if resp.status_code != 200:
            return None
        setlists = resp.json().get('setlist', [])
    except Exception:
        return None

    song_counts = Counter()
    for setlist in setlists[:10]:
        for s in setlist.get('sets', {}).get('set', []):
            for song in s.get('song', []):
                name = song.get('name', '')
                if name:
                    song_counts[name] += 1

    if not song_counts:
        return None
    return [name for name, _ in song_counts.most_common(count)]
