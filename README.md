# Hush

Streaming noise reduction for voice applications, by **SAO Labs**. Hush includes
a Rust engine, asynchronous Python client, Pipecat input filter and browser lab.
It integrates the authors' **pretrained DeepFilterNet3** model.

Existing integration names remain compatible:
the `sao-noise` package, `sao_noise` imports and `sao-denoise` executable.

## Build

Use Python 3.11 or 3.12 and a standard Rust toolchain with a system linker.
Rust 1.85.1 is the tested baseline. Run commands from this product directory:

```console
cargo build --release --locked
python -m venv .venv
python -m pip install -e "./python[pipecat]"
python -m pip install -r test-app/requirements.txt
```

Activate `.venv` before running the Python installation commands. On Windows,
use `.venv\Scripts\Activate.ps1`; on Linux/macOS, use
`source .venv/bin/activate`. Cargo downloads locked dependencies on the first
build. The pinned model is embedded in the executable, so inference works
offline after building. No GPU or cloud provider is needed.

## Try the listening lab

```console
python scripts/generate_fixture.py --output output/demo
python test-app/server.py
```

Open [Hush Lab](http://127.0.0.1:8765). Upload a WAV, record up to 60 seconds,
or use the generated sample, then compare original and cleaned audio. The
sample is an artificial voiced waveform with seeded noise, **not intelligible
speech or a quality benchmark**. No voice recordings are bundled.

[Hush Live](http://127.0.0.1:8765/live.html) compares the original microphone,
RNNoise and Hush while you speak, switching between them at the same moment.
See the [lab guide](test-app/README.md#live-comparison) for its requirements.

The lab finds `target/release/sao-denoise` (`.exe` on Windows). Override it with
`--executable /absolute/path/to/sao-denoise`. See the
[lab guide](test-app/README.md) for privacy, HTTPS and verification details.

## Audio contract

- Mono, 48,000 Hz, signed 16-bit little-endian PCM.
- Native calls process 480 samples / 10 ms. The Python client buffers partial
  hops and exchanges up to four already available hops without waiting for more.
- Signal alignment delay: 1,440 samples / 30 ms, plus up to 10 ms acquisition.
  Inference, IPC, scheduling and transport add latency.
- Each stream owns a separate recurrent state and native process.
- Default attenuation limit is 24 dB; finite values from 0 to 100 are accepted.
  Zero returns delayed dry audio, while the model continues to advance.
- WAV mode pads, drains and compensates delay, preserving input sample count.
  Live streams keep their delay. Feed at least 40 ms trailing silence before
  stopping when the speech tail matters; the stop hook cannot return audio.

This is speech enhancement, not acoustic echo cancellation or target-speaker
separation. It cannot guarantee removal of another person speaking nearby.
Stereo and other sample rates require conversion before the streaming engine.
Apply enhancement before VAD/STT. Evaluate cascaded filters before combining them.

## CLI and Python

```console
target/release/sao-denoise wav input.wav cleaned.wav
target/release/sao-denoise benchmark --seconds 10
```

Add `.exe` on Windows. WAV output must not already exist. The CLI emits a JSON
timing report; throughput excludes capture, transport and real-time scheduling.

```python
from sao_noise import RustDenoiser

denoiser = RustDenoiser("/absolute/path/to/sao-denoise", atten_lim_db=24)
await denoiser.start(48000)
try:
    cleaned_pcm = await denoiser.process(microphone_pcm)
finally:
    await denoiser.stop()
```

Use one instance per stream. For Pipecat, use
`sao_noise.pipecat.DeepFilterNetFilter` as the input transport filter, configure
mono audio at 48 kHz, and set `PipelineParams(audio_in_sample_rate=48000)`.
The [Python guide](python/README.md) documents lifecycle and controls.

## Verify

For the complete CI check, activate your Python environment, install the pinned
development dependencies and use Node.js 22 plus Rust 1.85.1:

```console
python -m pip install -r requirements-dev.txt
python -m pip install -e "./python[pipecat]"
python tools/verify.py
```

The verifier builds the current source and runs every suite with native
integration enabled. Skipped Python tests fail verification. Reports are
written to the ignored `output/verification` directory. Individual checks:

```console
cargo fmt -- --check
cargo test --release --locked
python -m unittest discover -s python/tests -v
python -m unittest discover -s test-app -p test_server.py -v
node --test test-app/test-recorder.cjs test-app/test-playback.cjs test-app/test-live.cjs
```

Set `SAO_DENOISE_EXE` to the absolute newly built binary path before Python
tests. Install the Pipecat extra to include the actual-model integration.
Otherwise those checks skip; a release verification must not accept skips.
HTTP tests generate their own temporary artificial fixture without a dataset
download. Tests cover state, silent gaps, PCM integrity, delayed dry/wet toggles,
tail draining, process failures, request bounds and browser capture/playback.

Optional benchmark scripts accept explicit binaries and input paths:

- `scripts/benchmark_bridge.py`: Pipecat/native IPC timing, synthetic input.
- `scripts/benchmark_speed.py`: repeated paired binaries/packages with PCM parity.
- `scripts/evaluate_demo.py`: mixes a supplied mono 48 kHz WAV with seeded noise;
  requires NumPy (`python -m pip install numpy`). You must have rights to use it.

Artificial fixtures test engineering behavior, not real meeting quality. Before
making performance claims, evaluate representative quiet speech, real noise,
overlapping speakers, downstream transcription and intended concurrent load.

## Repository layout

```text
src/                  Rust engine and PCM/WAV command line
python/src/sao_noise/  Async client and Pipecat input filter
python/tests/         Protocol, lifecycle and native integration checks
test-app/             Browser lab and HTTP/capture/playback tests
scripts/              Fixture generation and repeatable benchmarks
vendor/               Pinned DeepFilterNet runtime, model and notices
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and review guidance and
the [benchmark guide](docs/benchmarking.md) for measurement scope.
[Cue](https://github.com/saolabsai/cue) handles conversation turn detection.

## Model and licensing

[Vendor provenance](vendor/PROVENANCE.md) records the upstream revision, unchanged
model SHA-256 and local streaming/FFT fixes. Upstream notices are retained
verbatim in `vendor/libDF`. Hush does not claim authorship of the model or upstream
code. First-party code is licensed under [MIT](LICENSE); see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for dependency attribution.
