"""Flask web interface for the YouTube Playlist Creator.

Wraps the CLI logic in bot.py in a local web UI. Run with:

    python app.py

then open http://localhost:5000. Everything the CLI can do is available:
all 9 playlist modes, band sources (bands.txt, file upload, manual entry,
festival-poster OCR), genre-cluster preview, live progress, cancel and
resume.
"""
import json
import os
import tempfile
import threading
import time

from flask import Flask, jsonify, redirect, render_template, request
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

import bot
import config
import spotify_sync

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Job state: one background job at a time (playlist run or genre clustering).
# ---------------------------------------------------------------------------

_job_lock = threading.Lock()
_job = {'state': 'idle'}


def _new_job(kind):
    """Claim the job slot. Returns the job dict, or None if one is running."""
    global _job
    with _job_lock:
        if _job.get('state') == 'running':
            return None
        _job = {
            'state': 'running',
            'kind': kind,
            'cancel': False,
            'current': 0,
            'total': 0,
            'band': '',
            'log': [],
            'playlists': [],
            'clusters': None,
            'dupes': 0,
            'error': None,
        }
        return _job


def _job_snapshot():
    with _job_lock:
        snap = dict(_job)
    snap['log'] = list(snap.get('log', []))
    snap.pop('cancel', None)
    return snap


def _finish_job(job, state, error=None):
    job['state'] = state
    if error:
        job['error'] = str(error)


# ---------------------------------------------------------------------------
# YouTube auth (web flow — the CLI's run_local_server doesn't fit here)
# ---------------------------------------------------------------------------

_pending_flow = None


