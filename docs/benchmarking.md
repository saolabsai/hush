# Benchmarking Hush

Build the release executable before measuring it. The sample generator uses a
seeded harmonic waveform, noise, hum and taps. It contains no human speech and
is suitable for engineering checks only.

```sh
python scripts/generate_fixture.py --output output/demo
target/release/sao-denoise benchmark --seconds 10
target/release/sao-denoise benchmark --sample-rate 8000 --seconds 10
python scripts/benchmark_bridge.py --executable target/release/sao-denoise --frames 1000
```

Add `.exe` to the binary path on Windows. `benchmark_bridge.py` needs the
Pipecat extra. Native throughput excludes startup and transport; bridge timing
includes the Python filter call, IPC and inference, but not capture, network
or real-time scheduling. A real-time factor below one means the measured work
was faster than the audio duration under that test's conditions.

`benchmark_speed.py --help` lists options for repeated paired measurements of
two binaries and Python packages. Use the same mono PCM16 48 kHz WAV and compare
raw and aligned PCM hashes when a change should preserve output.

`evaluate_demo.py` mixes a supplied mono 48 kHz WAV with seeded interference.
It needs NumPy. Use only audio you have permission to process and do not commit
recordings or generated output.

Record the CPU, operating system, build profile, thread settings, packet size,
attenuation limit, warmup and number of runs. Report startup separately from
steady-state processing and use distributions rather than a single best run.
The 48 kHz engine's 30 ms signal delay and up to 10 ms frame acquisition remain
even when inference is fast. At 8 kHz, include the reported rate-converter delay
as well. Native 8 kHz benchmark timing includes the conversion and enhancement
work; it does not establish performance under real phone-network jitter or loss.

The fixture generator and Python benchmark scripts above remain 48 kHz tools.
Use the native `benchmark --sample-rate 8000` command for 8 kHz compute timing,
and the [telephony guide](telephony.md) for the phone-audio contract.

Representative speech and noise, listening tests, downstream transcription
accuracy and intended concurrent load are needed before making quality or
deployment claims. Hush is not a target-speaker separator.
