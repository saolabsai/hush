'use strict';

const $ = (id) => document.getElementById(id);
const ui = Object.fromEntries([
  'engine-dot', 'engine-status', 'error-message', 'record-button', 'record-label',
  'record-hint', 'record-action', 'upload-label', 'file-input', 'demo-button',
  'source-name', 'source-detail', 'source-check', 'attenuation', 'attenuation-value',
  'enhance-button', 'enhance-label', 'enhance-arrow', 'activity', 'original-player',
  'cleaned-player', 'original-waveform', 'cleaned-waveform', 'original-placeholder',
  'cleaned-placeholder', 'original-duration', 'result-pill', 'download-link',
  'switch-original', 'switch-cleaned', 'metrics', 'metric-duration', 'metric-inference',
  'metric-rtf', 'metrics-note', 'profile', 'sample-rate', 'enabled',
  'attenuation-number', 'config-preview', 'copy-config',
].map((id) => [id, $(id)]));

const state = {
  mode: 'idle', ready: false, demoAvailable: false, maxDuration: 60,
  source: null, sourceUrl: null, resultUrl: null, result: null,
  originalPeaks: null, cleanedPeaks: null, recorder: null,
  token: 0, playing: null, animation: null, unloaded: false,
  profiles: {}, profile: null, settings: null, originalSource: null,
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
function updateControls() {
  const idle = state.mode === 'idle';
  const recording = state.mode === 'recording';
  ui['record-button'].disabled = !state.ready || (!idle && !recording);
  ui['demo-button'].disabled = !state.ready || !idle || !state.demoAvailable;
  ui['file-input'].disabled = !state.ready || !idle;
  ui['upload-label'].setAttribute('aria-disabled', String(ui['file-input'].disabled));
  ui.attenuation.disabled = !state.ready || !idle;
  for (const id of ['profile', 'sample-rate', 'enabled', 'attenuation-number', 'copy-config']) ui[id].disabled = !state.ready || !idle;
  let settings = null;
  try { settings = readSettings(); } catch { ui['copy-config'].disabled = true; }
  ui['enhance-button'].disabled = !state.ready || !idle || !state.source || !settings || state.source.sampleRate !== settings.sample_rate;
  ui['switch-original'].disabled = !idle || !state.result;
  ui['switch-cleaned'].disabled = !idle || !state.result;
  ui['record-button'].classList.toggle('recording', recording);
  ui['record-label'].textContent = recording ? 'Recording… click to stop' : state.mode === 'preparing' ? 'Opening your microphone…' : 'Record a clip';
  ui['record-hint'].textContent = recording ? 'Speak naturally. Add a little background noise.' : `Use your microphone · up to ${state.maxDuration} seconds`;
  if (!recording) ui['record-action'].textContent = '↗';
  ui['enhance-label'].textContent = state.mode === 'processing' ? 'Processing audio…' : ui.enabled.checked ? 'Clean up audio' : 'Process without reduction';
  ui['enhance-arrow'].textContent = state.mode === 'processing' ? '···' : '↗';
}
function setMode(mode) { state.mode = mode; updateControls(); }

function validateSettings(settings) {
  if (![8000, 16000, 48000].includes(settings.sample_rate)) throw new Error('Choose 8, 16 or 48 kHz audio.');
  if (!Number.isFinite(settings.atten_lim_db) || settings.atten_lim_db < 0 || settings.atten_lim_db > 100) throw new Error('Noise reduction must be between 0 and 100 dB.');
  if (typeof settings.enabled !== 'boolean') throw new Error('Enabled must be a boolean.');
  return { sample_rate: settings.sample_rate, atten_lim_db: settings.atten_lim_db, enabled: settings.enabled };
}
function readSettings() {
  const limit = ui['attenuation-number'].value.trim();
  if (!limit) throw new Error('Enter a noise reduction limit from 0 to 100 dB.');
  return validateSettings({ sample_rate: Number(ui['sample-rate'].value), atten_lim_db: Number(limit), enabled: ui.enabled.checked });
}
function writeSettings(settings) {
  ui['sample-rate'].value = String(settings.sample_rate);
  ui.attenuation.value = String(settings.atten_lim_db);
  ui['attenuation-number'].value = String(settings.atten_lim_db);
  ui.enabled.checked = settings.enabled;
  ui['attenuation-value'].textContent = `${settings.atten_lim_db} dB`;
}
function renderConfiguration() {
  try { ui['config-preview'].textContent = JSON.stringify(readSettings(), null, 2); }
  catch { ui['config-preview'].textContent = 'Enter valid settings to build the configuration.'; }
}
async function changeSettings() {
  if (state.mode !== 'idle') return;
  clearError(); clearResult();
  let next;
  try { next = readSettings(); }
  catch (error) { renderConfiguration(); updateControls(); showError(error); return; }
  ui['attenuation-value'].textContent = `${next.atten_lim_db} dB`;
  renderConfiguration();
  const previous = state.settings, previousProfile = state.profile;
  if (state.source && state.source.sampleRate !== next.sample_rate) {
    const token = ++state.token;
    ui['original-player'].pause();
    setMode('converting'); message('Preparing the original audio at the selected rate…');
    try {
      if (!state.originalSource) throw new Error('Reload this clip before changing its sample rate.');
      await applyOriginalSource(state.originalSource, next.sample_rate, token);
      if (state.token !== token || state.unloaded) return;
    } catch (error) {
      if (previous) writeSettings(previous);
      ui.profile.value = previousProfile;
      renderConfiguration(); showError(error); message('Your previous clip and settings are still available.');
      return;
    } finally { if (state.token === token && !state.unloaded) setMode('idle'); }
  }
  state.settings = next;
  state.profile = ui.profile.value;
  message(state.source ? 'Settings updated. Process the clip again to compare.' : 'Select a clip to test these settings.');
  updateControls();
}
async function applyProfile() {
  if (state.mode !== 'idle') return;
  const defaults = state.profiles[ui.profile.value];
  if (!defaults) return;
  writeSettings(defaults);
  await changeSettings();
}
async function copyConfiguration() {
  try {
    const text = JSON.stringify(readSettings(), null, 2);
    if (!navigator.clipboard?.writeText) throw new Error('Select and copy the JSON below; clipboard access is unavailable.');
    await navigator.clipboard.writeText(text);
    message('Configuration copied. Use it with HushConfig.from_dict().');
  } catch (error) { showError(error); }
}

function resetPlayer(player) {
  player.pause();
  player.removeAttribute('src');
  player.load();
  player.classList.remove('has-source');
}
function clearResult() {
  resetPlayer(ui['cleaned-player']);
  state.playing = null;
  if (state.resultUrl) URL.revokeObjectURL(state.resultUrl);
  state.resultUrl = null;
  state.result = null;
  state.cleanedPeaks = null;
  ui['download-link'].removeAttribute('href');
  ui['download-link'].removeAttribute('download');
  ui['download-link'].setAttribute('aria-disabled', 'true');
  ui['cleaned-placeholder'].hidden = false;
  ui['result-pill'].textContent = 'WAITING FOR AUDIO';
  ui['result-pill'].classList.remove('done');
  ui.metrics.hidden = true;
  ui['metrics-note'].hidden = true;
  ui['switch-original'].classList.remove('active');
  ui['switch-cleaned'].classList.remove('active');
  drawWaveforms();
}

function createWav(samples, sampleRate = 48000) {
  const bytes = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(bytes);
  function write(offset, text) { for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i)); }
  write(0, 'RIFF'); view.setUint32(4, bytes.byteLength - 8, true); write(8, 'WAVE');
  write(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); write(36, 'data'); view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const sample = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, Math.round(sample < 0 ? sample * 32768 : sample * 32767), true);
  }
  return new Blob([bytes], { type: 'audio/wav' });
}

