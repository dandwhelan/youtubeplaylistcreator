/* Front-end for the YouTube Playlist Creator web UI. */

const $ = (id) => document.getElementById(id);

const COUNT_MODES = ['1', '2', '4', '5', '7', '8'];
let selectedMode = '1';
let modesData = [];
let pollTimer = null;
let lastClusters = null; // {genre: [bands]} from the preview job

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function api(path, options) {
  const resp = await fetch(path, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `Request failed (${resp.status})`);
  return data;
}

function postJSON(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

function getBands() {
  return $('bands-text').value.split('\n').map((b) => b.trim()).filter(Boolean);
}

function setBands(bands) {
  $('bands-text').value = bands.join('\n');
  updateBandCount();
}

function updateBandCount() {
  $('band-count').textContent = getBands().length;
}

// ---------------------------------------------------------------------------
// Status / auth
// ---------------------------------------------------------------------------

async function loadStatus() {
  const s = await api('/api/status');

  const authPill = $('pill-auth');
  if (s.authenticated) {
    authPill.textContent = 'YouTube: connected';
    authPill.className = 'pill ok';
    $('btn-connect').classList.add('hidden');
  } else {
    authPill.textContent = s.has_client_secret
      ? 'YouTube: not connected'
      : 'YouTube: client_secret*.json missing';
    authPill.className = 'pill bad';
    $('btn-connect').classList.toggle('hidden', !s.has_client_secret);
  }

  $('pill-setlist').textContent = s.setlist_key ? 'setlist.fm ✓' : 'setlist.fm key not set';
  $('pill-setlist').className = 'pill ' + (s.setlist_key ? 'ok' : '');
  $('pill-gemini').textContent = s.gemini_key ? 'Gemini ✓' : 'Gemini key not set';
  $('pill-gemini').className = 'pill ' + (s.gemini_key ? 'ok' : '');

  if (s.progress && s.job_state !== 'running') {
    $('resume-text').textContent =
      `Unfinished run found (${s.progress.mode}): band ${s.progress.band_index}/${s.progress.total}.`;
    $('resume-banner').classList.remove('hidden');
  } else {
    $('resume-banner').classList.add('hidden');
  }

  if (s.job_state === 'running') startPolling();
}

$('btn-connect').onclick = async () => {
  try {
    const data = await postJSON('/api/auth/start', {});
    window.location.href = data.auth_url;
  } catch (e) {
    alert(e.message);
  }
};

$('btn-resume').onclick = async () => {
  try {
    await postJSON('/api/resume', {});
    $('resume-banner').classList.add('hidden');
    startPolling();
  } catch (e) {
    alert(e.message);
  }
};

$('btn-discard').onclick = async () => {
  await postJSON('/api/progress/clear', {});
  $('resume-banner').classList.add('hidden');
};

// ---------------------------------------------------------------------------
// Bands
// ---------------------------------------------------------------------------

document.querySelectorAll('#band-tabs .tab').forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll('#band-tabs .tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    $('panel-upload').hidden = tab.dataset.tab !== 'upload';
    $('panel-poster').hidden = tab.dataset.tab !== 'poster';
    if (tab.dataset.tab === 'file') loadBandsFile();
    if (tab.dataset.tab === 'manual') $('bands-text').focus();
  };
});

async function loadBandsFile() {
  try {
    const data = await api('/api/bands');
    if (data.bands.length) setBands(data.bands);
  } catch (e) { /* bands.txt may not exist yet */ }
}

$('btn-reload').onclick = loadBandsFile;

$('btn-save').onclick = async () => {
  try {
    const data = await postJSON('/api/bands', { bands: getBands() });
    alert(`Saved ${data.saved} bands to bands.txt`);
  } catch (e) {
    alert(e.message);
  }
};

$('upload-file').onchange = (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => setBands(reader.result.split('\n').map((b) => b.trim()).filter(Boolean));
  reader.readAsText(file);
};

$('bands-text').oninput = updateBandCount;

