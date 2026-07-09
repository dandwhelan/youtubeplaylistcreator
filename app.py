"""Flask web interface for the YouTube Playlist Creator.

Wraps the CLI logic in bot.py in a local web UI. Run with:

    python app.py

then open http://localhost:5000. Everything the CLI can do is available:
all 9 playlist modes, band sources (bands.txt, file upload, manual entry,
festival-poster OCR), genre-cluster preview, live progress, cancel and
resume.
"""
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


def _get_youtube_or_error():
    creds = _load_credentials()
    if not creds:
        return None, (jsonify({'error': 'Not authenticated with YouTube. '
                                         'Click "Connect YouTube" first.'}), 401)
    return build('youtube', 'v3', credentials=creds), None


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
# Status, modes, bands
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def status():
    progress = bot.load_progress()
    progress_info = None
    if progress:
        progress_info = {
            'band_index': progress.get('band_index', 0),
            'total': len(progress.get('bands', [])),
            'mode': bot.ALL_MODES.get(progress.get('choice', ''), {}).get('name', '?'),
        }
    return jsonify({
        'authenticated': _load_credentials() is not None,
        'has_client_secret': os.path.exists(bot.YOUTUBE_CLIENT_SECRETS_FILE),
        'setlist_key': bool(bot.SETLIST_FM_API_KEY),
        'gemini_key': bool(os.environ.get('GEMINI_API_KEY', '')),
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

def _make_callback(job):
    def callback(done, total, band):
        job['current'] = done
        job['total'] = total
        job['band'] = band
        return not job['cancel']
    return callback


def _run_single(job, youtube, bands, choice, settings, playlist_name,
                privacy, resume=None):
    try:
        if resume:
            playlist_id = resume['playlist_id']
            seen = set(resume.get('added_video_ids', []))
            job['log'].extend(resume.get('log_entries', []))
            start_index = resume.get('band_index', 0)
        else:
            description = (f"{bot.ALL_MODES[choice]['description']}. "
                           "Generated by YouTube Playlist Creator.")
            playlist_id = bot.create_playlist(youtube, playlist_name,
                                              description, privacy)
            seen = set()
            start_index = 0
            job['log'].append(f"Mode: {bot.ALL_MODES[choice]['name']}")
            job['log'].append(f"Playlist: {playlist_name}")

        playlist_url = f"https://music.youtube.com/playlist?list={playlist_id}"
        job['playlists'].append({'name': playlist_name, 'url': playlist_url})
        job['total'] = len(bands)
        job['current'] = start_index

        dupes = bot.process_bands(youtube, playlist_id, bands, choice,
                                  settings, job['log'], seen,
                                  start_index=start_index,
                                  progress_callback=_make_callback(job))
        job['dupes'] = dupes

        if job['cancel']:
            _finish_job(job, 'cancelled')
            return

        bot.clear_progress()
        with open(bot.LOG_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(job['log']))
        _finish_job(job, 'done')
    except Exception as e:
        _finish_job(job, 'error', e)


def _run_clusters(job, youtube, clusters, track_choice, settings, privacy):
    """clusters: list of {'genre', 'name', 'bands'} dicts."""
    try:
        total_dupes = 0
        for cluster in clusters:
            if job['cancel']:
                _finish_job(job, 'cancelled')
                return
            genre = cluster['genre']
            playlist_name = cluster['name']
            genre_bands = cluster['bands']

            mode_desc = bot.TRACK_MODES[track_choice]['description']
            description = (f"{genre} bands - {mode_desc}. "
                           "Generated by YouTube Playlist Creator.")
            playlist_id = bot.create_playlist(youtube, playlist_name,
                                              description, privacy)
            playlist_url = f"https://music.youtube.com/playlist?list={playlist_id}"
            job['playlists'].append({'name': playlist_name, 'url': playlist_url})
            job['log'].append(f"--- {genre}: {playlist_name} ({playlist_url})")
            job['total'] = len(genre_bands)
            job['current'] = 0

            seen = set()  # duplicate tracking resets per genre playlist
            total_dupes += bot.process_bands(
                youtube, playlist_id, genre_bands, track_choice, settings,
                job['log'], seen, progress_callback=_make_callback(job))

        job['dupes'] = total_dupes
        if job['cancel']:
            _finish_job(job, 'cancelled')
            return
        bot.clear_progress()
        with open(bot.LOG_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(job['log']))
        _finish_job(job, 'done')
    except Exception as e:
        _finish_job(job, 'error', e)


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

    threading.Thread(
        target=_run_single,
        args=(job, youtube, bands, choice, settings, playlist_name,
              _parse_privacy(data)),
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

    threading.Thread(
        target=_run_clusters,
        args=(job, youtube, clusters, track_choice, settings,
              _parse_privacy(data)),
        daemon=True).start()
    return jsonify({'started': True})


@app.route('/api/resume', methods=['POST'])
def resume():
    progress = bot.load_progress()
    if not progress:
        return jsonify({'error': 'No saved progress found.'}), 404

    youtube, auth_err = _get_youtube_or_error()
    if auth_err:
        return auth_err

    job = _new_job('run')
    if job is None:
        return jsonify({'error': 'A job is already running.'}), 409

    choice = progress['choice']
    playlist_name = progress.get('settings', {}).get(
        'playlist_name', bot.ALL_MODES.get(choice, {}).get('name', 'Playlist'))
    threading.Thread(
        target=_run_single,
        args=(job, youtube, progress['bands'], choice,
              progress.get('settings', {}), playlist_name,
              bot.PLAYLIST_PRIVACY),
        kwargs={'resume': progress},
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
