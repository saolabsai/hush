from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from sao_noise import DenoiserError, RustDenoiser


FAKE = str(Path(__file__).with_name("fake_native.py"))


def wet(audio):
    return bytes(value ^ 0x55 for value in audio)


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def client(self, mode="normal", **kwargs):
        client = RustDenoiser([sys.executable, FAKE, mode], startup_timeout=10, **kwargs)
        self.addAsyncCleanup(client.stop)
        return client

    def test_attenuation_matches_native_supported_range(self):
        for attenuation in (-1, 101, float("nan"), float("inf"), float("-inf")):
            with self.subTest(attenuation=attenuation):
                with self.assertRaisesRegex(ValueError, "between 0 and 100"):
                    RustDenoiser(atten_lim_db=attenuation)
        for attenuation in (0, 24, 100):
            with self.subTest(attenuation=attenuation):
                RustDenoiser(atten_lim_db=attenuation)

    async def test_fragmented_and_large_chunks_preserve_bytes(self):
        client = self.client()
        await client.start()
        audio = bytes(range(256)) * 600  # More than ordinary OS pipe capacities.
        output = bytearray()
        offset = 0
        with patch.object(client, "_exchange", wraps=client._exchange) as exchange:
            for length in (2, 14, 1000, 222, 12_000, 140_362):
                output.extend(await client.process(audio[offset:offset + length]))
                offset += length
            output.extend(await client.process(audio[offset:]))
        batch_sizes = [len(call.args[0]) for call in exchange.await_args_list]
        self.assertTrue(all(0 < size < 4096 and size % 960 == 0 for size in batch_sizes))
        self.assertIn(3840, batch_sizes)
        self.assertEqual(output, wet(audio))
        self.assertEqual(await client.process(b""), b"")

    async def test_browser_packets_exchange_available_hops_without_extra_capture(self):
        client = self.client()
        await client.start()
        audio = bytes(range(256)) * 60  # Five 32 ms / 1536-sample capture packets.
        output = bytearray()
        with patch.object(client, "_exchange", wraps=client._exchange) as exchange:
            for offset in range(0, len(audio), 3072):
                # Three complete hops must be returned without waiting for a fourth.
                output.extend(await asyncio.wait_for(client.process(audio[offset:offset + 3072]), 2))
        self.assertEqual([len(call.args[0]) for call in exchange.await_args_list],
                         [2880, 2880, 2880, 2880, 3840])
        self.assertEqual(output, wet(audio))

    async def test_phone_packets_return_without_waiting_for_four_hops(self):
        client = self.client()
        await client.start(8000)
        self.assertEqual(client.stream_info.sample_rate, 8000)
        self.assertEqual(client.stream_info.frame_samples, 80)
        self.assertEqual(client.stream_info.model_sample_rate, 48000)
        packet = b"\x12\x34" * 160
        with patch.object(client, "_exchange", wraps=client._exchange) as exchange:
            for _ in range(5):
                self.assertEqual(await asyncio.wait_for(client.process(packet), 2), wet(packet))
        self.assertEqual([len(call.args[0]) for call in exchange.await_args_list], [320] * 5)

    async def test_phone_large_fragmented_input_stays_bounded_and_ordered(self):
        client = self.client()
        await client.start(8000)
        audio = bytes(range(256)) * 125
        output = bytearray()
        offset = 0
        with patch.object(client, "_exchange", wraps=client._exchange) as exchange:
            for length in (2, 62, 258, 318, 2048, 1198):
                output.extend(await client.process(audio[offset:offset + length]))
                offset += length
            output.extend(await client.process(audio[offset:]))
        self.assertEqual(output, wet(audio))
        sizes = [len(call.args[0]) for call in exchange.await_args_list]
        self.assertTrue(all(0 < size <= 640 and size % 160 == 0 for size in sizes))
        self.assertIn(640, sizes)

    async def test_phone_modes_keep_sample_positions_across_partial_hops(self):
        client = self.client("delay")
        await client.start(8000)
        expected = bytearray(client.stream_info.algorithmic_delay_samples * 2)
        output = bytearray()
        segments = [(True, b"\x01\x00" * 37), (False, b"\x02\x00" * 243),
                    (True, b"\x03\x00" * 161), (False, b"\x04\x00" * 279),
                    (True, bytes(480))]
        for enabled, segment in segments:
            await client.set_enabled(enabled)
            expected.extend(wet(segment) if enabled else segment)
            output.extend(await client.process(segment))
        self.assertEqual(output, expected[:len(output)])
        self.assertEqual(len(output), sum(len(segment) for _, segment in segments))

    async def test_restart_can_change_rate_without_old_audio_or_mode(self):
        client = self.client()
        await client.start(8000)
        await client.set_enabled(False)
        self.assertEqual(await client.process(b"\x12\x34" * 100), b"\x12\x34" * 80)
        await client.stop()
        await client.start(48000)
        self.assertTrue(client.enabled)
        self.assertEqual(client.stream_info.sample_rate, 48000)
        self.assertEqual(await client.process(bytes(960)), wet(bytes(960)))

    async def test_phone_rejects_worker_running_at_a_different_rate(self):
        client = self.client("wrong_rate")
        with self.assertRaisesRegex(DenoiserError, "sample_rate"):
            await client.start(8000)
        self.assertFalse(client.running)
        self.assertIsNone(client._process)

    async def test_unsupported_or_ambiguous_rates_do_not_spawn(self):
        client = self.client()
        with patch("asyncio.create_subprocess_exec") as spawn:
            for rate in (8, 22050, 44100, 8000.0, True, "8000"):
                with self.subTest(rate=rate), self.assertRaisesRegex(ValueError, "8000, 16000 or 48000"):
                    await client.start(rate)
            spawn.assert_not_called()

    async def test_batched_and_fragmented_toggles_have_identical_delayed_audio(self):
        batched, fragmented = self.client("delay"), self.client("delay")
        await asyncio.gather(batched.start(), fragmented.start())
        segments = [(True, b"\x01\x02" * 197), (False, b"\x03\x04" * 6803),
                    (True, b"\x05\x06" * 4001), (False, b"\x07\x08" * 1839),
                    (True, bytes(1560 * 2))]
        expected = bytearray(1440 * 2)
        whole_output, split_output = bytearray(), bytearray()
        for enabled, segment in segments:
            await batched.set_enabled(enabled)
            await fragmented.set_enabled(enabled)
            expected.extend(wet(segment) if enabled else segment)
            whole_output.extend(await batched.process(segment))
            for offset in range(0, len(segment), 314):  # Never a complete native hop.
                split_output.extend(await fragmented.process(segment[offset:offset + 314]))
        self.assertEqual(whole_output, expected[:len(whole_output)])
        self.assertEqual(split_output, whole_output)
        self.assertEqual(len(whole_output), sum(len(segment) for _, segment in segments))

    async def test_partial_hop_and_odd_input_validation(self):
        client = self.client()
        await client.start()
        self.assertEqual(await client.process(b"\x01\x00"), b"")
        with self.assertRaisesRegex(ValueError, "two-byte"):
            await client.process(b"\x01")
        self.assertEqual(await client.process(b"\x02\x00" * 479), wet(b"\x01\x00" + b"\x02\x00" * 479))

    async def test_toggle_with_partial_hops_and_model_delay(self):
        client = self.client("delay")
        await client.start()
        self.assertEqual(client.stream_info.algorithmic_delay_samples, 1440)
        segments = [(True, b"\x01\x00" * 197), (False, b"\x02\x00" * 803),
                    (True, b"\x03\x00" * 440), (False, b"\x04\x00" * 960),
                    (True, bytes(2880))]
        expected = bytearray(2880)
        output = bytearray()
        for enabled, segment in segments:
            await client.set_enabled(enabled)
            expected.extend(wet(segment) if enabled else segment)
            output.extend(await client.process(segment))
        self.assertEqual(output, expected[:len(output)])
        self.assertEqual(len(output), sum(len(segment) for _, segment in segments))

    async def test_lifecycle_and_restart_clears_old_partial_state(self):
        client = self.client()
        with self.assertRaisesRegex(RuntimeError, "start"):
            await client.process(b"")
        with self.assertRaisesRegex(RuntimeError, "start"):
            await client.set_enabled(False)
        with self.assertRaisesRegex(ValueError, "48000"):
            await client.start(44100)
        await client.start()
        with self.assertRaisesRegex(RuntimeError, "already"):
            await client.start()
        await client.process(b"\x12\x34")
        await client.set_enabled(False)
        previous = client._process
        await client.stop()
        self.assertIsNotNone(previous.returncode)
        await client.stop()
        await client.start()
        self.assertTrue(client.enabled)
        self.assertEqual(await client.process(bytes(960)), wet(bytes(960)))

    async def test_instances_have_independent_modes_and_buffers(self):
        first, second = self.client(), self.client()
        await asyncio.gather(first.start(), second.start())
        await first.set_enabled(False)
        await first.process(b"\x12\x34")
        output = await second.process(bytes(960))
        self.assertEqual(output, wet(bytes(960)))
        self.assertEqual(await first.process(bytes(958)), b"\x12\x34" + bytes(958))

    async def test_startup_failures_do_not_bypass(self):
        for mode in ("bad_header", "early_exit"):
            with self.subTest(mode=mode):
                client = self.client(mode)
                with self.assertRaises(DenoiserError):
                    await client.start()
                self.assertFalse(client.running)
                self.assertIsNone(client._process)
                with self.assertRaises(RuntimeError):
                    await client.process(bytes(960))

    async def test_startup_timeout_reaps_process(self):
        client = RustDenoiser([sys.executable, FAKE, "startup_timeout"], startup_timeout=.15)
        self.addAsyncCleanup(client.stop)
        with self.assertRaisesRegex(DenoiserError, "TimeoutError"):
            await client.start()
        self.assertIsNone(client._process)

    async def test_missing_executable_is_an_error(self):
        client = RustDenoiser("certainly-missing-sao-denoise-executable")
        self.addAsyncCleanup(client.stop)
        with self.assertRaises(DenoiserError):
            await client.start()
        self.assertFalse(client.running)

    async def test_response_timeout_and_short_response_close_session(self):
        for mode in ("response_timeout", "short_response"):
            with self.subTest(mode=mode):
                client = self.client(mode, response_timeout=.15)
                await client.start()
                process = client._process
                with self.assertRaises(DenoiserError):
                    await client.process(bytes(3840))
                self.assertFalse(client.running)
                self.assertIsNotNone(process.returncode)

    async def test_processing_cancellation_reaps_worker(self):
        client = self.client("response_timeout", response_timeout=10)
        await client.start()
        process = client._process
        processing = asyncio.create_task(client.process(bytes(3840)))
        await asyncio.sleep(.05)
        processing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await processing
        self.assertIsNotNone(process.returncode)
        self.assertFalse(client.running)

    async def test_startup_cancellation_reaps_worker(self):
        client = self.client("startup_timeout")
        startup = asyncio.create_task(client.start())
        for _ in range(100):
            if client._process is not None:
                break
            await asyncio.sleep(.01)
        process = client._process
        self.assertIsNotNone(process)
        startup.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await startup
        self.assertIsNotNone(process.returncode)
        self.assertFalse(client.running)

    async def test_stderr_is_drained_and_bounded(self):
        client = self.client("stderr_flood")
        await client.start()
        self.assertEqual(await client.process(bytes(960)), wet(bytes(960)))
        self.assertLessEqual(len(client._stderr_tail), 8192)
        self.assertIn("diagnostic", client.stderr_tail)

    async def test_stop_cancellation_still_reaps_worker(self):
        client = self.client()
        await client.start()
        process = client._process
        real_wait = process.wait
        waiting = asyncio.Event()

        async def slow_wait():
            waiting.set()
            await asyncio.sleep(.1)
            return await real_wait()

        with patch.object(process, "wait", slow_wait):
            stopping = asyncio.create_task(client.stop())
            await waiting.wait()
            stopping.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stopping
        self.assertIsNotNone(process.returncode)
        self.assertFalse(client.running)

    async def test_stop_with_unexpected_stdout_pressure(self):
        client = self.client("stdout_flood", shutdown_timeout=.5)
        await client.start()
        process = client._process
        await asyncio.sleep(.05)
        await asyncio.wait_for(client.stop(), 2)
        self.assertIsNotNone(process.returncode)

    def test_bad_metadata_is_rejected(self):
        import json
        valid = dict(protocol=1, sample_rate=48000, frame_samples=480,
                     algorithmic_delay_samples=1440, model="DeepFilterNet3")
        for key, value in [("protocol", True), ("protocol", 2), ("sample_rate", 16000),
                           ("frame_samples", 960), ("algorithmic_delay_samples", -1),
                           ("algorithmic_delay_samples", 2**64), ("model", "Unknown")]:
            with self.subTest(key=key, value=value):
                data = valid | {key: value}
                with self.assertRaises(ValueError):
                    RustDenoiser._parse_header(json.dumps(data).encode() + b"\n")

    def test_phone_metadata_uses_interface_samples_and_the_48k_model(self):
        import json
        valid = dict(protocol=1, sample_rate=8000, frame_samples=80,
                     algorithmic_delay_samples=304, model="DeepFilterNet3", model_sample_rate=48000)
        info = RustDenoiser._parse_header(json.dumps(valid).encode() + b"\n", 8000)
        self.assertEqual((info.sample_rate, info.frame_samples, info.algorithmic_delay_samples),
                         (8000, 80, 304))
        for key, value in [("sample_rate", 48000), ("sample_rate", 8000.0),
                           ("frame_samples", 480), ("algorithmic_delay_samples", 8001),
                           ("model_sample_rate", 8000), ("model_sample_rate", True)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                RustDenoiser._parse_header(json.dumps(valid | {key: value}).encode() + b"\n", 8000)


if __name__ == "__main__":
    unittest.main()
