# 8 kHz phone audio

Hush accepts mono 8,000 Hz PCM16 little-endian and returns cleaned PCM at the
same rate. It converts each stream to the pretrained DeepFilterNet3 model's
48 kHz rate, enhances it, and converts it back with stateful, band-limited
filters. The model and its weights are unchanged. Upsampling does not restore
frequencies lost by a phone channel.

## Codec boundary

G.711 mu-law and A-law call audio must be decoded to linear PCM16 before it
reaches Hush. Re-encode the cleaned PCM afterward if the outgoing call needs
G.711. Encoded bytes, RTP packets, base64 text and WAV headers are not PCM input
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
from sao_noise.pipecat import DeepFilterNetFilter

noise_filter = DeepFilterNetFilter(
    executable="/absolute/path/to/sao-denoise",
    atten_lim_db=24,
)
transport_params = TransportParams(
    audio_in_enabled=True,
    audio_in_sample_rate=8000,
    audio_in_channels=1,
    audio_in_filter=noise_filter,
)
pipeline_params = PipelineParams(audio_in_sample_rate=8000)
```

Use these input settings in your transport's parameter class and pass the
pipeline parameters to its worker. The transport must decode the call's codec
and deliver mono PCM16 LE at 8 kHz to the input filter. Pipecat starts the
filter at the transport's input rate. Hush leaves outgoing TTS and call encoding
to the application. The tested Pipecat baseline is 1.12.0.

The standalone client uses the same stream contract:

```python
from sao_noise import RustDenoiser

denoiser = RustDenoiser("/absolute/path/to/sao-denoise", atten_lim_db=24)
await denoiser.start(8000)
try:
    cleaned_packet = await denoiser.process(decoded_pcm_packet)
finally:
    await denoiser.stop()
```

Keep the client running across packets; do not create a new model per packet.
At 8 kHz, a 20 ms decoded packet contains 160 samples / 320 PCM bytes. Calls
can contain other complete-sample lengths: the client buffers incomplete
80-sample / 10 ms hops and may return fewer bytes than it receives.

## CLI and timing

From the repository root:

```console
target/release/sao-denoise wav phone-8000.wav cleaned-8000.wav
target/release/sao-denoise benchmark --sample-rate 8000 --seconds 10
```

Use `.exe` on Windows. WAV input must be mono PCM16 at 8 kHz or 48 kHz. WAV
processing compensates total signal delay and preserves the sample rate and
sample count. The output file must not already exist.

Raw native streaming uses `sao-denoise stream --sample-rate 8000`. Its stdout
begins with a startup JSON line, followed by binary PCM. Python handles this
protocol. Streaming output retains the signal delay, including both conversion
filters: 304 samples / 38 ms at 8 kHz (30 ms model delay plus 8 ms conversion).
Read `stream_info.algorithmic_delay_samples` and divide by
`stream_info.sample_rate` to obtain seconds; frame acquisition can add 10 ms,
with inference, IPC and transport latency on top.

Feed continuous audio, including silent intervals. To retain speech at the end
of a live stream, supply trailing silence for at least the reported delay plus
one hop before stopping; allow 50 ms at 8 kHz. The Pipecat stop hook cannot emit
the delayed tail.

## What to evaluate

Hush is intended to reduce background noise such as fans, hum and general
room noise. It cannot guarantee removal of another person talking, replace
acoustic echo cancellation, or separate overlapping speakers. Evaluate quiet
speech and consonants as well as the residual noise.

Compare original and cleaned 8 kHz call recordings you have permission to use.
Measure downstream transcription, speech distortion, clipping, delay and
CPU cost with representative codecs and packet conditions. Synthetic throughput
tests verify engineering behavior; they do not establish phone-call quality.