function parseNativeWav(bytes) {
  const view = new DataView(bytes);
  const text = (offset, size) => String.fromCharCode(...new Uint8Array(bytes, offset, size));
  if (view.byteLength < 12 || text(0, 4) !== 'RIFF' || text(8, 4) !== 'WAVE') return null;
  const end = view.getUint32(4, true) + 8;
  if (end < 12 || end > view.byteLength) throw new Error('The WAV file is incomplete. Please choose a complete recording.');
  let format = null, data = null;
  for (let offset = 12; offset < end;) {
    if (offset + 8 > end) throw new Error('The WAV contains an incomplete chunk header.');
    const kind = text(offset, 4), size = view.getUint32(offset + 4, true);
    const start = offset + 8, next = start + size + (size % 2);
    if (next > end) throw new Error('The WAV contains an incomplete chunk or missing padding.');
    if (kind === 'fmt ') {
      if (format || size < 16) throw new Error('The WAV format chunk is invalid.');
      format = {
        codec: view.getUint16(start, true), channels: view.getUint16(start + 2, true),
        sampleRate: view.getUint32(start + 4, true), byteRate: view.getUint32(start + 8, true),
        blockAlign: view.getUint16(start + 12, true), bits: view.getUint16(start + 14, true),
      };
    } else if (kind === 'data') {
      if (!format) throw new Error('The WAV audio data appears before its format chunk.');
      if (data) throw new Error('The WAV contains multiple audio data chunks.');
      data = { start, size };
    }
    offset = next;
  }
  if (!format || !data) throw new Error('The WAV is missing its format or audio data chunk.');
  if (format.codec !== 1 || format.channels !== 1 || format.bits !== 16 || ![8000, 16000, 48000].includes(format.sampleRate)) return null;
  if (format.blockAlign !== 2 || format.byteRate !== format.sampleRate * 2 || data.size % 2) throw new Error('The WAV contains invalid PCM16 framing.');
  if (!data.size) throw new Error('The audio file is empty. Please choose a clip with some sound.');
  const count = data.size / 2;
  if (count > state.maxDuration * format.sampleRate) throw new Error(`Please choose a clip of ${state.maxDuration} seconds or less.`);
  const samples = new Float32Array(count);
  for (let index = 0; index < count; index++) samples[index] = view.getInt16(data.start + index * 2, true) / 32768;
  return { samples, sampleRate: format.sampleRate, blob: new Blob([bytes], { type: 'audio/wav' }) };
}

