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

1. Choose **Phone call** (8 kHz) or **Video interview** (48 kHz). Both start at
   24 dB with enhancement enabled; they are starting values, not quality-tuned
   presets. Override the rate with 8, 16 or 48 kHz as your integration requires.
2. Record up to 60 seconds or upload a short WAV. Browser microphone use requires
   permission and a secure context (localhost or HTTPS).
3. Adjust the 0–100 dB slider or enter a precise number. Turn enhancement off
   for an unenhanced comparison without losing the configured strength.
   Select **Clean up audio** or **Process without reduction**.
4. Compare original and cleaned playback; listen for missing quiet words,
   consonants, natural tone and residual background sound.
5. Use **Copy JSON** to take the effective settings into your application, or
   download the processed WAV. Every setting change clears the previous result.

Use headphones during playback. The browser requests its echo cancellation,
automatic gain and noise suppression off for a useful comparison, but device
drivers and the operating system may still process microphone audio.

Microphone capture requests mono 48 kHz. The selected processing rate is applied
with the browser's offline audio resampler. Standard mono PCM16 WAVs already at
the chosen 8, 16 or 48 kHz rate keep their original file bytes when sent to the
engine. Other rates or channel formats are decoded, mixed to mono and converted
to the selected rate. Changing the rate always starts from the cached original
clip, avoiding repeated conversion through earlier choices. G.711 mu-law/A-law
call audio must first be decoded to linear PCM16 WAV; see the
[telephony guide](../docs/telephony.md).

WAV output compensates the engine delay and preserves input rate and duration.
Displayed whole-clip processing includes startup; native inference timing
excludes capture/network. There is
one enhancement request at a time. No acoustic echo cancellation or isolation
of an individual person from overlapping voices is provided.

## Use the configuration

The JSON preview contains `sample_rate`, `atten_lim_db` and `enabled`. Profile
defaults come from the same Python `HushConfig` catalog as the integration API.
After installing `./python[pipecat]`, use the copied settings with
`HushConfig.from_dict(settings)` and `DeepFilterNetFilter(config=config)`; see
the [Python guide](../python/README.md). Keep the call or video transport's input
rate consistent with the configuration. Hush is an input filter, not a call
transport, microphone driver or echo canceller.

For HTTP integrations, send a mono PCM16 WAV to `/api/enhance` with
`attenuation=24&enabled=true&sample_rate=8000`, using your chosen values.
The WAV header determines the actual rate. If a `sample_rate` query value is
present, it must match that header. `enabled=false` returns delay-compensated
dry audio while retaining the configured strength in the settings/response.
The `enabled` query accepts exactly `true` or `false`.

## Data and hosting

Audio uploads only when you select **Clean up audio** or **Process without
reduction**. With the default loopback
server, processing happens on your computer. Temporary request files are removed
after processing; no recording history is kept. Browser clips remain in memory
until replaced or the page closes.
Supported PCM WAV uploads at the chosen rate send the original file, including
any WAV metadata. Conversion creates a new PCM WAV without that metadata.

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
