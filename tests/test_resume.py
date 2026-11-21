"""Playlist runs: dedup, progress saving, and resuming after every kind of stop."""
import pytest

import bot
from conftest import FakeWriter, make_state


def test_full_run_adds_count_per_band(fake_modes):
    w = FakeWriter()
    assert bot.run_jobs(w, make_state(['A', 'B'], count=2)) == 'done'
    assert w.added == ['A-0', 'A-1', 'B-0', 'B-1']
    assert bot.load_progress() is None  # cleared on success


def test_unavailable_track_replaced_by_next(fake_modes):
    w = FakeWriter(reject={'A-0'})
    bot.run_jobs(w, make_state(['A'], count=2))
    assert w.added == ['A-1', 'A-2']


def test_duplicates_across_bands_skipped(monkeypatch):
    monkeypatch.setattr(bot, 'call_track_mode', lambda c, s, b: ['shared', f'{b}-x', f'{b}-y'])
    w = FakeWriter()
    bot.run_jobs(w, make_state(['A', 'B'], count=2))
    assert w.added == ['shared', 'A-x', 'B-x', 'B-y']


@pytest.mark.parametrize('exc,result', [
    (bot.QuotaExceeded('q'), 'quota'),
    (bot.AuthExpired('token revoked'), 'auth'),
    (bot.TemporaryFailure('net'), 'network'),
    (ValueError('boom'), 'error'),
])
def test_stop_mid_band_then_resume_without_duplicates(fake_modes, exc, result):
    w = FakeWriter(fail_at=4, exc=exc)
    assert bot.run_jobs(w, make_state(['A', 'B', 'C'], count=2)) == result
    saved = bot.load_progress()
    assert saved['jobs'][0]['band_index'] == 1
    assert saved['jobs'][0]['partial_ids'] == ['B-0']

    w2 = FakeWriter()
    assert bot.run_jobs(w2, saved) == 'done'
    assert w2.created == 0  # reuses the existing playlist
    assert w.added + w2.added == ['A-0', 'A-1', 'B-0', 'B-1', 'C-0', 'C-1']


def test_cancel_via_callback_is_resumable(fake_modes):
    w = FakeWriter()
    calls = []

    def cb(done, total, band):
        calls.append(done)
        return done < 1  # stop after the first band

    assert bot.run_jobs(w, make_state(['A', 'B'], count=1), progress_callback=cb) == 'stopped'
    assert bot.load_progress()['jobs'][0]['band_index'] == 1


def test_progress_write_is_atomic(tmp_path):
    bot.save_progress({'x': 1})
    assert not (tmp_path / 'progress.json.tmp').exists()
    assert (tmp_path / 'progress.json').read_text().strip().startswith('{')


def test_corrupt_progress_ignored(tmp_path):
    (tmp_path / 'progress.json').write_text('{not json')
    assert bot.load_progress() is None


def test_estimate_adds_counts_remaining_work():
    st = make_state(['A', 'B', 'C'], count=3)
    assert bot.estimate_adds(st) == 1 + 9
    st['jobs'][0].update(playlist_id='PL', band_index=1, partial_ids=['B-0'])
    assert bot.estimate_adds(st) == 6 - 1