async function decodeAudio(bytes) {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) throw new Error('This browser cannot read audio. Open the app in a recent Chrome, Edge, Firefox, or Safari browser.');
  const context = new AudioContextClass();
  try { return await context.decodeAudioData(bytes); }
  catch { throw new Error('This WAV could not be read. Try another uncompressed WAV recording.'); }
  finally { await context.close(); }
}

function readMono(buffer) {
  if (!buffer.length || !Number.isFinite(buffer.duration)) throw new Error('The audio file is empty. Please choose a clip with some sound.');
  if (buffer.duration > state.maxDuration + 0.001) throw new Error(`This clip is ${buffer.duration.toFixed(1)} seconds long. Please choose a clip of ${state.maxDuration} seconds or less.`);
  // Explicit averaging makes multichannel conversion predictable (including stereo).
  const mono = new Float32Array(buffer.length);
  for (let channel = 0; channel < buffer.numberOfChannels; channel++) {
    const samples = buffer.getChannelData(channel);
    for (let frame = 0; frame < mono.length; frame++) mono[frame] += samples[frame] / buffer.numberOfChannels;
  }
  return { samples: mono, sampleRate: buffer.sampleRate, blob: null };
}

async function resampleMono(samples, inputRate, outputRate) {
  if (inputRate === outputRate) return samples;
  const OfflineContext = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  if (!OfflineContext) throw new Error('This browser cannot prepare the audio. Please use a recent Chrome, Edge, Firefox, or Safari browser.');
  const frames = Math.max(1, Math.round(samples.length * outputRate / inputRate));
  const context = new OfflineContext(1, frames, outputRate);
  const input = context.createBuffer(1, samples.length, inputRate);
  input.copyToChannel(samples, 0);
  const source = context.createBufferSource(); source.buffer = input; source.connect(context.destination); source.start();
  return (await context.startRendering()).getChannelData(0).slice();
}