def _load_credentials():
    """Return valid Credentials from token.json, refreshing if needed."""
    if not os.path.exists(bot.TOKEN_FILE):
        return None
    try:
        creds = Credentials.from_authorized_user_file(bot.TOKEN_FILE, bot.SCOPES)
    except Exception:
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            return None
        with open(bot.TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
        return creds
    return None


_SPOTIFY_NOT_CONNECTED = ('Not connected to Spotify. Open "API keys & credentials", '
                          'follow the Spotify steps, then click "Connect Spotify".')


def _youtube_writer(data):
    """YouTube writer: quota-free browser sign-in if saved, else Data API."""
    if bot.has_browser_auth() and not data.get('force_data_api'):
        try:
            return bot.YTMusicWriter(), None
        except Exception:
            pass  # bad browser.json: fall through to the Data API
    creds = _load_credentials()
    if not creds:
        return None, (jsonify({'error': 'Not authenticated with YouTube. '
                                         'Click "Connect YouTube" first, or save a '
                                         'YouTube Music sign-in.'}), 401)
    return bot.DataApiWriter(build('youtube', 'v3', credentials=creds)), None


def _get_youtube_or_error(platform=None, source=None):
    """Return a playlist writer for the request's platform
    ('youtube', 'spotify' or 'both'), or a dry-run writer."""
    data = request.get_json(silent=True) or {}
    platform = platform or data.get('platform', 'youtube')
    source = source or data.get('source', 'spotify')
    if platform in ('spotify', 'both') and not spotify_sync.is_connected():
        return None, (jsonify({'error': _SPOTIFY_NOT_CONNECTED}), 401)
    if data.get('dry_run'):
        match = platform == 'both' or (platform == 'spotify' and source != 'spotify')
        return bot.DryRunWriter(match_spotify=match,
                                require_match=platform == 'spotify'), None
    if platform == 'spotify':
        return spotify_sync.SpotifyWriter(), None
    yt, err = _youtube_writer(data)
    if err:
        return None, err
    if platform == 'both':
        return bot.MultiWriter([spotify_sync.SpotifyWriter(), yt]), None
    return yt, None


@app.route('/api/auth/start', methods=['POST'])
def auth_start():
    global _pending_flow
    if not os.path.exists(bot.YOUTUBE_CLIENT_SECRETS_FILE):
        return jsonify({'error': 'No client_secret*.json found. Download OAuth '
                                 'credentials from Google Cloud Console (see README).'}), 400
    redirect_uri = request.host_url.rstrip('/') + '/oauth2callback'
    flow = Flow.from_client_secrets_file(
        bot.YOUTUBE_CLIENT_SECRETS_FILE, scopes=bot.SCOPES,
        redirect_uri=redirect_uri)
    auth_url, _state = flow.authorization_url(
        access_type='offline', prompt='consent', include_granted_scopes='true')
    _pending_flow = flow
    return jsonify({'auth_url': auth_url})


@app.route('/oauth2callback')
def oauth2callback():
    global _pending_flow
    if _pending_flow is None:
        return 'No sign-in in progress. Go back to the app and click "Connect YouTube".', 400
    try:
        _pending_flow.fetch_token(authorization_response=request.url)
    except Exception as e:
        return f'Sign-in failed: {e}', 400
    creds = _pending_flow.credentials
    with open(bot.TOKEN_FILE, 'w') as f:
        f.write(creds.to_json())
    _pending_flow = None
    return redirect('/')


# ---------------------------------------------------------------------------
# Spotify auth (PKCE; redirect must be a 127.0.0.1 loopback URL)
# ---------------------------------------------------------------------------

_spotify_pending = None


@app.route('/api/spotify/start', methods=['POST'])
def spotify_start():
    global _spotify_pending
    port = request.host.rsplit(':', 1)[-1] if ':' in request.host else '5000'
    try:
        url, _spotify_pending = spotify_sync.begin_auth(spotify_sync.web_redirect(port))
    except spotify_sync.SpotifyAuthError as e:
        return jsonify({'error': f'{e} Paste your Spotify Client ID in "API keys & '
                                 'credentials" first.'}), 400
    return jsonify({'auth_url': url})


@app.route('/spotify/callback')
def spotify_callback():
    global _spotify_pending
    if request.args.get('error'):
        return f"Spotify sign-in was cancelled or refused: {request.args['error']}", 400
    if _spotify_pending is None:
        return 'No Spotify sign-in in progress. Go back to the app and click "Connect Spotify".', 400
    try:
        spotify_sync.finish_auth(_spotify_pending, request.args.get('code'),
                                 request.args.get('state'))
    except spotify_sync.SpotifyAuthError as e:
        return str(e), 400
    _spotify_pending = None
    return redirect('/')


@app.route('/api/spotify/disconnect', methods=['POST'])
def spotify_disconnect():
    if os.path.exists(spotify_sync.TOKEN_FILE):
        os.remove(spotify_sync.TOKEN_FILE)
    return jsonify({'disconnected': True})


# ---------------------------------------------------------------------------
# API keys & credentials
# ---------------------------------------------------------------------------

@app.route('/api/keys', methods=['GET'])
def get_keys():
    keys = {}
    for name in config.MANAGED_KEYS:
        keys[name] = {
            'set': bool(config.get_key(name)),
            'source': config.key_source(name),
        }
    has_secret = os.path.exists(bot.YOUTUBE_CLIENT_SECRETS_FILE)
    return jsonify({
        'keys': keys,
        'client_secret': {
            'present': has_secret,
            'file': bot.YOUTUBE_CLIENT_SECRETS_FILE if has_secret else None,
        },
        'browser_auth': bot.has_browser_auth(),
    })


@app.route('/api/keys', methods=['POST'])
def save_keys():
    data = request.get_json(silent=True) or {}
    changed = []
    for name in config.MANAGED_KEYS:
        if name in data:
            config.set_key(name, str(data[name] or ''))
            changed.append(name)
    if not changed:
        return jsonify({'error': 'No keys given.'}), 400
    return jsonify({'saved': changed})


@app.route('/api/browser_auth', methods=['POST'])
def browser_auth():
    """Save YouTube Music request headers as browser.json (quota-free writes)."""
    data = request.get_json(silent=True) or {}
    headers_raw = (data.get('headers') or '').strip()
    if not headers_raw:
        if os.path.exists(bot.BROWSER_AUTH_FILE):
            os.remove(bot.BROWSER_AUTH_FILE)
            return jsonify({'removed': True})
        return jsonify({'error': 'Paste the request headers first.'}), 400
    if headers_raw.lstrip().startswith('{'):
        return jsonify({'error': 'That is the response (the music data), not the request '
                                 'headers. In DevTools click the Headers tab, or right-click '
                                 'the request > Copy > Copy request headers.'}), 400
    if headers_raw.lstrip().lower().startswith('http') and '\n' not in headers_raw.strip():
        return jsonify({'error': 'That is only the request address. Right-click the request '
                                 '> Copy > Copy request headers instead.'}), 400
    if 'cookie' not in headers_raw.lower() and not headers_raw.lstrip().lower().startswith('curl'):
        return jsonify({'error': 'No cookie header found. Copy the headers of a '
                                 'POST /browse request from a signed-in '
                                 'music.youtube.com tab.'}), 400
    try:
        bot.setup_browser_auth(headers_raw)
    except Exception as e:
        hint = ''
        if 'x-goog-authuser' in str(e):
            hint = (' The copy was probably cut off (Edge and Chrome shorten long values). '
                    'Right-click the request > Copy > Copy as cURL instead.')
        return jsonify({'error': f'Could not use those headers: {e}{hint}'}), 400
    return jsonify({'saved': bot.BROWSER_AUTH_FILE})


@app.route('/api/client_secret', methods=['POST'])
def upload_client_secret():
    upload = request.files.get('file')
    if not upload or not upload.filename:
        return jsonify({'error': 'No file uploaded.'}), 400

    raw = upload.read()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return jsonify({'error': 'Not a valid JSON file.'}), 400
    if not any(k in parsed for k in ('installed', 'web')):
        return jsonify({'error': 'This does not look like a Google OAuth client '
                                 'secret (missing "installed"/"web" section). '
                                 'Download it from Google Cloud Console → '
                                 'Credentials → your OAuth client.'}), 400

    # Overwrite the existing secrets file if there is one, else create the
    # default name and point bot at it.
    target = bot.YOUTUBE_CLIENT_SECRETS_FILE \
        if os.path.exists(bot.YOUTUBE_CLIENT_SECRETS_FILE) else 'client_secret.json'
    with open(target, 'wb') as f:
        f.write(raw)
    bot.YOUTUBE_CLIENT_SECRETS_FILE = target
    return jsonify({'saved': target})


# ---------------------------------------------------------------------------
# Status, modes, bands
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def status():
    progress = bot.load_progress()
    progress_info = None
    if progress and progress['job_index'] < len(progress['jobs']):
        current = progress['jobs'][progress['job_index']]
        progress_info = {
            'band_index': current['band_index'],
            'total': len(current['bands']),
            'mode': bot.ALL_MODES.get(progress.get('choice', ''), {}).get('name', '?'),
            'playlist': current['name'],
            'playlist_index': progress['job_index'] + 1,
            'playlist_total': len(progress['jobs']),
            'platform': progress.get('platform', 'youtube'),
        }
    return jsonify({
        'authenticated': _load_credentials() is not None or bot.has_browser_auth(),
        'browser_auth': bot.has_browser_auth(),
        'spotify_connected': spotify_sync.is_connected(),
        'spotify_client_id': bool(spotify_sync.client_id()),
        'quota': bot.quota_status(),
        'has_client_secret': os.path.exists(bot.YOUTUBE_CLIENT_SECRETS_FILE),
        'setlist_key': bool(config.get_key('SETLIST_FM_API_KEY')),
        'gemini_key': bool(config.get_key('GEMINI_API_KEY')),
        'bands_file_exists': os.path.exists(bot.DEFAULT_BANDS_FILE),
        'progress': progress_info,
        'job_state': _job_snapshot().get('state', 'idle'),
    })


@app.route('/api/modes')
def modes():
    result = []
    for key, mode in bot.ALL_MODES.items():
        result.append({
            'key': key,
            'name': mode['name'],
            'description': mode['description'],
        })
    return jsonify({'modes': result})


@app.route('/api/bands', methods=['GET'])
def get_bands():
    if not os.path.exists(bot.DEFAULT_BANDS_FILE):
        return jsonify({'bands': []})
    with open(bot.DEFAULT_BANDS_FILE, 'r', encoding='utf-8') as f:
        bands = [line.strip() for line in f if line.strip()]
    return jsonify({'bands': bands})


@app.route('/api/bands', methods=['POST'])
def save_bands():
    data = request.get_json(silent=True) or {}
    bands = [b.strip() for b in data.get('bands', []) if b and b.strip()]
    if not bands:
        return jsonify({'error': 'No bands to save.'}), 400
    with open(bot.DEFAULT_BANDS_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(bands) + '\n')
    return jsonify({'saved': len(bands)})


@app.route('/api/poster', methods=['POST'])
def poster():
    """Extract bands from a festival poster: uploaded file or URL."""
    try:
        from poster_ocr import extract_bands_from_poster
    except ImportError as e:
        return jsonify({'error': f'poster_ocr unavailable: {e}'}), 500

    source = None
    tmp_path = None
    if 'file' in request.files and request.files['file'].filename:
        upload = request.files['file']
        suffix = os.path.splitext(upload.filename)[1] or '.jpg'
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, 'wb') as f:
            upload.save(f)
        source = tmp_path
    else:
        data = request.get_json(silent=True) or request.form or {}
        source = (data.get('url') or '').strip()

    if not source:
        return jsonify({'error': 'Provide an image file or a poster URL.'}), 400

    try:
        bands = extract_bands_from_poster(source)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    return jsonify({'bands': bands})


