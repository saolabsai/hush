'use strict';

const $ = (id) => document.getElementById(id);
const NAMES = ['original', 'rnnoise', 'hush'];
const ui = Object.fromEntries([
  'engine-dot', 'engine-status', 'error-message', 'live-button', 'live-label', 'live-hint', 'live-action',
  'listen', 'attenuation', 'attenuation-value', 'activity', 'level-chart',
  'removed-rnnoise', 'removed-hush', 'compute-rnnoise', 'compute-hush',
  ...NAMES.flatMap((name) => [`version-${name}`, `level-${name}`, `floor-${name}`, `voice-${name}`,
    `delay-${name}`, `download-${name}`]),
].map((id) => [id, $(id)]));

const COLORS = { original: '#8f9486', rnnoise: '#c08a3e', hush: '#477864' };
const HISTORY_MS = 10_000;
const RECORD_FRAMES = 120 * 48_000;
const SILENCE_DB = -90;

const state = {
  ready: false, rnnoise: false, maxSeconds: 600, live: null, last: null, selected: 'hush',
  downloads: [], animation: null, unloaded: false,
};

function message(text) { ui.activity.textContent = text; }
function clearError() { ui['error-message'].hidden = true; ui['error-message'].textContent = ''; }
function showError(error) {
  const text = error instanceof Error ? error.message : String(error);
  ui['error-message'].textContent = text;
  ui['error-message'].hidden = false;
}
function timeLabel(seconds) {
  const whole = Math.floor(Math.max(0, seconds || 0));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}
function levelDb(samples) {
  let energy = 0;
  for (const value of samples) energy += value * value;
  return Math.max(SILENCE_DB, 10 * Math.log10(energy / Math.max(1, samples.length) + 1e-12));
}
function percentile(values, fraction) {
  if (!values.length) return null;
  const sorted = Float64Array.from(values).sort();
  return sorted[Math.min(sorted.length - 1, Math.floor(fraction * sorted.length))];
}
function dbLabel(value) { return value === null ? '—' : `${Math.round(value)} dB`; }

function available(name) { return name !== 'rnnoise' || state.rnnoise; }

function updateControls() {
  const live = state.live;
  const running = Boolean(live?.versions);
  ui['live-button'].disabled = !state.ready || Boolean(live && !running);
  ui['live-button'].classList.toggle('recording', Boolean(live));
  ui['live-label'].textContent = !live ? 'Start live test' : running ? 'Stop live test' : 'Starting…';
  ui['live-hint'].textContent = running ? 'Speak with real noise around you' : 'Up to 10 minutes · nothing is stored on the server';
  if (!live) ui['live-action'].textContent = '↗';
  ui.attenuation.disabled = !state.ready || Boolean(live);
  ui.listen.disabled = !state.ready;
  for (const name of NAMES) {
    const card = ui[`version-${name}`];
    card.disabled = !state.ready || !available(name);
    card.setAttribute('aria-pressed', String(state.selected === name));
    card.classList.toggle('active', state.selected === name);
  }
}

function select(name) {
  if (!available(name)) return;
  state.selected = name;
  const live = state.live;
  if (live?.versions) live.player.port.postMessage({ type: 'select', index: live.versions.indexOf(name) });
  updateControls();
}

function wavBlob(chunks) {
  const frames = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const header = new DataView(new ArrayBuffer(44));
  function write(offset, text) { for (let i = 0; i < text.length; i++) header.setUint8(offset + i, text.charCodeAt(i)); }
  write(0, 'RIFF'); header.setUint32(4, 36 + frames * 2, true); write(8, 'WAVE');
  write(12, 'fmt '); header.setUint32(16, 16, true); header.setUint16(20, 1, true);
  header.setUint16(22, 1, true); header.setUint32(24, 48000, true); header.setUint32(28, 96000, true);
  header.setUint16(32, 2, true); header.setUint16(34, 16, true); write(36, 'data'); header.setUint32(40, frames * 2, true);
  return new Blob([header.buffer, ...chunks], { type: 'audio/wav' });
}

