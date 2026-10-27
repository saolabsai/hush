# Phone and interview settings

Choose a preset and override only what your transport needs:

```python
from sao_noise import HushConfig

config = HushConfig.for_profile("phone_call", sample_rate=16000, atten_lim_db=30)
```

`phone_call` starts at 8 kHz; `video_interview` starts at 48 kHz. Both default
to 24 dB reduction and `enabled=True`. These are convenience defaults, not
quality-tuned recommendations. Use the rate of the decoded audio your
transport actually supplies.

| Setting | Accepted values | Effect |
| --- | --- | --- |
| `sample_rate` | `8000`, `16000`, `48000` | Input and output PCM rate |
| `atten_lim_db` | Finite number from `0` to `100` | Maximum reduction; `0` selects delayed dry audio |
| `enabled` | `True` or `False` | Initial enhancement/bypass state |

Audio must be mono, signed PCM16 little-endian. The model always runs at
48 kHz internally; 8/16 kHz streams use stateful conversion and keep their
source rate. Use a separate filter instance per caller or interview participant.

## JSON

Start with [phone-call.json](../examples/phone-call.json) or
[video-interview.json](../examples/video-interview.json):

```json
{
  "profile": "phone_call",
  "sample_rate": 16000,
  "atten_lim_db": 30,
  "enabled": true
}
```

```python
config = HushConfig.from_json("examples/phone-call.json")
settings = config.to_dict()
```

`from_dict(settings)` accepts the same fields. An optional `profile` supplies
defaults before explicit values are applied. Unknown fields, unsupported rates,
non-finite/out-of-range strength and non-boolean `enabled` values are rejected.
`to_dict()` returns the three resolved settings, without the preset name.

## Python and Pipecat

```python
from pipecat.pipeline.worker import PipelineParams
from pipecat.transports.base_transport import TransportParams
from sao_noise import HushConfig
from sao_noise.pipecat import DeepFilterNetFilter

config = HushConfig.for_profile("video_interview", atten_lim_db=18)
noise_filter = DeepFilterNetFilter("/absolute/path/to/sao-denoise", config=config)
transport_params = TransportParams(
    audio_in_enabled=True,
    audio_in_sample_rate=config.sample_rate,
    audio_in_channels=1,
    audio_in_filter=noise_filter,
)
pipeline_params = PipelineParams(audio_in_sample_rate=config.sample_rate)
```

Use these values with your transport's parameter class and pass the pipeline
parameters to its worker. A transport/config rate mismatch raises an error;
Hush cannot infer the true rate from raw bytes. This configures an existing
phone/video transport; it does not establish a SIP call or join a meeting.
Pipecat 1.12.0 is the tested integration baseline.

For standalone use, pass `config=config` to `RustDenoiser(...)` and call
`await denoiser.start()` without repeating the rate. Existing strength arguments
and `start(rate)` calls remain supported without a config. An explicit
`atten_lim_db` constructor argument overrides the config's strength.

`frame_samples` and `frame_bytes` are derived 10 ms hop sizes.
`config.packet_bytes(20)` sizes 20 ms of decoded PCM; it does not change your
transport's packet interval. See [telephony](telephony.md) for sizes and delay.

## Changing settings

Set `enabled=False` to begin bypassed. During a stream, standalone clients use
`await denoiser.set_enabled(False)`; Pipecat input transports accept upstream
`FilterUpdateSettingsFrame(settings={"enabled": False})`. Use `True` to
restore enhancement. The model still advances while bypassed, preserving its
state and the audio timeline; bypass does not remove CPU use.

Stop the stream and create a client with a new config to change rate or
strength. Never relabel packets as a different rate. Restart restores the
configured initial enabled state. The [Python guide](../python/README.md)
documents lifecycle, draining and failures.

The browser lab exposes profile, rate, strength and enabled settings for
listening tests. Evaluate representative audio before production use. The
call/video transport owns G.711/Opus decoding and encoding, echo cancellation,
jitter and loss handling. Hush reduces noise and does not guarantee isolation
of other people speaking.
