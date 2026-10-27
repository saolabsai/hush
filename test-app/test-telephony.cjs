'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function app() {
  const elements = new Map();
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
    Blob,
    URL: { createObjectURL: () => 'blob:telephony-test', revokeObjectURL() {} },
    cancelAnimationFrame() {}, requestAnimationFrame() {},
    fetch: async () => ({ ok: true, json: async () => ({ ready: true, demo_available: true }) }),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'static', 'app.js'), 'utf8'), context, { filename: 'app.js' });
  return { context, elements, run: (code) => vm.runInContext(code, context) };
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
  for (const options of [{ rate: 16000 }, { channels: 2 }, { bits: 8 }, { codec: 3 }]) {
    context.payload = arrayBuffer(wav([1, -1], options));
    assert.equal(run('parseNativeWav(payload)'), null);
  }
  context.payload = arrayBuffer(wav([1], { rate: 48000 }));
  assert.equal(run('parseNativeWav(payload).sampleRate'), 48000);
});

test('uploading native phone PCM retains its blob and duration without browser resampling', async () => {
  const { context, elements, run } = app();
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
  run("setSource(new Float32Array(480), 'prior.wav', 'uploaded clip')");
  const bytes = wav([1, -1]).subarray(0, -1);
  context.phoneFile = { name: 'broken.wav', size: bytes.length, arrayBuffer: async () => arrayBuffer(bytes) };
  await run('loadAudio(phoneFile)');
  assert.equal(run('state.source.name'), 'prior.wav');
  assert.equal(elements.get('error-message').hidden, false);
  assert.match(elements.get('error-message').textContent, /incomplete/);
});
