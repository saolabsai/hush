# Hush Python integration

The compatible package name is `sao-noise`, with imports under `sao_noise`.
Inference runs in a persistent Rust `sao-denoise` subprocess. Build the engine
first; Python does not download or load a separate neural model.

From the product directory:

```console
python -m pip install -e "./python[pipecat]"
```

Omit `[pipecat]` for the standalone asynchronous client.

```python
from pipecat.pipeline.worker import PipelineParams
from pipecat.transports.base_transport import TransportParams
from sao_noise import HushConfig
from sao_noise.pipecat import DeepFilterNetFilter

config = HushConfig.for_profile("video_interview", atten_lim_db=18)
noise_filter = DeepFilterNetFilter(
    executable="/absolute/path/to/sao-denoise",  # .exe on Windows
    config=config,
)
transport_params = TransportParams(
    audio_in_enabled=True,
    audio_in_sample_rate=config.sample_rate,
    audio_in_channels=1,
    audio_in_filter=noise_filter,
)
pipeline_params = PipelineParams(audio_in_sample_rate=config.sample_rate)
```

Use these input settings with your transport's parameters and pass
`pipeline_params` to its pipeline worker. The transport must deliver decoded
mono PCM16 LE to the filter. See the [telephony guide](../docs/telephony.md) for
codec boundaries. Use `HushConfig.for_profile("phone_call")` for an 8 kHz
starting point; override `sample_rate=16000` for a decoded wideband source.
Both presets start at 24 dB and `enabled=True`; these are convenience defaults,
not quality-tuned recommendations. See the
[configuration guide](../docs/configuration.md) and checked-in
[phone](../examples/phone-call.json) and
[interview](../examples/video-interview.json) JSON settings.

For direct use, pass `config=config` to `RustDenoiser(...)`, then call
`await start()` without repeating the rate. Legacy `start(8000)`,
`start(16000)` and `start(48000)` calls remain supported without a config.
With an explicit config, a mismatched `start(sample_rate)` raises an error.
An explicit `atten_lim_db` constructor argument overrides config strength.
Call `await process(pcm_bytes)` and use `await stop()` in `finally` on
`sao_noise.RustDenoiser`. Use a separate instance
per input stream. Input must be mono PCM16 little-endian at the selected rate;
raw bytes cannot reveal stereo, so channel count must be configured upstream.
An 8/16 kHz stream is converted to the pretrained model's 48 kHz rate and back
inside the native worker, preserving state across calls.

## Framing and controls

Input may contain any number of complete two-byte samples. Partial 80-sample
hops at 8 kHz, 160-sample hops at 16 kHz or 480-sample hops at 48 kHz are
buffered; an output call can return fewer bytes than its input, or none.
Up to four available hops are exchanged per bounded IPC call,
without waiting to fill a batch. No audio is silently dropped to match lengths.

`config.frame_samples` and `config.frame_bytes` describe a native hop.
`config.packet_bytes(20)` sizes 20 ms of decoded mono PCM16; this helper does
not change your transport's packetization.

Startup metadata reports the selected sample rate, frame length and total
signal delay in that rate's samples. At 48 kHz the delay is 1,440 samples /
30 ms; at 8 kHz it is 304 samples / 38 ms, and at 16 kHz it is 544 samples /
34 ms. Lower-rate values include conversion delay.
Frame acquisition adds up to 10 ms; IPC, processing, scheduling and transport
add their own delay.
Initial delayed samples remain in live output.

Set `enabled=False` in the config to start bypassed. Send
`FilterUpdateSettingsFrame(settings={"enabled": False})` upstream to the
Pipecat input transport to select delayed dry audio; use `True` to restore it.
Values must be actual booleans. `FilterEnableFrame` is also understood when
calling the filter directly, but Pipecat 1.12 does not route it to the input
filter. The integration baseline is Pipecat 1.12.0.

The model continues processing while disabled, so bypass does not save CPU.
Control modes follow input samples through the delay; toggles preserve order
and buffered state. Instant wet/dry switching has no crossfade and may click.
Stop the stream and create a client with a new config to change rate or strength.

`stop()` discards incomplete input and delayed tail because Pipecat cleanup
cannot emit audio. Preserve trailing silence for at least the reported signal
delay plus one hop when the last speech samples matter: allow 50 ms at 8/16 kHz
or 40 ms at 48 kHz. Restart creates fresh state and restores the configured
initial enabled state (the legacy default is `True`).

## Failures and lifecycle

Startup timeout defaults to 60 s; each bounded exchange to 5 s; shutdown to 2 s
before forced termination. These values are configurable. Stdout carries only
startup JSON followed by PCM. Stderr is continuously drained with its latest
8 KiB retained for bounded diagnostics. No shell launches the worker.

Cancellation or stream failure closes and invalidates the native session;
explicitly restart before submitting more audio. Unreturned audio from a
cancelled call is discarded. Errors never silently bypass enhancement. Calls
before startup, duplicate startup and odd-length PCM raise errors.

On Windows use Python's default Proactor event loop; Selector loops do not
support the required subprocess transport.

## Tests

From the product directory:

```console
python -m unittest discover -s python/tests -v
```

Set `SAO_DENOISE_EXE` to the absolute built binary and install `[pipecat]` to
include actual-model tests. A configured invalid path fails rather than skips.
The protocol fixture transforms bytes and is not a neural quality simulation.
Native checks verify real model framing, silence, delayed toggles, restart
determinism and streaming/WAV PCM parity. These are engineering regressions,
not subjective quality or overlapping-speaker isolation tests.