async function applyOriginalSource(original, sampleRate, token) {
  const samples = await resampleMono(original.samples, original.sampleRate, sampleRate);
  if (state.token !== token || state.unloaded) return;
  state.originalSource = original;
  setSource(samples, original.name, original.description, sampleRate, original.sampleRate === sampleRate ? original.blob : null);
}

function makePeaks(samples, bins = 720) {
  const peaks = new Float32Array(bins);
  const width = Math.max(1, samples.length / bins);
  for (let bin = 0; bin < bins; bin++) {
    let peak = 0;
    for (let index = Math.floor(bin * width); index < Math.min(samples.length, Math.ceil((bin + 1) * width)); index++) peak = Math.max(peak, Math.abs(samples[index]));
    peaks[bin] = peak;
  }
  return peaks;
}
function drawWaveform(canvas, peaks, color, player) {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.round(rect.width * ratio), height = Math.round(rect.height * ratio);
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  const context = canvas.getContext('2d');
  context.clearRect(0, 0, width, height);
  context.strokeStyle = color; context.globalAlpha = peaks ? 0.12 : 0.24;
  context.beginPath(); context.moveTo(0, height / 2); context.lineTo(width, height / 2); context.stroke();
  context.globalAlpha = 1;
  if (!peaks) return;
  // A shared fixed amplitude scale keeps original/cleaned waveforms comparable.
  const step = width / peaks.length;
  context.fillStyle = color;
  for (let bin = 0; bin < peaks.length; bin++) {
    const amplitude = Math.max(ratio * 0.6, Math.min(1, peaks[bin]) * height * 0.46);
    context.fillRect(bin * step, height / 2 - amplitude, Math.max(1, step * 0.8), amplitude * 2);
  }
  if (Number.isFinite(player.duration) && player.currentTime > 0) {
    const x = Math.min(1, player.currentTime / player.duration) * width;
    context.fillStyle = '#ffffff70'; context.fillRect(0, 0, x, height);
    context.fillStyle = '#4e5546'; context.fillRect(x, 6 * ratio, ratio, height - 12 * ratio);
  }
}
function drawWaveforms() {
  drawWaveform(ui['original-waveform'], state.originalPeaks, '#8d9d72', ui['original-player']);
  drawWaveform(ui['cleaned-waveform'], state.cleanedPeaks, '#b87b54', ui['cleaned-player']);
}
function animateWaveforms() {
  cancelAnimationFrame(state.animation);
  const draw = () => { drawWaveforms(); if (!ui['original-player'].paused || !ui['cleaned-player'].paused) state.animation = requestAnimationFrame(draw); };
  draw();
}

function setSource(samples, name, description, sampleRate = 48000, rawBlob = null) {
  clearResult();
  resetPlayer(ui['original-player']);
  state.playing = null;
  if (state.sourceUrl) URL.revokeObjectURL(state.sourceUrl);
  state.source = { blob: rawBlob || createWav(samples, sampleRate), duration: samples.length / sampleRate, sampleRate, name };
  state.sourceUrl = URL.createObjectURL(state.source.blob);
  state.originalPeaks = makePeaks(samples);
  ui['original-player'].src = state.sourceUrl;
  ui['original-player'].classList.add('has-source');
  ui['original-placeholder'].hidden = true;
  ui['original-duration'].textContent = timeLabel(state.source.duration);
  ui['source-name'].textContent = name;
  ui['source-name'].title = name;
  ui['source-detail'].textContent = `${state.source.duration.toFixed(1)} sec · ${description} · mono ${sampleRate / 1000} kHz`;
  ui['source-check'].hidden = false;
  message('Ready when you are. Clean up the clip, then compare A and B.');
  drawWaveforms(); updateControls();
}