function clearDownloads() {
  state.downloads.forEach((url) => URL.revokeObjectURL(url));
  state.downloads = [];
  for (const name of NAMES) {
    const link = ui[`download-${name}`];
    link.removeAttribute('href');
    link.setAttribute('aria-disabled', 'true');
  }
}

function prepareDownloads(live) {
  for (const name of live.versions || []) {
    if (!live.recordings[name].length) continue;
    const url = URL.createObjectURL(wavBlob(live.recordings[name]));
    state.downloads.push(url);
    const link = ui[`download-${name}`];
    link.href = url;
    link.download = `hush-live-${name}.wav`;
    link.setAttribute('aria-disabled', 'false');
  }
}

function record(live, name, pcm) {
  const chunks = live.recordings[name];
  chunks.push(pcm.slice());
  live.recorded[name] += pcm.length;
  while (live.recorded[name] - chunks[0].length >= RECORD_FRAMES) live.recorded[name] -= chunks.shift().length;
}

function handleAudio(live, buffer) {
  if (!live.versions) return;
  const now = performance.now();
  const span = buffer.byteLength / live.versions.length / 2;
  const floats = live.versions.map((name, index) => {
    const pcm = new Int16Array(buffer, index * span * 2, span);
    record(live, name, pcm);
    const samples = Float32Array.from(pcm, (value) => value / 32768);
    const history = live.levels[name];
    history.push([now, levelDb(samples)]);
    while (history.length && history[0][0] < now - HISTORY_MS) history.shift();
    return samples;
  });
  live.player.port.postMessage({ type: 'audio', versions: floats }, floats.map((samples) => samples.buffer));
}

function handleEvent(live, event) {
  if (event.type === 'ready') {
    live.versions = event.versions;
    for (const name of live.versions) { live.levels[name] = []; live.recordings[name] = []; live.recorded[name] = 0; }
    for (const [name, delay] of Object.entries(event.delay_ms)) {
      if (live.versions.includes(name)) ui[`delay-${name}`].textContent = `${Math.round(delay)} ms`;
    }
    live.started = performance.now();
    select(live.versions.includes(state.selected) ? state.selected : 'hush');
    message(ui.listen.checked ? 'Live. Switch versions with 1, 2 and 3 while you talk.'
      : 'Live, with playback off. Put headphones on, then turn on playback to listen.');
    updateControls();
    drawLoop();
  } else if (event.type === 'stats') {
    for (const [name, value] of Object.entries(event.compute_ms_per_10ms)) {
      if (ui[`compute-${name}`]) ui[`compute-${name}`].textContent = `${value.toFixed(2)} ms`;
    }
  } else if (event.type === 'error') {
    live.error = event.message;
    showError(event.message);
  }
}

function sendChunk(live, chunk) {
  if (!live.versions || live.socket.readyState !== WebSocket.OPEN) return;
  const pcm = Int16Array.from(chunk, (value) => Math.round(Math.max(-1, Math.min(1, value)) * 32767));
  live.socket.send(pcm.buffer);
}

function drawChart(live) {
  const canvas = ui['level-chart'];
  const box = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(box.width * ratio));
  const height = Math.max(1, Math.round(box.height * ratio));
  // Resizing reallocates the canvas, so only resize when the layout changed.
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  const context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  if (!live?.versions) return;
  // A stopped test stays on screen, frozen at the moment it ended.
  const now = live.endedAt ?? performance.now();
  // Map -80..0 dBFS onto the chart height; quieter than -80 sits on the floor.
  const y = (db) => canvas.height * Math.min(1, Math.max(0, -db / 80));
  context.lineWidth = 1.5 * ratio;
  for (const name of live.versions) {
    context.strokeStyle = COLORS[name];
    context.beginPath();
    live.levels[name].forEach(([time, db], index) => {
      const x = canvas.width * (1 - (now - time) / HISTORY_MS);
      if (index) context.lineTo(x, y(db)); else context.moveTo(x, y(db));
    });
    context.stroke();
  }
}

