'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function app() {
  const elements = new Map();
  function element(id) {
    if (elements.has(id)) return elements.get(id);
    const classes = new Set();
    const attributes = new Map();
    const result = {
      value: id === 'attenuation' ? '24' : '',
      paused: true, currentTime: 0, duration: 10,
      classList: {
        add(value) { classes.add(value); },
        remove(value) { classes.delete(value); },
        toggle(value, enabled) { if (enabled) classes.add(value); else classes.delete(value); },
        contains(value) { return classes.has(value); },
      },
      setAttribute(name, value) { attributes.set(name, value); },
      removeAttribute(name) { attributes.delete(name); },
      addEventListener() {},
      pause() { this.paused = true; },
      play() { this.paused = false; return Promise.resolve(); },
      load() {}, focus() {},
      getBoundingClientRect() { return { width: 100, height: 50 }; },
      getContext() { return { clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fillRect() {} }; },
    };
    elements.set(id, result);
    return result;
  }
  const context = vm.createContext({
    document: { getElementById: element },
    window: { devicePixelRatio: 1, addEventListener() {} },
    Blob,
    URL: { createObjectURL: () => 'blob:local-test', revokeObjectURL() {} },
    cancelAnimationFrame() {}, requestAnimationFrame() {},
    fetch: async () => ({ ok: true, json: async () => ({ ready: true, demo_available: true,
      profiles: { phone_call: { sample_rate: 8000, atten_lim_db: 24, enabled: true }, video_interview: { sample_rate: 48000, atten_lim_db: 24, enabled: true } },
    }) }),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'static', 'app.js'), 'utf8'), context, { filename: 'app.js' });
  return { run: (code) => vm.runInContext(code, context), elements };
}

test('reprocessing clears the prior B selection so the new B button switches and highlights', () => {
  const { run, elements } = app();
  run("state.playing = 'cleaned'; state.result = {}; clearResult(); state.result = {}; ui['original-player'].currentTime = 4; switchPlayback(ui['cleaned-player']);");
  assert.equal(elements.get('cleaned-player').currentTime, 4);
  assert.equal(elements.get('switch-cleaned').classList.contains('active'), true);
  assert.equal(elements.get('switch-original').classList.contains('active'), false);
});

test('loading another source clears prior playback selection and results', () => {
  const { run, elements } = app();
  run("state.playing = 'cleaned'; state.result = {}; setSource(new Float32Array(480), 'new.wav', 'uploaded clip');");
  assert.equal(run('state.playing'), null);
  assert.equal(run('state.result'), null);
  assert.equal(elements.get('switch-cleaned').classList.contains('active'), false);
  assert.equal(elements.get('source-name').textContent, 'new.wav');
});

test('clicking the currently selected A/B button does not jump backward', () => {
  const { run, elements } = app();
  run("state.result = {}; state.playing = 'original'; ui['original-player'].currentTime = 7; ui['cleaned-player'].currentTime = 2; switchPlayback(ui['original-player']);");
  assert.equal(elements.get('original-player').currentTime, 7);
});