async function readError(response) {
  try { const json = await response.json(); return json.error || `Request failed (${response.status}).`; }
  catch { return `Request failed (${response.status}). The app server may be unavailable. Please try again.`; }
}

async function loadAudio(source) {
  if (state.mode !== 'idle') return;
  clearError();
  setMode('loading'); message(source === 'demo' ? 'Loading the sample…' : 'Preparing your audio…');
  const token = ++state.token;
  try {
    let bytes, name, description;
    if (source === 'demo') {
      const response = await fetch('/api/demo', { cache: 'no-store' });
      if (!response.ok) throw new Error(await readError(response));
      bytes = await response.arrayBuffer(); name = 'The noisy room'; description = 'sample clip';
    } else {
      if (!/\.wav$/i.test(source.name)) throw new Error('Please select a WAV file.');
      if (source.size > 64 * 1024 * 1024) throw new Error('This file is too large. Choose a WAV file smaller than 64 MB and up to 60 seconds long.');
      bytes = await source.arrayBuffer(); name = source.name; description = 'uploaded clip';
    }
    const native = parseNativeWav(bytes);
    const original = native || readMono(await decodeAudio(bytes));
    await applyOriginalSource({ ...original, name, description }, Number(ui['sample-rate'].value), token);
  } catch (error) { if (state.token === token) { showError(error); message(state.source ? 'Your previous clip is still available.' : 'Choose another clip to get started.'); } }
  finally { if (state.token === token && !state.unloaded) setMode('idle'); ui['file-input'].value = ''; }
}

async function enhance() {
  if (state.mode !== 'idle' || !state.source || !state.ready) return;
  clearError();
  ui['original-player'].pause(); clearResult();
  const token = ++state.token;
  let settings;
  try { settings = readSettings(); }
  catch (error) { showError(error); updateControls(); return; }
  const source = state.source;
  if (source.sampleRate !== settings.sample_rate) { showError('Prepare the clip at the selected sample rate before processing.'); return; }
  setMode('processing');
  message('The audio engine is working. The first run can take a moment to initialize.');
  ui['result-pill'].textContent = 'PROCESSING';
  ui['cleaned-placeholder'].textContent = 'Finding a little more clarity…';
  try {
    const response = await fetch(`/api/enhance?attenuation=${settings.atten_lim_db}&enabled=${settings.enabled}&sample_rate=${settings.sample_rate}`, { method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: source.blob });
    if (!response.ok) throw new Error(await readError(response));
    const bytes = await response.arrayBuffer();
    const decoded = await decodeAudio(bytes.slice(0));
    if (state.token !== token || state.unloaded) return;
    const blob = new Blob([bytes], { type: 'audio/wav' });
    state.resultUrl = URL.createObjectURL(blob);
    state.result = { attenuation: settings.atten_lim_db, enabled: settings.enabled, sampleRate: settings.sample_rate };
    state.cleanedPeaks = makePeaks(decoded.getChannelData(0));
    ui['cleaned-player'].src = state.resultUrl;
    ui['cleaned-player'].classList.add('has-source');
    ui['cleaned-placeholder'].hidden = true;
    ui['download-link'].href = state.resultUrl;
    ui['download-link'].download = `${source.name.replace(/\.wav$/i, '').replace(/[^a-zA-Z0-9._-]+/g, '-').slice(0, 90) || 'recording'}-cleaned.wav`;
    ui['download-link'].setAttribute('aria-disabled', 'false');
    ui['result-pill'].textContent = settings.enabled ? `${settings.atten_lim_db} dB LIMIT` : 'BYPASS';
    ui['result-pill'].classList.add('done');
    const metric = (header) => { const value = response.headers.get(header); return value === null || value.trim() === '' ? null : Number(value); };
    const duration = metric('X-Audio-Duration'), inference = metric('X-Inference-Ms'), rtf = metric('X-Real-Time-Factor'), total = metric('X-Processing-Ms');
    ui['metric-duration'].textContent = duration !== null && Number.isFinite(duration) ? `${duration.toFixed(1)} s` : `${decoded.duration.toFixed(1)} s`;
    ui['metric-inference'].textContent = inference !== null && Number.isFinite(inference) ? `${Math.round(inference)} ms` : '—';
    ui['metric-rtf'].textContent = rtf !== null && Number.isFinite(rtf) ? `${rtf.toFixed(3)}×` : '—';
    ui.metrics.hidden = false;
    ui['metrics-note'].textContent = `Real-time factor is processing time ÷ clip length; below 1× is faster than playback.${total !== null && Number.isFinite(total) ? ` Total engine time, including startup: ${(total / 1000).toFixed(2)} s.` : ''}`;
    ui['metrics-note'].hidden = false;
    message('Your comparison is ready. Switch between A and B at the same point in the clip.');
    drawWaveforms();
  } catch (error) { if (state.token === token) { showError(error); ui['result-pill'].textContent = 'TRY AGAIN'; message('The clip could not be processed. You can try again.'); } }
  finally { ui['cleaned-placeholder'].textContent = 'Your cleaner conversation will appear here.'; if (state.token === token && !state.unloaded) setMode('idle'); }
}

