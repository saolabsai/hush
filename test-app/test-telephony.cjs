'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const catalog = {
  ready: true, demo_available: true,
  profiles: {
    phone_call: { sample_rate: 8000, atten_lim_db: 24, enabled: true },
    video_interview: { sample_rate: 48000, atten_lim_db: 24, enabled: true },
  },
};

function app(options = {}) {
  const elements = new Map();
  const rendered = [];
  const copied = [];
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: id === 'attenuation' ? '24' : '', paused: true, currentTime: 0,
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {}, removeAttribute() {}, addEventListener() {},
      pause() {}, load() {},
      getBoundingClientRect: () => ({ width: 100, height: 50 }),
      getContext: () => ({ clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fillRect() {} }),
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: element },
    window: { devicePixelRatio: 1, addEventListener() {} },
    navigator: { clipboard: { writeText: async (text) => copied.push(text) } },
    Blob,
    URL: { createObjectURL: () => 'blob:telephony-test', revokeObjectURL() {} },
    cancelAnimationFrame() {}, requestAnimationFrame() {},
    fetch: options.fetch || (async () => ({ ok: true, json: async () => catalog })),
  });
  if (options.resample) context.window.OfflineAudioContext = class {
    constructor(channels, length, sampleRate) { this.length = length; this.sampleRate = sampleRate; }
    createBuffer(channels, length, sampleRate) {
      return { length, sampleRate, copyToChannel: (samples) => { this.samples = samples.slice(); } };
    }
    createBufferSource() {
      const source = { connect() {}, start: () => { this.source = source.buffer; } };
      return source;
    }
    async startRendering() {
      rendered.push({ inputRate: this.source.sampleRate, inputFrames: this.source.length, outputRate: this.sampleRate, outputFrames: this.length });
      if (options.renderGate) await options.renderGate;
      return { getChannelData: () => new Float32Array(this.length).fill(0.125) };
    }
  };
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'static', 'app.js'), 'utf8'), context, { filename: 'app.js' });
  return { context, elements, rendered, copied, run: (code) => vm.runInContext(code, context) };
}

function chunk(name, bytes) {
  const result = Buffer.alloc(8 + bytes.length + bytes.length % 2);
  result.write(name); result.writeUInt32LE(bytes.length, 4); bytes.copy(result, 8);
  return result;
}

function wav(samples, { rate = 8000, channels = 1, bits = 16, codec = 1, before = [], after = [], pcm = null } = {}) {
  const format = Buffer.alloc(16);
  format.writeUInt16LE(codec); format.writeUInt16LE(channels, 2);
  format.writeUInt32LE(rate, 4); format.writeUInt32LE(rate * channels * bits / 8, 8);
  format.writeUInt16LE(channels * bits / 8, 12); format.writeUInt16LE(bits, 14);
  const audio = pcm || Buffer.alloc(samples.length * 2);
  if (!pcm) samples.forEach((sample, index) => audio.writeInt16LE(sample, index * 2));
  const body = Buffer.concat([...before, chunk('fmt ', format), chunk('data', audio), ...after]);
  const result = Buffer.alloc(12 + body.length);
  result.write('RIFF'); result.writeUInt32LE(result.length - 8, 4); result.write('WAVE', 8);
  body.copy(result, 12);
  return result;
}

