# Hush Lab

A local browser bench for recording, processing and comparing audio with the
Rust DeepFilterNet3 engine. This is a clip-based lab, not a live virtual microphone.

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

Microphone recordings are prepared as mono PCM16 at 48 kHz. Uploaded mono
PCM16 WAVs at 8 kHz or 48 kHz keep their original rate and PCM bytes when sent
to the engine, including phone clips. Other browser-decodable WAV formats are
converted to mono 48 kHz. G.711 mu-law/A-law call audio must first be decoded
to linear PCM16 WAV; see the [telephony guide](../docs/telephony.md).

WAV output compensates the engine delay and preserves input rate and duration.
Displayed whole-clip processing
includes startup; native inference timing excludes capture/network. There is
one enhancement request at a time. No acoustic echo cancellation or isolation
of an individual person from overlapping voices is provided.

## Data and hosting

Audio uploads only when you select **Clean up audio**. With the default loopback
server, processing happens on your computer. Temporary request files are removed
after processing; no recording history is kept. Browser clips remain in memory
until replaced or the page closes.
Supported PCM WAV uploads send the original file, including any WAV metadata.

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
node --test test-app/test-recorder.cjs test-app/test-playback.cjs test-app/test-telephony.cjs
```

Set `SAO_DENOISE_EXE` to the built absolute binary path. Native checks require
the binary and should not be skipped for release verification. Tests generate
a temporary deterministic sample with Python's standard library, so no external
audio asset is needed. Hardware microphone/permission behavior requires an
interactive check on your own device.