async function releaseRecorder(recorder) {
  clearInterval(recorder.timer);
  clearTimeout(recorder.stopTimeout);
  recorder.stream?.getTracks().forEach((track) => { track.onended = null; track.stop(); });
  try { recorder.node?.disconnect(); recorder.input?.disconnect(); recorder.silent?.disconnect(); } catch { /* Already disconnected. */ }
  if (recorder.context && recorder.context.state !== 'closed') await recorder.context.close();
}

async function finishRecording(recorder) {
  if (recorder.finalizing || state.recorder !== recorder) return;
  recorder.finalizing = true;
  setMode('loading'); message('Preparing your recording…');
  try {
    await releaseRecorder(recorder);
    if (state.unloaded) return;
    const length = recorder.chunks.reduce((sum, chunk) => sum + chunk.length, 0);
    if (!length) throw new Error('No audio was captured. Check your microphone and try recording again.');
    const samples = new Float32Array(length);
    let offset = 0;
    for (const chunk of recorder.chunks) { samples.set(chunk, offset); offset += chunk.length; }
    const buffer = new AudioBuffer({ length, numberOfChannels: 1, sampleRate: recorder.sampleRate });
    buffer.copyToChannel(samples, 0);
    const original = { ...readMono(buffer), name: 'Microphone recording', description: 'recorded just now' };
    await applyOriginalSource(original, Number(ui['sample-rate'].value), state.token);
  } catch (error) { showError(error); message('The recording could not be prepared. Please try again.'); }
  finally { if (state.recorder === recorder) state.recorder = null; if (!state.unloaded) setMode('idle'); }
}

function stopRecording() {
  const recorder = state.recorder;
  if (!recorder || recorder.stopping || recorder.finalizing) return;
  recorder.stopping = true;
  clearInterval(recorder.timer);
  setMode('loading'); message('Finishing the recording…');
  // The worklet flushes its partial chunk before acknowledging stop.
  recorder.node.port.postMessage({ type: 'stop' });
  recorder.stream.getTracks().forEach((track) => { track.onended = null; track.stop(); });
  recorder.stopTimeout = setTimeout(async () => {
    if (recorder.finalizing || state.recorder !== recorder) return;
    recorder.finalizing = true;
    await releaseRecorder(recorder);
    state.recorder = null;
    showError('The microphone stopped responding before the recording was complete. Please try again.');
    message('Recording stopped.'); setMode('idle');
  }, 3000);
}