$('btn-poster').onclick = async () => {
  const file = $('poster-file').files[0];
  const url = $('poster-url').value.trim();
  if (!file && !url) return alert('Choose a poster image or paste a URL.');

  $('poster-status').textContent = 'Extracting bands from poster (usually 5–15 seconds)…';
  $('btn-poster').disabled = true;
  try {
    let data;
    if (file) {
      const form = new FormData();
      form.append('file', file);
      data = await api('/api/poster', { method: 'POST', body: form });
    } else {
      data = await postJSON('/api/poster', { url });
    }
    setBands(data.bands);
    $('poster-status').textContent =
      `Found ${data.bands.length} bands — review the list, then pick a mode.`;
  } catch (e) {
    $('poster-status').textContent = 'Poster OCR failed: ' + e.message;
  } finally {
    $('btn-poster').disabled = false;
  }
};

// ---------------------------------------------------------------------------
// Modes & settings
// ---------------------------------------------------------------------------

async function loadModes() {
  const data = await api('/api/modes');
  modesData = data.modes;
  const list = $('mode-list');
  list.innerHTML = '';
  const trackSelect = $('opt-trackmode');
  trackSelect.innerHTML = '';

  for (const mode of modesData) {
    const card = document.createElement('div');
    card.className = 'mode-card' + (mode.key === selectedMode ? ' selected' : '');
    card.innerHTML = `<div class="mode-name">${mode.key}. ${mode.name}</div>` +
      `<div class="mode-desc">${mode.description}</div>`;
    card.onclick = () => selectMode(mode.key);
    list.appendChild(card);

    if (mode.key !== '9') {
      const opt = document.createElement('option');
      opt.value = mode.key;
      opt.textContent = `${mode.name}`;
      trackSelect.appendChild(opt);
    }
  }
  updateSettingsVisibility();
}

function selectMode(key) {
  selectedMode = key;
  document.querySelectorAll('.mode-card').forEach((c, i) => {
    c.classList.toggle('selected', modesData[i].key === key);
  });
  updateSettingsVisibility();
}

function effectiveTrackMode() {
  return selectedMode === '9' ? $('opt-trackmode').value : selectedMode;
}

function updateSettingsVisibility() {
  const isCluster = selectedMode === '9';
  const track = effectiveTrackMode();

  $('setting-count').classList.toggle('hidden', !COUNT_MODES.includes(track));
  $('setting-albums').classList.toggle('hidden', track !== '3');
  $('setting-era').classList.toggle('hidden', track !== '7');
  $('setting-trackmode').classList.toggle('hidden', !isCluster);
  $('setting-name').classList.toggle('hidden', isCluster);
  $('cluster-card').classList.toggle('hidden', !isCluster);
  $('run-step').textContent = isCluster ? '4' : '3';
  $('btn-run').textContent = isCluster ? '▶ Create genre playlists' : '▶ Create playlist';
}

$('opt-trackmode').onchange = updateSettingsVisibility;

function collectSettings() {
  const track = effectiveTrackMode();
  const settings = {};
  if (COUNT_MODES.includes(track)) settings.count = parseInt($('opt-count').value, 10) || 3;
  if (track === '3') settings.max_albums = parseInt($('opt-albums').value, 10) || 5;
  if (track === '7') {
    settings.start_year = parseInt($('opt-start-year').value, 10);
    settings.end_year = parseInt($('opt-end-year').value, 10);
    if (!settings.start_year || !settings.end_year) {
      throw new Error('Era Picker needs a start and end year.');
    }
  }
  settings.privacy = $('opt-privacy').value;
  return settings;
}

// ---------------------------------------------------------------------------
// Genre clustering (mode 9)
// ---------------------------------------------------------------------------

$('btn-cluster').onclick = async () => {
  const bands = getBands();
  if (!bands.length) return alert('Add some bands first.');
  try {
    await postJSON('/api/cluster', { bands });
    lastClusters = null;
    $('cluster-results').innerHTML = '';
    startPolling();
  } catch (e) {
    alert(e.message);
  }
};