# ---------------------------------------------------------------------------
# Playlist runs
# ---------------------------------------------------------------------------

def _run_state(job, youtube, state):
    """Run (or resume) a bot.run_jobs state in the background job slot.

    All playlist logic, progress saving and quota handling live in
    bot.run_jobs; this only mirrors its progress into the job dict the
    front end polls."""
    def callback(done, total, band):
        job['current'] = done
        job['total'] = total
        job['band'] = band
        label = {'youtube': 'YouTube Music', 'spotify': 'Spotify'}
        job['playlists'] = [
            {'name': j['name'] + (f" ({label.get(plat, plat)})"
                                  if youtube.platform == 'both' else ''),
             'url': url}
            for j in state['jobs']
            if j.get('playlist_id') and not str(j['playlist_id']).startswith('DRYRUN')
            for plat, url in bot.playlist_links(youtube, j['playlist_id'])]
        job['dupes'] = state.get('dupes', 0)
        return not job['cancel']

    try:
        job['log'] = state['log_entries']  # same list: the log shows live
        bot.match_warnings.clear()
        result = bot.run_jobs(youtube, state, progress_callback=callback)
        callback(job['current'], job['total'], job['band'])
        job['warnings'] = list(bot.match_warnings)
        _finish_job(job, {'done': 'done', 'quota': 'quota', 'stopped': 'cancelled',
                          'auth': 'interrupted', 'network': 'interrupted',
                          'error': 'interrupted'}[result],
                    state.get('last_stop') if result in ('auth', 'network', 'error')
                    else None)
    except Exception as e:
        _finish_job(job, 'error', e)


