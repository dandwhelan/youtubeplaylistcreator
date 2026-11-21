"""YouTube Data API writer, error classification and quota tracking."""
import json

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

import bot
from conftest import make_state


class FakeResp(dict):
    def __init__(self, status):
        super().__init__(status=str(status))
        self.status = status
        self.reason = ''


def http_error(status, reason=''):
    body = json.dumps({'error': {'errors': [{'reason': reason}]}}).encode()
    return HttpError(FakeResp(status), body)


class Req:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def execute(self):
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def test_quota_error_maps_to_quota_exceeded():
    with pytest.raises(bot.QuotaExceeded):
        bot._execute(Req([http_error(403, 'quotaExceeded')]))


def test_401_and_refresh_error_map_to_auth_expired():
    with pytest.raises(bot.AuthExpired):
        bot._execute(Req([http_error(401, 'authError')]))
    with pytest.raises(bot.AuthExpired):
        bot._execute(Req([RefreshError('invalid_grant')]))


def test_transient_errors_retried():
    req = Req([ConnectionError('reset'), http_error(503, 'backendError'), {'id': 'ok'}])
    assert bot._execute(req) == {'id': 'ok'}


def test_transient_errors_give_up_as_temporary_failure():
    with pytest.raises(bot.TemporaryFailure):
        bot._execute(Req([ConnectionError('x')] * 10))


def test_quota_tracker_counts_and_resets(monkeypatch):
    bot._record_quota(50)
    bot._record_quota(50)
    assert bot.quota_status()['used'] == 100
    assert bot.quota_status()['adds_left'] == 198
    monkeypatch.setattr(bot, '_pacific_today', lambda: '2099-01-01')
    assert bot.quota_status()['used'] == 0  # new Pacific day


def test_quota_limit_override(monkeypatch):
    monkeypatch.setenv('YOUTUBE_DAILY_QUOTA', '20000')
    assert bot.quota_status()['adds_left'] == 400


def test_data_api_writer_records_quota_and_wraps_failures():
    class Res:
        def __init__(self, outcomes):
            self.outcomes = outcomes

        def insert(self, **kw):
            return Req([self.outcomes.pop(0)])

    class YT:
        def __init__(self):
            self.items = Res([{}, http_error(404, 'videoNotFound'), http_error(403, 'quotaExceeded')])

        def playlists(self):
            return Res([{'id': 'PL9'}])

        def playlistItems(self):
            return self.items

    w = bot.DataApiWriter(YT())
    assert w.create_playlist('n', 'd', 'private') == 'PL9'
    w.add_video('PL9', 'v1')
    with pytest.raises(bot.AddFailed):
        w.add_video('PL9', 'v2')
    assert bot.quota_status()['used'] == 150
    with pytest.raises(bot.QuotaExceeded):
        w.add_video('PL9', 'v3')
    assert bot.quota_status()['remaining'] == 0


def test_forecast_warns_when_over_quota():
    bot._record_quota(9900)
    msg = bot.quota_forecast(make_state(['A'] * 10, count=3), bot.DataApiWriter(None))
    assert 'resume on ~1 more day' in msg


def test_dry_run_writes_nothing(fake_modes, tmp_path):
    w = bot.DryRunWriter()
    assert bot.run_jobs(w, make_state(['A'], count=2)) == 'done'
    assert w.tracks == ['A-0', 'A-1']
    assert not (tmp_path / 'progress.json').exists()


def test_browser_auth_setup_writes_file(tmp_path):
    headers = ("accept: */*\ncookie: SAPISID=abc; __Secure-3PAPISID=abc\n"
               "x-goog-authuser: 0\nuser-agent: Mozilla/5.0")
    bot.setup_browser_auth(headers)
    assert bot.has_browser_auth()


def test_track_titles_noted_from_ytmusic_results():
    bot._note_titles({'tracks': [{'videoId': 'v1', 'title': 'Song'}],
                      'other': {'results': [{'videoId': 'v2', 'title': 'Two'}]}})
    assert bot._track_titles == {'v1': 'Song', 'v2': 'Two'}
