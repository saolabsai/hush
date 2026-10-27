"""Real 8 kHz PCM/native/Pipecat checks, not a speech-quality benchmark."""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import math
import os
from pathlib import Path
import random
import struct
import tempfile
import unittest
import wave


NATIVE_EXE = os.environ.get("SAO_DENOISE_EXE")
HAS_PIPECAT = importlib.util.find_spec("pipecat") is not None
SAMPLE_RATE = 8_000
HOP = 80


def telephone_pcm(samples=22 * HOP):
    noise = random.Random(1987)
    values = []
    for index in range(samples):
        if 6 <= index // HOP < 9:
            value = 0
        else:
            phase = 2 * math.pi * index / SAMPLE_RATE
            value = round(6200 * math.sin(230 * phase)
                          + 2400 * math.sin(460 * phase)
                          + 1200 * math.sin(1380 * phase)
                          + 400 * noise.uniform(-1, 1))
        values.append(value)
    return struct.pack(f"<{samples}h", *values)


def pad_for_drain(source, delay_samples):
    samples = len(source) // 2 + delay_samples
    padded_samples = (samples + HOP - 1) // HOP * HOP
    return source + bytes(padded_samples * 2 - len(source))


async def feed(audio_filter, audio, sizes=(2, 320, 18, 146, 2, 960)):
    output = bytearray()
    offset = 0
    for size in itertools.cycle(sizes):
        if offset >= len(audio):
            break
        chunk = audio[offset:offset + size]
        output.extend(await audio_filter.filter(chunk))
        offset += len(chunk)
    return bytes(output)


@unittest.skipUnless(NATIVE_EXE, "set SAO_DENOISE_EXE for actual native integration")
@unittest.skipUnless(HAS_PIPECAT, "install sao-noise[pipecat] for native integration")
class TelephonyNativeTests(unittest.IsolatedAsyncioTestCase):
    async def new_filter(self):
        from sao_noise.pipecat import DeepFilterNetFilter

        executable = str(Path(NATIVE_EXE).resolve())
        self.assertTrue(Path(executable).is_file(), executable)
        audio_filter = DeepFilterNetFilter(executable, atten_lim_db=24)
        self.addAsyncCleanup(audio_filter.stop)
        await audio_filter.start(SAMPLE_RATE)
        info = audio_filter.denoiser.stream_info
        self.assertEqual((info.sample_rate, info.frame_samples, info.model),
                         (SAMPLE_RATE, HOP, "DeepFilterNet3"))
        self.assertGreaterEqual(info.algorithmic_delay_samples, 240)
        return audio_filter, info

    async def test_telephone_packetization_silent_gap_and_reset(self):
        audio_filter, info = await self.new_filter()
        source = telephone_pcm()
        audio = pad_for_drain(source, info.algorithmic_delay_samples)
        baseline = await audio_filter.filter(audio)
        self.assertEqual(len(baseline), len(audio))
        self.assertTrue(any(baseline), "native model returned only zeros")
        delay_bytes = info.algorithmic_delay_samples * 2
        self.assertTrue(any(baseline[len(source):len(source) + delay_bytes]),
                        "trailing silence did not emit the delayed telephone signal")

        await audio_filter.stop()
        await audio_filter.start(SAMPLE_RATE)
        self.assertEqual(await feed(audio_filter, audio), baseline)

        await audio_filter.stop()
        await audio_filter.start(SAMPLE_RATE)
        self.assertEqual(await feed(audio_filter, audio, (320,)), baseline)

    async def test_telephone_bypass_follows_original_sample_positions(self):
        from pipecat.frames.frames import FilterUpdateSettingsFrame

        audio_filter, info = await self.new_filter()
        audio = pad_for_drain(telephone_pcm(), info.algorithmic_delay_samples)
        baseline = await feed(audio_filter, audio)
        delay_bytes = info.algorithmic_delay_samples * 2

        await audio_filter.stop()
        await audio_filter.start(SAMPLE_RATE)
        selected = bytearray()
        modes = bytearray([1]) * delay_bytes
        segments = ((True, 17 * 2), (False, 143 * 2), (True, 201 * 2),
                    (False, 99 * 2), (True, len(audio) - 460 * 2))
        offset = 0
        for enabled, count in segments:
            await audio_filter.process_frame(FilterUpdateSettingsFrame(settings={"enabled": enabled}))
            selected.extend(await feed(audio_filter, audio[offset:offset + count]))
            modes.extend(bytes([int(enabled)]) * count)
            offset += count
        self.assertEqual(offset, len(audio))
        delayed_raw = bytes(delay_bytes) + audio
        expected = bytes(wet if enabled else dry
                         for wet, dry, enabled in zip(baseline, delayed_raw, modes))
        self.assertEqual(selected, expected)

        await audio_filter.stop()
        await audio_filter.start(SAMPLE_RATE)
        await audio_filter.process_frame(FilterUpdateSettingsFrame(settings={"enabled": False}))
        self.assertEqual(await feed(audio_filter, audio), delayed_raw[:len(audio)])

    async def test_telephone_wav_preserves_rate_length_and_delayed_tail(self):
        executable = str(Path(NATIVE_EXE).resolve())
        with tempfile.TemporaryDirectory(prefix="hush-telephone-") as directory:
            for count in (1, 23, 22 * HOP + 37):
                with self.subTest(input_samples=count):
                    source = telephone_pcm(count)
                    audio_filter, info = await self.new_filter()
                    delay_bytes = info.algorithmic_delay_samples * 2
                    streamed = await feed(audio_filter, pad_for_drain(source, info.algorithmic_delay_samples))
                    await audio_filter.stop()
                    input_path = Path(directory) / f"input-{count}.wav"
                    output_path = Path(directory) / f"output-{count}.wav"
                    with wave.open(str(input_path), "wb") as writer:
                        writer.setnchannels(1)
                        writer.setsampwidth(2)
                        writer.setframerate(SAMPLE_RATE)
                        writer.writeframes(source)
                    process = await asyncio.create_subprocess_exec(
                        executable, "wav", str(input_path), str(output_path),
                        "--atten-lim-db", "24", stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
                    finally:
                        if process.returncode is None:
                            process.kill()
                            await process.communicate()
                    self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
                    self.assertEqual(stdout, b"")
                    report = json.loads(stderr)
                    self.assertEqual(report["sample_rate"], SAMPLE_RATE)
                    self.assertEqual(report["input_samples"], count)
                    self.assertEqual(report["output_samples"], count)
                    self.assertEqual(report["algorithmic_delay_samples"], info.algorithmic_delay_samples)
                    self.assertTrue(report["delay_compensated"])
                    with wave.open(str(output_path), "rb") as reader:
                        self.assertEqual((reader.getnchannels(), reader.getsampwidth(), reader.getframerate()),
                                         (1, 2, SAMPLE_RATE))
                        self.assertEqual(reader.getnframes(), count)
                        actual = reader.readframes(count)
                    self.assertEqual(actual, streamed[delay_bytes:delay_bytes + len(source)])


if __name__ == "__main__":
    unittest.main()
