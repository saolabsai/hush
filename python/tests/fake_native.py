"""Deterministic protocol fixture; this is NOT a noise suppression model."""

import json
import sys
import time

mode = sys.argv[1]
if sys.argv[2:4] != ["stream", "--atten-lim-db"]:
    sys.exit("invalid command")
if mode == "startup_timeout":
    time.sleep(30)
if mode == "bad_header":
    sys.stdout.buffer.write(b'{"protocol":2}\n')
    sys.stdout.buffer.flush()
    time.sleep(30)
    sys.exit()
if mode == "early_exit":
    sys.stderr.buffer.write(b"model unavailable\n")
    sys.exit(5)

sample_rate = int(sys.argv[sys.argv.index("--sample-rate") + 1]) if "--sample-rate" in sys.argv else 48000
frame_bytes = sample_rate // 100 * 2
delay_samples = sample_rate * 3 // 100 if mode == "delay" else 0
header = dict(protocol=1, sample_rate=sample_rate, frame_samples=sample_rate // 100,
              algorithmic_delay_samples=delay_samples, model="DeepFilterNet3")
if mode == "wrong_rate":
    header.update(sample_rate=48000, frame_samples=480)
sys.stdout.buffer.write(json.dumps(header).encode() + b"\n")
sys.stdout.buffer.flush()
delayed = bytearray(delay_samples * 2)
if mode == "stderr_flood":
    sys.stderr.buffer.write(b"diagnostic\n" * 100_000)
    sys.stderr.buffer.flush()
if mode == "stdout_flood":
    sys.stdout.buffer.write(bytes(10_000_000))
    sys.stdout.buffer.flush()
while True:
    frame = sys.stdin.buffer.read(frame_bytes)
    if not frame:
        break
    if len(frame) != frame_bytes:
        sys.exit("partial frame")
    if mode == "response_timeout":
        time.sleep(30)
    if mode == "short_response":
        sys.stdout.buffer.write(frame[:16])
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(b"native crashed\n")
        sys.exit(7)
    wet = bytes(value ^ 0x55 for value in frame)
    delayed.extend(wet)
    output = delayed[:frame_bytes]
    del delayed[:frame_bytes]
    # Deliberately split writes; readexactly must assemble the frame.
    sys.stdout.buffer.write(output[:100])
    sys.stdout.buffer.flush()
    sys.stdout.buffer.write(output[100:])
    sys.stdout.buffer.flush()
