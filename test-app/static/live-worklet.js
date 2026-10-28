// Live comparison: capture the microphone in 20 ms chunks and play one of the returned versions.
const CHUNK_FRAMES = 960;
// Start or restart playback with 40 ms queued so network jitter does not cause gaps.
const START_FRAMES = 1920;
// Drop the oldest audio past 200 ms so listening never drifts behind the speaker.
const MAX_QUEUED_FRAMES = 9600;

class LiveCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunk = new Float32Array(CHUNK_FRAMES);
    this.used = 0;
  }

  process(inputs) {
    const channels = inputs[0];
    if (!channels.length) return true;
    for (let frame = 0; frame < channels[0].length; frame++) {
      let value = 0;
      for (const channel of channels) value += channel[frame] || 0;
      this.chunk[this.used++] = value / channels.length;
      if (this.used === CHUNK_FRAMES) {
        this.port.postMessage(this.chunk, [this.chunk.buffer]);
        this.chunk = new Float32Array(CHUNK_FRAMES);
        this.used = 0;
      }
    }
    return true;
  }
}

class LivePlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    // Each entry holds every version of the same time span, so a switch lands at the same moment.
    this.queue = [];
    this.offset = 0;
    this.queued = 0;
    this.selected = 0;
    this.playing = false;
    this.port.onmessage = (event) => {
      if (event.data.type === 'audio') this.enqueue(event.data.versions);
      else if (event.data.type === 'select') this.selected = event.data.index;
    };
  }

  enqueue(versions) {
    this.queue.push(versions);
    this.queued += versions[0].length;
    while (this.queued - this.offset > MAX_QUEUED_FRAMES && this.queue.length > 1) {
      this.queued -= this.queue.shift()[0].length;
      this.offset = 0;
    }
  }

  process(inputs, outputs) {
    const output = outputs[0][0];
    if (!this.playing && this.queued - this.offset < START_FRAMES) {
      output.fill(0);
      return true;
    }
    this.playing = true;
    let written = 0;
    while (written < output.length && this.queue.length) {
      const source = this.queue[0][this.selected];
      const count = Math.min(output.length - written, source.length - this.offset);
      output.set(source.subarray(this.offset, this.offset + count), written);
      written += count;
      this.offset += count;
      if (this.offset === source.length) {
        this.queued -= this.queue.shift()[0].length;
        this.offset = 0;
      }
    }
    if (written < output.length) {
      output.fill(0, written);
      this.playing = false;
    }
    return true;
  }
}

registerProcessor('live-capture', LiveCapture);
registerProcessor('live-player', LivePlayer);