def _new_state(choice, settings, privacy, jobs):
    data = request.get_json(silent=True) or {}
    return {
        'platform': data.get('platform') if data.get('platform') in ('spotify', 'both')
        else 'youtube',
        'choice': choice,
        'settings': settings,
        'privacy': privacy,
        'jobs': jobs,
        'job_index': 0,
        'log_entries': [],
    }


def _cluster_preview(job, bands):
    try:
        clusters = {}
        job['total'] = len(bands)
        for i, band in enumerate(bands, 1):
            if job['cancel']:
                _finish_job(job, 'cancelled')
                return
            job['current'] = i
            job['band'] = band
            tags = bot.get_artist_genre(band)
            genre = bot.classify_genre(tags)
            clusters.setdefault(genre, []).append(band)
            job['log'].append(f"{band}: {genre}")
            job['clusters'] = {g: list(b) for g, b in clusters.items()}
            time.sleep(1)  # MusicBrainz rate limit: 1 req/sec
        _finish_job(job, 'done')
    except Exception as e:
        _finish_job(job, 'error', e)


def _parse_settings(choice, data):
    """Validate mode settings from the request. Returns (settings, error)."""
    settings = {}
    if choice == '3':
        try:
            settings['max_albums'] = max(1, int(data.get('max_albums', 5)))
        except (TypeError, ValueError):
            return None, 'Albums per artist must be a number.'
    if choice == '7':
        try:
            settings['start_year'] = int(data.get('start_year'))
            settings['end_year'] = int(data.get('end_year'))
        except (TypeError, ValueError):
            return None, 'Era Picker needs a valid start and end year.'
        if settings['start_year'] > settings['end_year']:
            return None, 'Start year must be before end year.'
    if choice in ('1', '2', '4', '5', '7', '8'):
        try:
            settings['count'] = max(1, int(data.get('count', 3)))
        except (TypeError, ValueError):
            return None, 'Songs per artist must be a number.'
    if data.get('platform') == 'spotify' and data.get('source', 'spotify') == 'spotify':
        settings['source'] = 'spotify'
    return settings, None


def _parse_privacy(data):
    privacy = data.get('privacy', bot.PLAYLIST_PRIVACY)
    return privacy if privacy in ('public', 'unlisted', 'private') else bot.PLAYLIST_PRIVACY


