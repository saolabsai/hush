"""Actual wideband PCM, preset and WAV regression; no quality score implied."""

from __future__ import annotations

import asyncio
import importlib.util
import math
import os
from pathlib import Path
import struct
import tempfile
import unittest
import wave

from sao_noise import HushConfig


NATIVE_EXE = os.environ.get("SAO_DENOISE_EXE")
HAS_PIPECAT = importlib.util.find_spec("pipecat") is not None


@unittest.skipUnless(NATIVE_EXE and HAS_PIPECAT, "set SAO_DENOISE_EXE and install sao-noise[pipecat]")
class WidebandNativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_wideband_config_packets_wav_and_disabled_restart(self):
        from sao_noise.pipecat import DeepFilterNetFilter

        config = HushConfig.for_profile("phone_call", sample_rate=16000)
        audio_filter = DeepFilterNetFilter(NATIVE_EXE, config=config)
        self.addAsyncCleanup(audio_filter.stop)
        await audio_filter.start(config.sample_rate)
        info = audio_filter.denoiser.stream_info
        self.assertEqual((info.sample_rate, info.frame_samples, info.algorithmic_delay_samples),
                         (16000, 160, 544))
        self.assertEqual(info.model_sample_rate, 48000)
        source = struct.pack("<1637h", *[
            0 if 320 <= i < 640 else round(8000 * math.sin(2 * math.pi * 300 * i / 16000))
            for i in range(1637)
        ])
        samples = (len(source) // 2 + info.algorithmic_delay_samples + 159) // 160 * 160
        padded = source + bytes(samples * 2 - len(source))
        baseline = await audio_filter.filter(padded)
        self.assertEqual(len(baseline), len(padded))
        self.assertTrue(any(baseline))
        await audio_filter.stop()
        await audio_filter.start(config.sample_rate)
        fragmented = bytearray()
        for offset in range(0, len(padded), 106):
            fragmented.extend(await audio_filter.filter(padded[offset:offset + 106]))
        self.assertEqual(fragmented, baseline)
        await audio_filter.stop()

        with tempfile.TemporaryDirectory(prefix="hush-wideband-") as folder:
            input_path, output_path = Path(folder) / "input.wav", Path(folder) / "output.wav"
            with wave.open(str(input_path), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(16000)
                writer.writeframes(source)
            process = await asyncio.create_subprocess_exec(
                NATIVE_EXE, "wav", str(input_path), str(output_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), 60)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()
            self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
            with wave.open(str(output_path), "rb") as reader:
                self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getsampwidth()),
                                 (16000, 1, 2))
                self.assertEqual(reader.getnframes(), len(source) // 2)
                self.assertEqual(reader.readframes(reader.getnframes()),
                                 baseline[info.algorithmic_delay_samples * 2:][:len(source)])

        disabled = DeepFilterNetFilter(NATIVE_EXE, config=HushConfig.for_profile(
            "phone_call", sample_rate=16000, enabled=False))
        self.addAsyncCleanup(disabled.stop)
        await disabled.start(16000)
        self.assertEqual(await disabled.filter(padded),
                         (bytes(info.algorithmic_delay_samples * 2) + padded)[:len(padded)])


if __name__ == "__main__":
    unittest.main()