async function startRecording() {
  if (state.mode !== 'idle' || !state.ready) return;
  clearError();
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
    showError('Microphone recording needs a recent browser on HTTPS or localhost. You can still upload a WAV file.'); return;
  }
  ui['original-player'].pause(); ui['cleaned-player'].pause();
  setMode('preparing'); message('Allow microphone access in your browser to start recording.');
  const recorder = { chunks: [], stream: null, context: null, node: null, input: null, silent: null, timer: null, stopTimeout: null, stopping: false, finalizing: false };
  state.recorder = recorder;
  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    recorder.context = new AudioContextClass({ sampleRate: 48000 });
    // Resume in the click gesture, before waiting for the permissions prompt.
    await recorder.context.resume();
    recorder.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, noiseSuppression: false, echoCancellation: false, autoGainControl: false } });
    if (state.unloaded) { await releaseRecorder(recorder); return; }
    await recorder.context.audioWorklet.addModule('/recorder-worklet.js');
    recorder.sampleRate = recorder.context.sampleRate;
    recorder.node = new AudioWorkletNode(recorder.context, 'raw-recorder', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], processorOptions: { maxFrames: recorder.sampleRate * state.maxDuration } });
    recorder.node.port.onmessage = (event) => {
      if (event.data.type === 'data') recorder.chunks.push(event.data.chunk);
      if (event.data.type === 'stopped') finishRecording(recorder);
    };
    recorder.node.onprocessorerror = async () => {
      if (recorder.finalizing) return;
      recorder.finalizing = true;
      await releaseRecorder(recorder);
      state.recorder = null;
      showError('The microphone recorder encountered an error. Please try again.');
      message('Recording stopped.'); setMode('idle');
    };
    recorder.input = recorder.context.createMediaStreamSource(recorder.stream);
    recorder.silent = recorder.context.createGain(); recorder.silent.gain.value = 0;
    recorder.input.connect(recorder.node); recorder.node.connect(recorder.silent); recorder.silent.connect(recorder.context.destination);
    recorder.stream.getTracks().forEach((track) => { track.onended = () => stopRecording(); });
    recorder.start = performance.now();
    setMode('recording'); ui['record-action'].textContent = '0:00';
    message('Requesting raw microphone audio. Stop when you have a good sample.');
    recorder.timer = setInterval(() => {
      const seconds = (performance.now() - recorder.start) / 1000;
      ui['record-action'].textContent = timeLabel(Math.min(state.maxDuration, seconds));
      if (seconds >= state.maxDuration + 1) stopRecording();
    }, 150);
  } catch (error) {
    await releaseRecorder(recorder);
    if (state.recorder === recorder) state.recorder = null;
    if (state.unloaded) return;
    const details = { NotAllowedError: 'Microphone access was not allowed. Enable it in your browser, or upload a WAV file.', NotFoundError: 'No microphone was found. Connect a microphone, or upload a WAV file.', NotReadableError: 'The microphone could not be opened. Check whether another app is using it and try again.' };
    showError(details[error.name] || `Recording could not start: ${error.message}`);
    message(state.source ? 'Your previous clip is still available.' : 'You can also upload a WAV or try the sample.');
    setMode('idle');
  }
}

function switchPlayback(target) {
  if (!state.result || state.mode !== 'idle') return;
  const name = target === ui['original-player'] ? 'original' : 'cleaned';
  if (state.playing === name) return;
  const other = target === ui['original-player'] ? ui['cleaned-player'] : ui['original-player'];
  const wasPlaying = !other.paused || !target.paused;
  const position = other.currentTime;
  other.pause();
  if (Number.isFinite(target.duration)) target.currentTime = Math.min(position, target.duration);
  state.playing = name;
  ui['switch-original'].classList.toggle('active', target === ui['original-player']);
  ui['switch-cleaned'].classList.toggle('active', target === ui['cleaned-player']);
  if (wasPlaying) target.play().catch((error) => showError(`Playback could not start: ${error.message}`));
  else { target.focus(); drawWaveforms(); }
}