function renderClusters(clusters, done) {
  const wrap = $('cluster-results');
  wrap.innerHTML = '';
  if (!clusters) return;
  for (const genre of Object.keys(clusters).sort()) {
    const bands = clusters[genre];
    const div = document.createElement('div');
    div.className = 'cluster-group';
    div.innerHTML =
      `<div class="genre-title">${genre} (${bands.length} bands)</div>` +
      `<div class="genre-bands">${bands.join(', ')}</div>`;
    if (done) {
      const input = document.createElement('input');
      input.type = 'text';
      input.value = `${genre} Mix`;
      input.dataset.genre = genre;
      input.className = 'cluster-name';
      const label = document.createElement('label');
      label.textContent = 'Playlist name';
      div.appendChild(label);
      div.appendChild(input);
    }
    wrap.appendChild(div);
  }
}

// ---------------------------------------------------------------------------
// Run + job polling
// ---------------------------------------------------------------------------

$('btn-run').onclick = async () => {
  const bands = getBands();
  if (!bands.length) return alert('Add some bands first.');

  let settings;
  try {
    settings = collectSettings();
  } catch (e) {
    return alert(e.message);
  }

  try {
    if (selectedMode === '9') {
      if (!lastClusters) return alert('Run "Preview genre groups" first.');
      const clusters = [];
      document.querySelectorAll('.cluster-name').forEach((input) => {
        clusters.push({
          genre: input.dataset.genre,
          name: input.value.trim(),
          bands: lastClusters[input.dataset.genre],
        });
      });
      await postJSON('/api/run_clusters', {
        clusters,
        track_mode: $('opt-trackmode').value,
        ...settings,
      });
    } else {
      await postJSON('/api/run', {
        bands,
        mode: selectedMode,
        playlist_name: $('opt-name').value.trim(),
        ...settings,
      });
    }
    $('playlist-links').innerHTML = '';
    $('log').textContent = '';
    startPolling();
  } catch (e) {
    alert(e.message);
  }
};

$('btn-cancel').onclick = () => postJSON('/api/job/cancel', {}).catch(() => {});

function startPolling() {
  if (pollTimer) return;
  setRunningUI(true);
  pollTimer = setInterval(pollJob, 1200);
  pollJob();
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
  setRunningUI(false);
}

function setRunningUI(running) {
  $('btn-run').disabled = running;
  $('btn-cluster').disabled = running;
  $('btn-cancel').classList.toggle('hidden', !running);
  $('progress-wrap').classList.toggle('hidden', !running && !$('log').textContent);
}

async function pollJob() {
  let job;
  try {
    job = await api('/api/job');
  } catch (e) {
    return;
  }

  // Progress bar + text
  const pct = job.total ? Math.round((job.current / job.total) * 100) : 0;
  $('progress-wrap').classList.remove('hidden');
  $('progress-bar').style.width = pct + '%';
  const verb = job.kind === 'cluster' ? 'Classifying' : 'Processing';
  if (job.state === 'running') {
    $('progress-text').textContent =
      `${verb} ${job.current}/${job.total}` + (job.band ? ` — ${job.band}` : '');
  }

  // Log
  if (job.log && job.log.length) {
    $('log').classList.remove('hidden');
    $('log').textContent = job.log.join('\n');
    $('log').scrollTop = $('log').scrollHeight;
  }

  // Playlist links
  if (job.playlists && job.playlists.length) {
    $('playlist-links').innerHTML = job.playlists
      .map((p) => `<a href="${p.url}" target="_blank" rel="noopener">🎵 ${p.name} — ${p.url}</a>`)
      .join('');
  }

  // Cluster preview results
  if (job.kind === 'cluster') {
    lastClusters = job.clusters;
    renderClusters(job.clusters, job.state === 'done');
  }

  if (job.state !== 'running') {
    stopPolling();
    if (job.state === 'done') {
      $('progress-text').textContent = job.kind === 'cluster'
        ? 'Genre groups ready — name the playlists below, then create them.'
        : `Done! ${job.total} bands processed, ${job.dupes} duplicate(s) skipped.`;
      $('progress-bar').style.width = '100%';
    } else if (job.state === 'error') {
      $('progress-text').textContent = 'Error: ' + job.error;
    } else if (job.state === 'cancelled') {
      $('progress-text').textContent =
        'Cancelled. Progress was saved — you can resume from the banner after reloading.';
    }
    loadStatus();
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

loadStatus();
loadModes();
loadBandsFile();