function updateMeters(live) {
  const floors = {};
  for (const name of live.versions) {
    const levels = live.levels[name].map(([, db]) => db);
    const latest = levels.at(-1) ?? SILENCE_DB;
    ui[`level-${name}`].style.width = `${Math.max(0, Math.min(100, (latest + 70) / 70 * 100))}%`;
    floors[name] = percentile(levels, 0.1);
    ui[`floor-${name}`].textContent = dbLabel(floors[name]);
    ui[`voice-${name}`].textContent = dbLabel(percentile(levels, 0.95));
  }
  for (const name of live.versions.slice(1)) {
    const removed = floors.original === null || floors[name] === null ? null : floors.original - floors[name];
    ui[`removed-${name}`].textContent = removed === null ? '—' : `${removed.toFixed(0)} dB`;
  }
  const seconds = (performance.now() - live.started) / 1000;
  ui['live-action'].textContent = timeLabel(seconds);
  if (seconds >= state.maxSeconds) stopLive(live, 'The live test reached its 10-minute limit.');
}

function drawLoop(now = performance.now()) {
  cancelAnimationFrame(state.animation);
  const live = state.live;
  if (!live?.versions) { drawChart(null); return; }
  // Numbers refresh four times a second, which is readable and keeps the page light.
  if (now - live.metersAt >= 250) { live.metersAt = now; updateMeters(live); }
  drawChart(live);
  state.animation = requestAnimationFrame(drawLoop);
}

async function release(live) {
  live.stream?.getTracks().forEach((track) => { track.onended = null; track.stop(); });
  try {
    live.input?.disconnect(); live.capture?.disconnect(); live.player?.disconnect();
    live.gain?.disconnect(); live.silent?.disconnect();
  } catch { /* Already disconnected. */ }
  if (live.context && live.context.state !== 'closed') await live.context.close();
}

async function stopLive(live, note) {
  // A late close event from an earlier test must not stop the current one.
  if (!live || live.stopping || state.live !== live) return;
  live.stopping = true;
  live.endedAt = performance.now();
  if (live.socket && live.socket.readyState <= WebSocket.OPEN) live.socket.close();
  await release(live);
  cancelAnimationFrame(state.animation);
  state.live = null;
  if (state.unloaded) return;
  if (live.versions) state.last = live;
  drawChart(state.last);
  prepareDownloads(live);
  updateControls();
  message(live.error ? 'The live test stopped.' : note || 'Stopped. Download each version to compare it later.');
}