ui['demo-button'].addEventListener('click', () => loadAudio('demo'));
ui['file-input'].addEventListener('change', () => { if (ui['file-input'].files[0]) loadAudio(ui['file-input'].files[0]); });
ui['enhance-button'].addEventListener('click', enhance);
ui['record-button'].addEventListener('click', () => state.mode === 'recording' ? stopRecording() : startRecording());
ui.profile.addEventListener('change', applyProfile);
ui['sample-rate'].addEventListener('change', changeSettings);
ui.enabled.addEventListener('change', changeSettings);
ui.attenuation.addEventListener('input', () => { ui.attenuation.value = String(Math.round(Number(ui.attenuation.value) * 10) / 10); ui['attenuation-number'].value = ui.attenuation.value; changeSettings(); });
ui['attenuation-number'].addEventListener('input', () => { if (ui['attenuation-number'].value.trim()) ui.attenuation.value = ui['attenuation-number'].value; changeSettings(); });
ui['copy-config'].addEventListener('click', copyConfiguration);
ui['switch-original'].addEventListener('click', () => switchPlayback(ui['original-player']));
ui['switch-cleaned'].addEventListener('click', () => switchPlayback(ui['cleaned-player']));
for (const name of ['original', 'cleaned']) {
  const player = ui[`${name}-player`];
  const other = ui[`${name === 'original' ? 'cleaned' : 'original'}-player`];
  player.addEventListener('play', () => { other.pause(); state.playing = name; ui['switch-original'].classList.toggle('active', name === 'original'); ui['switch-cleaned'].classList.toggle('active', name === 'cleaned'); animateWaveforms(); });
  for (const event of ['pause', 'ended', 'seeked', 'loadedmetadata']) player.addEventListener(event, drawWaveforms);
}
window.addEventListener('resize', drawWaveforms);
window.addEventListener('pagehide', () => {
  state.unloaded = true; state.token++;
  if (state.recorder) releaseRecorder(state.recorder);
  if (state.sourceUrl) URL.revokeObjectURL(state.sourceUrl);
  if (state.resultUrl) URL.revokeObjectURL(state.resultUrl);
  cancelAnimationFrame(state.animation);
});

async function initialize() {
  drawWaveforms();
  try {
    const response = await fetch('/api/status', { cache: 'no-store' });
    if (!response.ok) throw new Error(await readError(response));
    const status = await response.json();
    state.ready = Boolean(status.ready);
    state.demoAvailable = Boolean(status.demo_available);
    for (const name of ['phone_call', 'video_interview']) {
      if (!status.profiles || !status.profiles[name]) throw new Error('The settings catalog is unavailable. Update and restart the lab server.');
      state.profiles[name] = validateSettings(status.profiles[name]);
    }
    ui.profile.value = 'phone_call';
    writeSettings(state.profiles.phone_call);
    state.profile = 'phone_call'; state.settings = { ...state.profiles.phone_call };
    renderConfiguration();
    if (Number.isFinite(status.max_duration_seconds)) state.maxDuration = status.max_duration_seconds;
    ui['engine-dot'].className = `status-dot ${state.ready ? 'ready' : 'error'}`;
    ui['engine-status'].textContent = state.ready ? 'Engine ready for a listening test' : 'Engine is not ready';
    if (!state.ready) { showError(status.error || 'The noise suppression engine is unavailable. Please try again later.'); message('Refresh once the engine is ready.'); }
  } catch (error) {
    state.ready = false;
    ui['engine-dot'].className = 'status-dot error'; ui['engine-status'].textContent = 'Cannot connect to the audio engine';
    showError(error); message('Check your connection, then refresh this page.');
  }
  updateControls();
}
const initialized = initialize();
