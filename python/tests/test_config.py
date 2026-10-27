from __future__ import annotations

from dataclasses import FrozenInstanceError
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from sao_noise import HushConfig, RustDenoiser


FAKE = str(Path(__file__).with_name("fake_native.py"))
HAS_PIPECAT = importlib.util.find_spec("pipecat") is not None


class ConfigTests(unittest.TestCase):
    def test_profiles_allow_independent_overrides_and_json_round_trip(self):
        phone = HushConfig.for_profile("phone_call")
        interview = HushConfig.for_profile("video_interview")
        custom = HushConfig.for_profile("phone_call", sample_rate=16000, atten_lim_db=30, enabled=False)
        self.assertEqual(phone.to_dict(), {"sample_rate": 8000, "atten_lim_db": 24, "enabled": True})
        self.assertEqual(interview.sample_rate, 48000)
        self.assertEqual(custom.sample_rate, 16000)
        self.assertEqual(HushConfig.from_dict(custom.to_dict()), custom)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "phone.json"
            path.write_text(json.dumps({"profile": "phone_call", "sample_rate": 16000,
                                        "atten_lim_db": 30, "enabled": False}), encoding="utf-8")
            self.assertEqual(HushConfig.from_json(path), custom)
            path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8-sig")
            self.assertEqual(HushConfig.from_json(path), custom)
        self.assertEqual(HushConfig.for_profile("phone_call"), phone)
        with self.assertRaises(FrozenInstanceError):
            phone.sample_rate = 16000

    def test_packet_sizes_follow_rate_without_changing_engine_hops(self):
        for rate, hop, packet in ((8000, 80, 320), (16000, 160, 640), (48000, 480, 1920)):
            with self.subTest(rate=rate):
                config = HushConfig(sample_rate=rate)
                self.assertEqual(config.frame_samples, hop)
                self.assertEqual(config.frame_bytes, hop * 2)
                self.assertEqual(config.packet_bytes(20), packet)
                self.assertEqual(config.packet_bytes(10), hop * 2)
        for duration in (0, -1, 20.0, True):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                HushConfig().packet_bytes(duration)

    def test_invalid_settings_fail_before_streaming(self):
        for rate in (8, 44100, True, 8000.0, "8000"):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                HushConfig(sample_rate=rate)
        for value in (-1, 101, 10**1000, float("nan"), float("inf"), True, "24"):
            with self.subTest(attenuation=value), self.assertRaises(ValueError):
                HushConfig(atten_lim_db=value)
        for value in (0, 1, "false", None):
            with self.subTest(enabled=value), self.assertRaises(TypeError):
                HushConfig(enabled=value)
        for values in ({"profile": "unknown"}, {"attenuation": 30}, {"sample_rate": None}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                HushConfig.from_dict(values)
        with self.assertRaises(TypeError):
            HushConfig.from_dict([])

    def test_legacy_strength_override_does_not_mutate_shared_config(self):
        original = HushConfig.for_profile("phone_call")
        client = RustDenoiser(config=original, atten_lim_db=31)
        self.assertEqual(client.config.atten_lim_db, 31)
        self.assertEqual(original.atten_lim_db, 24)
        self.assertEqual(RustDenoiser().config, HushConfig())
        with self.assertRaises(TypeError):
            RustDenoiser(config={"sample_rate": 8000})


class ConfigStreamTests(unittest.IsolatedAsyncioTestCase):
    def client(self, config, mode="normal"):
        client = RustDenoiser([sys.executable, FAKE, mode], config=config, startup_timeout=10)
        self.addAsyncCleanup(client.stop)
        return client

    async def test_config_starts_at_wideband_rate_without_repeating_values(self):
        config = HushConfig.for_profile("phone_call", sample_rate=16000)
        client = self.client(config)
        await client.start()
        self.assertEqual(client.stream_info.sample_rate, 16000)
        self.assertEqual(client.stream_info.frame_samples, 160)
        packet = b"\x12\x34" * 320
        self.assertEqual(await client.process(packet), bytes(x ^ 0x55 for x in packet))

    async def test_transport_mismatch_does_not_spawn_or_retag_audio(self):
        client = self.client(HushConfig.for_profile("phone_call"))
        with patch("asyncio.create_subprocess_exec") as spawn:
            with self.assertRaisesRegex(ValueError, "does not match configured"):
                await client.start(48000)
            spawn.assert_not_called()
        self.assertFalse(client.running)

    async def test_initial_disabled_mode_and_restart_restore_config(self):
        client = self.client(HushConfig.for_profile("phone_call", enabled=False), "delay")
        self.assertFalse(client.enabled)
        await client.start()
        audio = b"\x12\x34" * 800
        delay = client.stream_info.algorithmic_delay_samples * 2
        self.assertEqual(await client.process(audio), (bytes(delay) + audio)[:len(audio)])
        await client.set_enabled(True)
        await client.stop()
        await client.start()
        self.assertFalse(client.enabled)
        self.assertEqual(await client.process(audio), (bytes(delay) + audio)[:len(audio)])

    @unittest.skipUnless(HAS_PIPECAT, "install sao-noise[pipecat] for adapter checks")
    async def test_pipecat_uses_the_same_config_and_checks_transport_rate(self):
        from pipecat.frames.frames import FilterUpdateSettingsFrame
        from sao_noise.pipecat import DeepFilterNetFilter
        config = HushConfig.for_profile("video_interview", sample_rate=16000, enabled=False)
        audio_filter = DeepFilterNetFilter([sys.executable, FAKE, "normal"], config=config)
        self.addAsyncCleanup(audio_filter.stop)
        with self.assertRaisesRegex(ValueError, "does not match configured"):
            await audio_filter.start(48000)
        await audio_filter.start(config.sample_rate)
        packet = b"\x12\x34" * 320
        self.assertEqual(await audio_filter.filter(packet), packet)
        for field, value in (("sample_rate", 48000), ("atten_lim_db", 30)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "after stopping"):
                await audio_filter.process_frame(FilterUpdateSettingsFrame(settings={field: value, "enabled": True}))
            self.assertFalse(audio_filter.denoiser.enabled)


if __name__ == "__main__":
    unittest.main()