async function startLive() {
  if (state.live || !state.ready) return;
  clearError();
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode || !window.WebSocket) {
    showError('The live test needs a recent browser on HTTPS or localhost.'); return;
  }
  clearDownloads();
  state.last = null;
  const live = { versions: null, levels: {}, recordings: {}, recorded: {}, started: 0, metersAt: 0, error: null, stopping: false };
  state.live = live;
  updateControls();
  message('Allow microphone access in your browser to start.');
  try {
    live.context = new AudioContext({ sampleRate: 48000 });
    // Resume in the click gesture, before waiting for the permissions prompt.
    await live.context.resume();
    live.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, noiseSuppression: false, echoCancellation: false, autoGainControl: false } });
    if (state.live !== live) { await release(live); return; }
    await live.context.audioWorklet.addModule('/live-worklet.js');
    live.capture = new AudioWorkletNode(live.context, 'live-capture', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
    live.player = new AudioWorkletNode(live.context, 'live-player', { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] });
    live.input = live.context.createMediaStreamSource(live.stream);
    live.silent = live.context.createGain(); live.silent.gain.value = 0;
    live.gain = live.context.createGain(); live.gain.gain.value = ui.listen.checked ? 1 : 0;
    live.input.connect(live.capture); live.capture.connect(live.silent); live.silent.connect(live.context.destination);
    live.player.connect(live.gain); live.gain.connect(live.context.destination);
    live.capture.port.onmessage = (event) => sendChunk(live, event.data);
    live.stream.getTracks().forEach((track) => { track.onended = () => stopLive(live, 'The microphone stopped.'); });
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    live.socket = new WebSocket(`${scheme}://${location.host}/api/live?attenuation=${encodeURIComponent(ui.attenuation.value)}`);
    live.socket.binaryType = 'arraybuffer';
    live.socket.onmessage = (event) => {
      if (typeof event.data === 'string') handleEvent(live, JSON.parse(event.data));
      else handleAudio(live, event.data);
    };
    live.socket.onclose = () => {
      if (!live.versions && !live.error) showError('The live engine could not be reached. Check the server terminal.');
      stopLive(live);
    };
  } catch (error) {
    await release(live);
    if (state.live === live) state.live = null;
    if (state.unloaded) return;
    const details = { NotAllowedError: 'Microphone access was not allowed. Enable it in your browser and try again.', NotFoundError: 'No microphone was found. Connect one and try again.', NotReadableError: 'The microphone could not be opened. Check whether another app is using it.' };
    showError(details[error.name] || `The live test could not start: ${error.message}`);
    message('Start again when the microphone is ready.');
    updateControls();
  }
}

ui['live-button'].addEventListener('click', () => (state.live ? stopLive(state.live) : startLive()));
ui.listen.addEventListener('change', () => {
  if (state.live?.gain) state.live.gain.gain.value = ui.listen.checked ? 1 : 0;
  if (state.live?.versions) message(ui.listen.checked ? 'Listening. Switch versions with 1, 2 and 3.' : 'Playback is off. Meters keep running.');
});
ui.attenuation.addEventListener('input', () => { ui['attenuation-value'].textContent = `${ui.attenuation.value} dB`; });
for (const name of NAMES) ui[`version-${name}`].addEventListener('click', () => select(name));
document.addEventListener('keydown', (event) => {
  if (event.target instanceof HTMLInputElement || event.metaKey || event.ctrlKey || event.altKey) return;
  const name = NAMES[Number(event.key) - 1];
  if (name && state.ready) select(name);
});
window.addEventListener('resize', () => drawChart(state.live || state.last));
window.addEventListener('pagehide', () => {
  state.unloaded = true;
  stopLive(state.live);
  clearDownloads();
});

async function initialize() {
  drawChart(null);
  try {
    const response = await fetch('/api/status', { cache: 'no-store' });
    const status = await response.json();
    if (!response.ok) throw new Error(status.error || 'The engine status is unavailable.');
    state.ready = Boolean(status.live_available);
    state.rnnoise = Boolean(status.rnnoise_available);
    if (Number.isFinite(status.live_max_seconds)) state.maxSeconds = status.live_max_seconds;
    if (Number.isFinite(status.default_attenuation_db)) { ui.attenuation.value = status.default_attenuation_db; ui['attenuation-value'].textContent = `${ui.attenuation.value} dB`; }
    ui['engine-dot'].className = `status-dot ${state.ready ? 'ready' : 'error'}`;
    ui['engine-status'].textContent = !state.ready ? 'The live comparison is not ready'
      : state.rnnoise ? 'Hush and RNNoise ready for a live test' : 'Hush ready · RNNoise not installed';
    if (!state.ready) showError(status.error || 'Build the Rust engine and install ./python, then refresh.');
    else if (!state.rnnoise) message('Install pipecat-ai[rnnoise] to add RNNoise to the comparison.');
    else message('Start the test, then speak with your usual background noise.');
  } catch (error) {
    ui['engine-dot'].className = 'status-dot error'; ui['engine-status'].textContent = 'Cannot connect to the audio engine';
    showError(error); message('Check that the lab server is running, then refresh.');
  }
  updateControls();
}
initialize();