function arrayBuffer(bytes) {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

test('8 kHz PCM preserves bytes and reads metadata chunks with odd padding', async () => {
  const { context, run } = app();
  const bytes = wav([-32768, 0, 16384, 32767], {
    before: [chunk('JUNK', Buffer.from([1, 2, 3]))], after: [chunk('LIST', Buffer.from([4]))],
  });
  context.payload = arrayBuffer(bytes);
  const parsed = run('parseNativeWav(payload)');
  assert.equal(parsed.sampleRate, 8000);
  assert.deepEqual(Array.from(parsed.samples), [-1, 0, 0.5, 32767 / 32768]);
  assert.deepEqual(Buffer.from(await parsed.blob.arrayBuffer()), bytes);
});

test('truncated RIFF chunks, missing padding and partial PCM samples are rejected', () => {
  const { context, run } = app();
  const original = wav([1, -1]);
  const oversizedData = Buffer.from(original); oversizedData.writeUInt32LE(100, 40);
  const padded = wav([1], { after: [chunk('JUNK', Buffer.from([7]))] });
  const missingPad = padded.subarray(0, -1); missingPad.writeUInt32LE(missingPad.length - 8, 4);
  for (const bytes of [original.subarray(0, -1), oversizedData, missingPad, wav([], { pcm: Buffer.from([1, 2, 3]) })]) {
    context.payload = arrayBuffer(bytes);
    assert.throws(() => run('parseNativeWav(payload)'), /incomplete|padding|PCM16/);
  }
});

test('duration limits are based on 8 kHz samples and empty clips are rejected', () => {
  const { context, run } = app();
  for (const samples of [[], new Int16Array(8000 * 60 + 1)]) {
    context.payload = arrayBuffer(wav(samples));
    assert.throws(() => run('parseNativeWav(payload)'), /empty|60 seconds/);
  }
  context.payload = arrayBuffer(wav(new Int16Array(8000 * 60)));
  assert.equal(run('parseNativeWav(payload).samples.length'), 8000 * 60);
});

test('other rates and channel formats retain the existing browser conversion path', () => {
  const { context, run } = app();
  for (const options of [{ rate: 44100 }, { channels: 2 }, { bits: 8 }, { codec: 3 }]) {
    context.payload = arrayBuffer(wav([1, -1], options));
    assert.equal(run('parseNativeWav(payload)'), null);
  }
  context.payload = arrayBuffer(wav([1], { rate: 48000 }));
  assert.equal(run('parseNativeWav(payload).sampleRate'), 48000);
  context.payload = arrayBuffer(wav([1], { rate: 16000 }));
  assert.equal(run('parseNativeWav(payload).sampleRate'), 16000);
});

test('uploading native phone PCM retains its blob and duration without browser resampling', async () => {
  const { context, elements, run } = app();
  await run('initialized');
  const bytes = wav(new Int16Array(160).fill(1024));
  context.phoneFile = { name: 'phone.wav', size: bytes.length, arrayBuffer: async () => arrayBuffer(bytes) };
  await run('loadAudio(phoneFile)');
  assert.equal(run('state.source.sampleRate'), 8000);
  assert.equal(run('state.source.duration'), 0.02);
  assert.deepEqual(Buffer.from(await run('state.source.blob.arrayBuffer()')), bytes);
  assert.match(elements.get('source-detail').textContent, /mono 8 kHz/);
});

test('a malformed phone upload leaves the prior source available', async () => {
  const { context, elements, run } = app();
  await run('initialized');
  run("setSource(new Float32Array(480), 'prior.wav', 'uploaded clip')");
  const bytes = wav([1, -1]).subarray(0, -1);
  context.phoneFile = { name: 'broken.wav', size: bytes.length, arrayBuffer: async () => arrayBuffer(bytes) };
  await run('loadAudio(phoneFile)');
  assert.equal(run('state.source.name'), 'prior.wav');
  assert.equal(elements.get('error-message').hidden, false);
  assert.match(elements.get('error-message').textContent, /incomplete/);
});

test('profiles come from the server catalog and precise overrides produce copyable settings', async () => {
  const { run, elements, copied } = app();
  await run('initialized');
  assert.deepEqual(JSON.parse(elements.get('config-preview').textContent), catalog.profiles.phone_call);
  elements.get('profile').value = 'video_interview';
  await run('applyProfile()');
  assert.equal(elements.get('sample-rate').value, '48000');
  elements.get('sample-rate').value = '16000';
  elements.get('attenuation-number').value = '37.25';
  elements.get('enabled').checked = false;
  await run('changeSettings()');
  await run('copyConfiguration()');
  assert.deepEqual(JSON.parse(copied[0]), { sample_rate: 16000, atten_lim_db: 37.25, enabled: false });
  elements.get('profile').value = 'phone_call';
  await run('applyProfile()');
  assert.deepEqual(JSON.parse(elements.get('config-preview').textContent), catalog.profiles.phone_call);
});

test('rate overrides render the cached original at the actual target rate without chained conversion', async () => {
  const { context, run, elements, rendered } = app({ resample: true });
  await run('initialized');
  const bytes = wav(new Int16Array(4800).fill(1024), { rate: 48000 });
  context.sourceFile = { name: 'video.wav', size: bytes.length, arrayBuffer: async () => arrayBuffer(bytes) };
  await run('loadAudio(sourceFile)');
  let prepared = Buffer.from(await run('state.source.blob.arrayBuffer()'));
  assert.equal(prepared.readUInt32LE(24), 8000);
  assert.equal(prepared.readUInt32LE(40), 800 * 2);
  assert.equal(run('state.source.duration'), 0.1);
  run('state.result = { attenuation: 24 };');
  elements.get('sample-rate').value = '16000';
  await run('changeSettings()');
  prepared = Buffer.from(await run('state.source.blob.arrayBuffer()'));
  assert.equal(prepared.readUInt32LE(24), 16000);
  assert.equal(prepared.readUInt32LE(40), 1600 * 2);
  assert.equal(run('state.result'), null);
  assert.deepEqual(rendered, [
    { inputRate: 48000, inputFrames: 4800, outputRate: 8000, outputFrames: 800 },
    { inputRate: 48000, inputFrames: 4800, outputRate: 16000, outputFrames: 1600 },
  ]);
  elements.get('sample-rate').value = '48000';
  await run('changeSettings()');
  assert.deepEqual(Buffer.from(await run('state.source.blob.arrayBuffer()')), bytes);
  assert.equal(rendered.length, 2);
});

test('busy conversion disables settings until the prepared PCM matches the selection', async () => {
  let release;
  const renderGate = new Promise((resolve) => { release = resolve; });
  const { context, run, elements } = app({ resample: true, renderGate });
  await run('initialized');
  const bytes = wav(new Int16Array(160).fill(1024));
  context.sourceFile = { name: 'phone.wav', size: bytes.length, arrayBuffer: async () => arrayBuffer(bytes) };
  await run('loadAudio(sourceFile)');
  elements.get('sample-rate').value = '16000';
  const changing = run('changeSettings()');
  for (const name of ['profile', 'sample-rate', 'enabled', 'attenuation', 'attenuation-number', 'enhance-button']) assert.equal(elements.get(name).disabled, true, name);
  assert.equal(run('state.source.sampleRate'), 8000);
  release(); await changing;
  assert.equal(run('state.source.sampleRate'), 16000);
  assert.equal(elements.get('enhance-button').disabled, false);
});

test('strength and enable changes clear stale results while invalid values block processing', async () => {
  const { context, run, elements } = app();
  await run('initialized');
  const bytes = wav(new Int16Array(160).fill(1024));
  context.sourceFile = { name: 'phone.wav', size: bytes.length, arrayBuffer: async () => arrayBuffer(bytes) };
  await run('loadAudio(sourceFile)');
  run('state.result = { attenuation: 24 };');
  elements.get('attenuation-number').value = '92.5';
  elements.get('enabled').checked = false;
  await run('changeSettings()');
  assert.equal(run('state.result'), null);
  assert.equal(JSON.parse(elements.get('config-preview').textContent).atten_lim_db, 92.5);
  for (const invalid of ['', '-1', '101', 'NaN']) {
    elements.get('attenuation-number').value = invalid;
    await run('changeSettings()');
    assert.equal(elements.get('enhance-button').disabled, true);
    assert.equal(elements.get('copy-config').disabled, true);
  }
});

test('disabled processing sends explicit bypass and retains the configured strength', async () => {
  const calls = [];
  const { context, run, elements } = app({ fetch: async (url, options) => {
    if (url === '/api/status') return { ok: true, json: async () => catalog };
    calls.push({ url, body: options.body });
    return { ok: false, json: async () => ({ error: 'test request captured' }) };
  } });
  await run('initialized');
  const bytes = wav(new Int16Array(160).fill(1024));
  context.sourceFile = { name: 'phone.wav', size: bytes.length, arrayBuffer: async () => arrayBuffer(bytes) };
  await run('loadAudio(sourceFile)');
  elements.get('attenuation-number').value = '73.5';
  elements.get('enabled').checked = false;
  await run('changeSettings()');
  await run('enhance()');
  assert.match(calls[0].url, /attenuation=73\.5&enabled=false&sample_rate=8000$/);
  assert.deepEqual(Buffer.from(await calls[0].body.arrayBuffer()), bytes);
  assert.equal(elements.get('attenuation-number').value, '73.5');
  assert.equal(JSON.parse(elements.get('config-preview').textContent).enabled, false);
});
