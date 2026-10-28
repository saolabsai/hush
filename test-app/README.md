# Hush Lab

A local browser bench for recording, processing and comparing audio with the
Rust DeepFilterNet3 engine. The main page works on clips; the
[live comparison](#live-comparison) streams your microphone. Neither is a
virtual microphone for other apps.

## Start

From the product directory, build `cargo build --release --locked`, activate a
Python 3.11/3.12 environment and run:

```console
python -m pip install -r test-app/requirements.txt
python scripts/generate_fixture.py --output output/demo
python test-app/server.py
```

Open [Hush Lab](http://127.0.0.1:8765). `--port 8766` selects another port.
Use `--executable /absolute/path/to/sao-denoise` to override binary discovery.
Windows users can use `./test-app/start.ps1` after creating `.venv` and building.

The generated sample contains an artificial voiced waveform mixed with seeded
noise. It contains no words, cloned voice or downloaded recording. It exercises
the interface and audio processing; it does not establish speech quality.
Alternatively upload your own WAV or explicitly start microphone recording.

## Listen and compare

1. Record up to 60 seconds or upload a short WAV. Browser microphone use requires
   permission and a secure context (localhost or HTTPS).
2. Start with the 24 dB reduction limit. Select **Clean up audio**.
3. Compare original and cleaned playback; listen for missing quiet words,
   consonants, natural tone and residual background sound.
4. Adjust strength and process the same clip again, or download the cleaned WAV.

Use headphones during playback. The browser requests its echo cancellation,
automatic gain and noise suppression off for a useful comparison, but device
drivers and the operating system may still process microphone audio.

The browser converts recordings to mono PCM16 at 48 kHz. WAV output compensates
the engine delay and preserves input duration. Displayed whole-clip processing
includes startup; native inference timing excludes capture/network. There is
one enhancement request at a time. No acoustic echo cancellation or isolation
of an individual person from overlapping voices is provided.

## Live comparison

Open [Hush Live](http://127.0.0.1:8765/live.html) to hear noise reduction while
you speak. The page streams the microphone to the local server, which returns
three versions of the same moment:

- **Original**: the raw microphone, with the browser's noise suppression, echo
  cancellation and gain control requested off.
- **RNNoise**: Pipecat's `RNNoiseFilter`, a common filter in Pipecat voice bots.
  Install it with `python -m pip install "pipecat-ai[rnnoise]==1.12.0"`; without
  it the page compares the original with Hush only.
- **Hush**: DeepFilterNet3 through the `sao_noise` client, so install `./python`
  in the same environment.

Put on headphones, turn on playback, then switch versions with the cards or the
1, 2 and 3 keys while you talk. Original and RNNoise are delayed to Hush's 30 ms,
so a switch lands at the same moment. Each card shows the background level (the
quietest tenth of the last 10 seconds), the voice level (the loudest twentieth),
the background removed compared with the original, and the filter's measured
compute per 10 ms of audio. These are loudness estimates, not speech quality
scores. After stopping, download the last two minutes of each version as
time-aligned WAVs.

One live test runs at a time, for up to 10 minutes. The server holds audio only
while processing it; the downloads are built in the browser.

## Data and hosting

Audio uploads only when you select **Clean up audio**. With the default loopback
server, processing happens on your computer. Temporary request files are removed
after processing; no recording history is kept. Browser clips remain in memory
until replaced or the page closes.

For remote microphone access, place an HTTPS reverse proxy in front of loopback
and pass its exact origin:

```console
python test-app/server.py --public-origin https://audio.example.com
```

The proxy must preserve `Host`; writes require the same HTTPS `Origin`.
Forwarded headers never authorize another host. This lab has no login system:
add access control at the proxy before allowing remote users. When hosted
remotely, audio is sent to that server and passes through your proxy provider.

## Verify

```console
python -m unittest discover -s test-app -p test_server.py -v
node --test test-app/test-recorder.cjs test-app/test-playback.cjs test-app/test-live.cjs
```

Set `SAO_DENOISE_EXE` to the built absolute binary path. Native checks require
the binary and should not be skipped for release verification. Tests generate
a temporary deterministic sample with Python's standard library, so no external
audio asset is needed. Hardware microphone/permission behavior requires an
interactive check on your own device.
