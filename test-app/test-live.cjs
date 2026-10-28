'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function loadWorklet() {
  const processors = {};
  class AudioWorkletProcessor {
    constructor() {
      this.messages = [];
      this.port = { postMessage: (message) => this.messages.push(message) };
    }
  }
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, 'static', 'live-worklet.js'), 'utf8'),
    { AudioWorkletProcessor, Float32Array, Math, registerProcessor(name, constructor) { processors[name] = constructor; } },
    { filename: 'live-worklet.js' },
  );
  return processors;
}

// One span in every version, told apart by value: original n, RNNoise 1000 + n, Hush 2000 + n.
function span(start, length = 960) {
  return [0, 1000, 2000].map((base) => Float32Array.from({ length }, (_, index) => base + start + index));
}

function makePlayer() {
  const player = new (loadWorklet()['live-player'])();
  return {
    send: (data) => player.port.onmessage({ data }),
    play(blocks = 1) {
      const samples = [];
      for (let block = 0; block < blocks; block++) {
        const output = new Float32Array(128);
        assert.equal(player.process([], [[output]]), true);
        samples.push(...output);
      }
      return samples;
    },
  };
}

test('capture sends 20 ms mono chunks that average the input channels', () => {
  const capture = new (loadWorklet()['live-capture'])();
  for (let block = 0; block < 15; block++) {
    assert.equal(capture.process([[new Float32Array(128).fill(1), new Float32Array(128).fill(3)]]), true);
  }
  assert.equal(capture.messages.length, 2);
  for (const chunk of capture.messages) {
    assert.equal(chunk.length, 960);
    assert.ok(chunk.every((value) => value === 2));
  }
});

test('playback waits for 40 ms of audio, then plays the selected version', () => {
  const player = makePlayer();
  player.send({ type: 'audio', versions: span(0) });
  assert.ok(player.play().every((value) => value === 0), 'started before 40 ms was queued');
  player.send({ type: 'audio', versions: span(960) });
  player.send({ type: 'select', index: 2 });
  assert.deepEqual(player.play().slice(0, 3), [2000, 2001, 2002]);
});

test('switching versions continues at the same moment', () => {
  const player = makePlayer();
  player.send({ type: 'audio', versions: span(0) });
  player.send({ type: 'audio', versions: span(960) });
  assert.deepEqual(player.play().slice(-2), [126, 127]);
  player.send({ type: 'select', index: 1 });
  assert.deepEqual(player.play().slice(0, 2), [1128, 1129]);
});

test('a gap plays silence and waits for 40 ms before resuming', () => {
  const player = makePlayer();
  player.send({ type: 'audio', versions: span(0) });
  player.send({ type: 'audio', versions: span(960) });
  const played = player.play(15);
  assert.equal(played.at(-1), 1919);
  assert.ok(player.play().every((value) => value === 0), 'an empty queue must play silence');
  player.send({ type: 'audio', versions: span(1920) });
  assert.ok(player.play().every((value) => value === 0), 'resumed before 40 ms was queued');
  player.send({ type: 'audio', versions: span(2880) });
  assert.equal(player.play()[0], 1920);
});

test('the queue drops the oldest audio past 200 ms so listening never lags further', () => {
  const player = makePlayer();
  for (let chunk = 0; chunk < 12; chunk++) player.send({ type: 'audio', versions: span(chunk * 960) });
  assert.equal(player.play()[0], 1920);
});
