# Phone audio

Hush accepts mono 8,000, 16,000 or 48,000 Hz PCM16 little-endian and returns
cleaned PCM at the same rate. Lower-rate streams use stateful, band-limited
conversion around the pretrained DeepFilterNet3 model's 48 kHz rate.
The model and its weights are unchanged. Upsampling does not restore
frequencies lost by a phone channel.

## Codec boundary

G.711 mu-law and A-law call audio must be decoded to linear PCM16 before it
reaches Hush. Re-encode the cleaned PCM afterward if the outgoing call needs
G.711. The same codec boundary applies to Opus and other formats.
Encoded bytes, RTP packets, base64 text and WAV headers are not PCM input
to `RustDenoiser.process`. Your call transport or codec layer handles those
formats, packet ordering, jitter and loss.

Keep separate state for each caller/channel. A stereo recording needs channel
separation or an explicit mono mix before enhancement. Apply Hush before VAD
and speech recognition.

## Python and Pipecat

Build `sao-denoise` and install `./python[pipecat]` as described in the
[README](../README.md). For an existing Pipecat phone transport:

```python
from pipecat.pipeline.worker import PipelineParams
from pipecat.transports.base_transport import TransportParams
from sao_noise import HushConfig
from sao_noise.pipecat import DeepFilterNetFilter

config = HushConfig.for_profile("phone_call")
noise_filter = DeepFilterNetFilter(
    executable="/absolute/path/to/sao-denoise",
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

Use these input settings in your transport's parameter class and pass the
pipeline parameters to its worker. The transport must decode the call's codec
and deliver mono PCM16 LE at `config.sample_rate` to the input filter. Pipecat starts the
filter at the transport's input rate. A config/rate mismatch raises an error.
For a decoded wideband source, use `sample_rate=16000` in the preset.
Both presets are convenience starting values, not quality-tuned settings;
see [configuration](configuration.md) for JSON and overrides.
Hush leaves outgoing TTS and call encoding to the application. The tested
Pipecat baseline is 1.12.0.

The standalone client uses the same stream contract:

```python
from sao_noise import HushConfig, RustDenoiser

config = HushConfig.for_profile("phone_call")
denoiser = RustDenoiser("/absolute/path/to/sao-denoise", config=config)
await denoiser.start()
try:
    cleaned_packet = await denoiser.process(decoded_pcm_packet)
finally:
    await denoiser.stop()
```

Keep the client running across packets; do not create a new model per packet.
At 8 kHz, a 20 ms decoded packet contains 160 samples / 320 PCM bytes. Calls
can contain other complete-sample lengths: the client buffers incomplete
80-sample / 10 ms hops and may return fewer bytes than it receives.

At 16 kHz, a 20 ms decoded packet has 320 samples / 640 bytes and a native
10 ms hop has 160 samples. At 48 kHz those counts are 960 / 1,920 bytes and
480 samples. `config.packet_bytes(20)` derives the packet byte count; the
transport still chooses packetization.

## CLI and timing

From the repository root:

```console
target/release/sao-denoise wav phone-8000.wav cleaned-8000.wav
target/release/sao-denoise benchmark --profile phone_call --sample-rate 16000 --seconds 10
```

Use `.exe` on Windows. WAV input must be mono PCM16 at 8, 16 or 48 kHz. WAV
processing compensates total signal delay and preserves the sample rate and
sample count. The output file must not already exist.

Raw native streaming uses `sao-denoise stream --profile phone_call`. Explicit
`--sample-rate` and `--atten-lim-db` flags override stream/benchmark profile
defaults. Without a profile, defaults remain 48 kHz and 24 dB. WAV mode always
uses its header's rate; a preset cannot relabel audio bytes. Stream stdout
begins with a startup JSON line, followed by binary PCM. Python handles this
protocol. Streaming output retains the signal delay, including both conversion
filters: 304 samples / 38 ms at 8 kHz and 544 samples / 34 ms at 16 kHz.
The 48 kHz path remains 1,440 samples / 30 ms.
Read `stream_info.algorithmic_delay_samples` and divide by
`stream_info.sample_rate` to obtain seconds; frame acquisition can add 10 ms,
with inference, IPC and transport latency on top.

Feed continuous audio, including silent intervals. To retain speech at the end
of a live stream, supply trailing silence for at least the reported delay plus
one hop before stopping; allow 50 ms at 8/16 kHz or 40 ms at 48 kHz.
The Pipecat stop hook cannot emit the delayed tail.

## What to evaluate

Hush is intended to reduce background noise such as fans, hum and general
room noise. It cannot guarantee removal of another person talking, replace
acoustic echo cancellation, or separate overlapping speakers. Evaluate quiet
speech and consonants as well as the residual noise.

Compare original and cleaned call recordings you have permission to use.
Measure downstream transcription, speech distortion, clipping, delay and
CPU cost with representative codecs and packet conditions. Synthetic throughput
tests verify engineering behavior; they do not establish phone-call quality.