@app.route('/api/run', methods=['POST'])
def run():
    data = request.get_json(silent=True) or {}
    bands = [b.strip() for b in data.get('bands', []) if b and b.strip()]
    choice = str(data.get('mode', ''))

    if not bands:
        return jsonify({'error': 'No bands given.'}), 400
    if choice not in bot.TRACK_MODES:
        return jsonify({'error': 'Invalid mode.'}), 400

    settings, err = _parse_settings(choice, data)
    if err:
        return jsonify({'error': err}), 400

    playlist_name = (data.get('playlist_name') or '').strip()
    if not playlist_name:
        suffix = ''
        if choice == '7':
            suffix = f" ({settings['start_year']}-{settings['end_year']})"
        playlist_name = f"{bot.ALL_MODES[choice]['name']}{suffix}"

    youtube, auth_err = _get_youtube_or_error()
    if auth_err:
        return auth_err

    job = _new_job('run')
    if job is None:
        return jsonify({'error': 'A job is already running.'}), 409

    state = _new_state(choice, settings, _parse_privacy(data), [bot.new_job(
        playlist_name,
        f"{bot.ALL_MODES[choice]['description']}. Generated by YouTube Playlist Creator.",
        bands,
        [f"Mode: {bot.ALL_MODES[choice]['name']}", f"Playlist: {playlist_name}"],
    )])
    threading.Thread(target=_run_state, args=(job, youtube, state),
                     daemon=True).start()
    return jsonify({'started': True})


@app.route('/api/cluster', methods=['POST'])
def cluster():
    data = request.get_json(silent=True) or {}
    bands = [b.strip() for b in data.get('bands', []) if b and b.strip()]
    if not bands:
        return jsonify({'error': 'No bands given.'}), 400

    job = _new_job('cluster')
    if job is None:
        return jsonify({'error': 'A job is already running.'}), 409

    threading.Thread(target=_cluster_preview, args=(job, bands),
                     daemon=True).start()
    return jsonify({'started': True})


@app.route('/api/run_clusters', methods=['POST'])
def run_clusters():
    data = request.get_json(silent=True) or {}
    track_choice = str(data.get('track_mode', ''))
    clusters = data.get('clusters', [])

    if track_choice not in bot.TRACK_MODES:
        return jsonify({'error': 'Invalid track mode.'}), 400
    if not clusters or not all(c.get('bands') for c in clusters):
        return jsonify({'error': 'No genre clusters given.'}), 400
    for c in clusters:
        if not (c.get('name') or '').strip():
            return jsonify({'error': f"Playlist name missing for {c.get('genre', '?')}."}), 400

    settings, err = _parse_settings(track_choice, data)
    if err:
        return jsonify({'error': err}), 400

    youtube, auth_err = _get_youtube_or_error()
    if auth_err:
        return auth_err

    job = _new_job('run')
    if job is None:
        return jsonify({'error': 'A job is already running.'}), 409

    mode = bot.TRACK_MODES[track_choice]
    state = _new_state(track_choice, settings, _parse_privacy(data), [
        bot.new_job(
            c['name'].strip(),
            f"{c.get('genre', '?')} bands - {mode['description']}. "
            "Generated by YouTube Playlist Creator.",
            [b.strip() for b in c['bands'] if b and b.strip()],
            [f"Genre: {c.get('genre', '?')}", f"Playlist: {c['name'].strip()}",
             f"Track mode: {mode['name']}"],
        ) for c in clusters])
    threading.Thread(target=_run_state, args=(job, youtube, state),
                     daemon=True).start()
    return jsonify({'started': True})


@app.route('/api/resume', methods=['POST'])
def resume():
    progress = bot.load_progress()
    if not progress or progress['job_index'] >= len(progress['jobs']):
        return jsonify({'error': 'No saved progress found.'}), 404

    youtube, auth_err = _get_youtube_or_error(
        progress.get('platform', 'youtube'),
        progress.get('settings', {}).get('source', 'youtube'))
    if auth_err:
        return auth_err

    job = _new_job('run')
    if job is None:
        return jsonify({'error': 'A job is already running.'}), 409

    threading.Thread(target=_run_state, args=(job, youtube, progress),
                     daemon=True).start()
    return jsonify({'started': True})


@app.route('/api/progress/clear', methods=['POST'])
def progress_clear():
    bot.clear_progress()
    return jsonify({'cleared': True})


@app.route('/api/job')
def job_status():
    return jsonify(_job_snapshot())


@app.route('/api/job/cancel', methods=['POST'])
def job_cancel():
    with _job_lock:
        if _job.get('state') == 'running':
            _job['cancel'] = True
            return jsonify({'cancelling': True})
    return jsonify({'error': 'No job running.'}), 400


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    # OAuth over plain http://localhost requires this for oauthlib
    os.environ.setdefault('OAUTHLIB_INSECURE_TRANSPORT', '1')
    print(f"YouTube Playlist Creator web UI: http://localhost:{port}")
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
