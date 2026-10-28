"""HTTP and real DeepFilterNet3 regression checks for the local listening app.

Run from the Hush product directory:
    .venv/Scripts/python.exe -m unittest discover -s test-app -v

Native checks use the release executable, or SAO_DENOISE_EXE when set. They
exercise framing and signal preservation, not subjective speech quality.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
from pathlib import Path
import random
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from aiohttp import WSMsgType, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer
import numpy as np


sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import create_app  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
EXECUTABLE = Path(os.environ.get(
    "SAO_DENOISE_EXE",
    str(ROOT / "target" / "release" / ("sao-denoise.exe" if os.name == "nt" else "sao-denoise")),
))
sys.path.insert(0, str(ROOT / "scripts"))
from generate_fixture import generate_fixture

DEMO = ROOT / "output" / "demo" / "noisy.wav"
_demo_directory = None


def setUpModule():
    global DEMO, _demo_directory
    _demo_directory = tempfile.TemporaryDirectory(prefix="hush-tests-")
    fixture = Path(_demo_directory.name) / "fixture"
    generate_fixture(fixture)
    DEMO = fixture / "noisy.wav"


def tearDownModule():
    if _demo_directory is not None:
        _demo_directory.cleanup()
RATE = 48_000
PUBLIC_ORIGIN = "https://noise.example.test"
PUBLIC_HOST = "noise.example.test"


def make_wav(samples, *, channels=1, rate=RATE, width=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        if isinstance(samples, bytes):
            writer.writeframes(samples)
        else:
            writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return output.getvalue()


def read_wav(payload):
    with wave.open(io.BytesIO(payload), "rb") as reader:
        metadata = (reader.getnchannels(), reader.getsampwidth(), reader.getframerate())
        count = reader.getnframes()
        pcm = reader.readframes(count)
    return metadata, struct.unpack(f"<{len(pcm) // 2}h", pcm), count


def synthetic_samples(count=24_137):
    """Broadband noise with a real silent pause and a partial final 10 ms hop."""
    noise = random.Random(1709)
    return [
        0 if 4_800 <= index < 9_600 else noise.randrange(-6_000, 6_001)
        for index in range(count)
    ]


class _ApiClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_app(executable=EXECUTABLE, demo_path=DEMO),
                                           handler_cancellation=True))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def assert_error(self, response, status):
        self.assertEqual(response.status, status, await response.text())
        self.assertEqual(response.content_type, "application/json")
        body = await response.json()
        self.assertIsInstance(body.get("error"), str)
        self.assertTrue(body["error"])


class ApiTests(_ApiClient):
    async def test_status_describes_the_actual_local_model(self):
        response = await self.client.get("/api/status")
        self.assertEqual(response.status, 200)
        status = await response.json()
        self.assertEqual(status["model"], "DeepFilterNet3")
        self.assertEqual(status["sample_rate"], RATE)
        self.assertEqual(status["max_duration_seconds"], 60)
        self.assertEqual(status["default_attenuation_db"], 24)
        self.assertEqual(status["ready"], EXECUTABLE.is_file())
        self.assertEqual(status["demo_available"], DEMO.is_file())
        self.assertEqual(status["deployment_mode"], "local")
        self.assertIsNone(status["public_origin"])

    async def test_demo_is_the_original_noisy_wav(self):
        response = await self.client.get("/api/demo")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), DEMO.read_bytes())

    async def test_missing_executable_is_reported_before_processing(self):
        missing = ROOT / "does-not-exist-test-sao-denoise.exe"
        self.assertFalse(missing.exists())
        client = TestClient(TestServer(create_app(executable=missing, demo_path=missing)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        status = await (await client.get("/api/status")).json()
        self.assertFalse(status["ready"])
        self.assertFalse(status["demo_available"])
        await self.assert_error(await client.get("/api/demo"), 404)
        response = await client.post("/api/enhance", data=make_wav([0] * 480))
        await self.assert_error(response, 503)

    async def test_foreign_origins_cannot_submit_microphone_audio(self):
        for origin in ("https://example.com", "null", "http://127.0.0.1:1"):
            with self.subTest(origin=origin):
                response = await self.client.post(
                    "/api/enhance", data=make_wav([0] * 480), headers={"Origin": origin}
                )
                await self.assert_error(response, 403)

    async def test_non_loopback_host_is_rejected(self):
        response = await self.client.get("/api/status", headers={"Host": "example.com"})
        await self.assert_error(response, 403)

    async def test_public_headers_cannot_enable_public_access_by_default(self):
        response = await self.client.get("/api/status", headers={
            "Host": PUBLIC_HOST,
            "Origin": PUBLIC_ORIGIN,
            "Forwarded": f"host={PUBLIC_HOST};proto=https",
            "X-Forwarded-Host": "localhost",
            "X-Forwarded-Proto": "https",
        })
        await self.assert_error(response, 403)

    async def test_url_syntax_cannot_disguise_an_untrusted_host_as_localhost(self):
        for host in ("evil.example@localhost", "localhost/evil", "localhost?evil", "localhost#evil"):
            with self.subTest(host=host):
                response = await self.client.get("/api/status", headers={"Host": host})
                await self.assert_error(response, 403)


class PublicApiTests(_ApiClient):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_app(
            executable=EXECUTABLE, demo_path=DEMO, public_origin=PUBLIC_ORIGIN,
        ), handler_cancellation=True))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_public_read_and_localhost_access_are_both_available(self):
        for headers in ({"Host": PUBLIC_HOST}, {}):
            with self.subTest(headers=headers):
                response = await self.client.get("/api/status", headers=headers)
                self.assertEqual(response.status, 200)
                status = await response.json()
                self.assertEqual(status["deployment_mode"], "public")
                self.assertEqual(status["public_origin"], PUBLIC_ORIGIN)
                self.assertEqual(status["ready"], EXECUTABLE.is_file())

    async def test_lookalike_hosts_and_forwarded_spoofing_are_rejected(self):
        for host in ("example.com", PUBLIC_HOST + ".example.com", "evil." + PUBLIC_HOST,
                     PUBLIC_HOST + ":443", PUBLIC_HOST + "@localhost", PUBLIC_HOST + "/localhost"):
            with self.subTest(host=host):
                response = await self.client.get("/api/status", headers={
                    "Host": host,
                    "Origin": PUBLIC_ORIGIN,
                    "Forwarded": f"host={PUBLIC_HOST};proto=https",
                    "X-Forwarded-Host": PUBLIC_HOST,
                    "X-Forwarded-Proto": "https",
                })
                await self.assert_error(response, 403)

    async def test_public_writes_require_the_exact_https_origin(self):
        for origin in (None, "", "null", "http://" + PUBLIC_HOST, "https://example.com",
                       PUBLIC_ORIGIN + ".example.com", PUBLIC_ORIGIN + "/", PUBLIC_ORIGIN + ":443"):
            with self.subTest(origin=origin):
                headers = {"Host": PUBLIC_HOST, "X-Forwarded-Proto": "https"}
                if origin is not None:
                    headers["Origin"] = origin
                response = await self.client.post("/api/enhance", data=make_wav([0] * 480), headers=headers)
                await self.assert_error(response, 403)

    async def test_forwarded_host_does_not_turn_a_local_request_into_a_public_one(self):
        response = await self.client.post("/api/enhance", data=make_wav([0] * 480), headers={
            "Origin": PUBLIC_ORIGIN,
            "Forwarded": f"host={PUBLIC_HOST};proto=https",
            "X-Forwarded-Host": PUBLIC_HOST,
            "X-Forwarded-Proto": "https",
        })
        await self.assert_error(response, 403)

    @unittest.skipUnless(EXECUTABLE.is_file(), "build release sao-denoise for native HTTP checks")
    async def test_https_public_origin_processes_audio_through_http_loopback_proxy(self):
        samples = synthetic_samples(2_513)
        # The reverse proxy terminates TLS and preserves Host. The application
        # still receives HTTP on loopback, so external HTTPS must be configured.
        response = await self.client.post("/api/enhance?attenuation=0", data=make_wav(samples), headers={
            "Host": PUBLIC_HOST, "Origin": PUBLIC_ORIGIN,
        })
        self.assertEqual(response.status, 200, await response.text() if response.status != 200 else "")
        metadata, cleaned, count = read_wav(await response.read())
        self.assertEqual(metadata, (1, 2, RATE))
        self.assertEqual(count, len(samples))
        self.assertLessEqual(max(abs(wet - dry) for wet, dry in zip(cleaned, samples)), 1)


class PublicConfigurationTests(unittest.TestCase):
    def test_public_origin_requires_https_without_credentials_or_url_suffixes(self):
        for origin in ("", "http://noise.example.test", "https://", "noise.example.test",
                       "https://user:pass@noise.example.test", PUBLIC_ORIGIN + "/app",
                       PUBLIC_ORIGIN + "?query=1", PUBLIC_ORIGIN + "#fragment",
                       PUBLIC_ORIGIN + ":wrong", PUBLIC_ORIGIN + ":0", PUBLIC_ORIGIN + ":65536",
                       "https://noise.example.test\\example.com", "https://noise.example.test\n"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                create_app(public_origin=origin)


@unittest.skipUnless(EXECUTABLE.is_file(), "build release sao-denoise for native HTTP checks")
class NativeApiTests(_ApiClient):
    async def test_enhancement_preserves_partial_frame_and_tail_after_pause(self):
        samples = synthetic_samples()
        self.assertNotEqual(len(samples) % 480, 0)
        response = await self.client.post("/api/enhance", data=make_wav(samples))
        self.assertEqual(response.status, 200, await response.text() if response.status != 200 else "")
        metadata, cleaned, count = read_wav(await response.read())
        self.assertEqual(metadata, (1, 2, RATE))
        self.assertEqual(count, len(samples))
        self.assertEqual(len(cleaned), len(samples))
        self.assertTrue(any(cleaned[-137:]), "the last incomplete frame was lost")
        self.assertTrue(any(cleaned[9_600:]), "silence stopped recurrent-state processing")
        self.assertLess(sum(value * value for value in cleaned),
                        sum(value * value for value in samples),
                        "default enhancement did not reduce this noise-only fixture")
        timings = {}
        for name in ("X-Processing-Ms", "X-Inference-Ms", "X-Real-Time-Factor", "X-Audio-Duration"):
            self.assertIn(name, response.headers)
            timings[name] = float(response.headers[name])
            self.assertTrue(math.isfinite(timings[name]), name)
            self.assertGreaterEqual(timings[name], 0, name)
        self.assertGreaterEqual(timings["X-Processing-Ms"], timings["X-Inference-Ms"])
        self.assertAlmostEqual(timings["X-Audio-Duration"], len(samples) / RATE, places=4)

    async def test_zero_attenuation_reconstructs_samples_without_alignment_loss(self):
        samples = synthetic_samples(12_037)
        response = await self.client.post(
            "/api/enhance?attenuation=0", data=make_wav(samples),
            headers={"Origin": str(self.client.make_url("/")).rstrip("/")},
        )
        self.assertEqual(response.status, 200, await response.text() if response.status != 200 else "")
        metadata, cleaned, count = read_wav(await response.read())
        self.assertEqual(metadata, (1, 2, RATE))
        self.assertEqual(count, len(samples))
        self.assertLessEqual(max(abs(wet - dry) for wet, dry in zip(cleaned, samples)), 1)

    async def test_complete_silence_remains_finite_silence(self):
        response = await self.client.post("/api/enhance", data=make_wav([0] * 2_513))
        self.assertEqual(response.status, 200)
        _, cleaned, count = read_wav(await response.read())
        self.assertEqual(count, 2_513)
        self.assertFalse(any(cleaned))

    async def test_invalid_wavs_return_actionable_json_errors(self):
        wav = make_wav([1, -1] * 480)
        cases = {
            "not a wav": b"this is not a WAV",
            "stereo": make_wav([1, -1] * 480, channels=2),
            "wrong rate": make_wav([0] * 480, rate=16_000),
            "wrong width": make_wav(bytes(480), width=1),
            "truncated": wav[:-2],
            "empty recording": make_wav([]),
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                response = await self.client.post("/api/enhance", data=payload)
                await self.assert_error(response, 400)

    async def test_invalid_attenuation_is_rejected(self):
        for attenuation in ("-1", "101", "nan", "inf", "abc", ""):
            with self.subTest(attenuation=attenuation):
                response = await self.client.post(
                    "/api/enhance", params={"attenuation": attenuation},
                    data=make_wav([0] * 480),
                )
                await self.assert_error(response, 400)

    async def test_recordings_longer_than_one_minute_are_rejected(self):
        response = await self.client.post("/api/enhance", data=io.BytesIO(make_wav(bytes(61 * RATE * 2))))
        await self.assert_error(response, 413)

    async def test_oversized_request_body_is_rejected(self):
        response = await self.client.post("/api/enhance", data=io.BytesIO(bytes(8 * 1024 * 1024)))
        await self.assert_error(response, 413)

    async def test_stalled_upload_times_out_and_releases_engine(self):
        sender_cancelled = asyncio.Event()

        async def stalled_body():
            try:
                yield b"RIFF"
                await asyncio.Event().wait()
            finally:
                sender_cancelled.set()

        with patch("server.UPLOAD_TIMEOUT_SECONDS", 0.05):
            response = await asyncio.wait_for(
                self.client.post("/api/enhance", data=stalled_body()), timeout=5,
            )
            await self.assert_error(response, 408)
        await asyncio.wait_for(sender_cancelled.wait(), timeout=5)
        response = await self.client.post("/api/enhance", data=make_wav([0] * 480))
        self.assertEqual(response.status, 200)
        await response.read()

    async def test_disconnected_upload_aborts_read_and_releases_engine(self):
        read_started = asyncio.Event()
        read_cancelled = asyncio.Event()
        original_read = web.Request.read

        async def observed_read(request):
            read_started.set()
            try:
                return await original_read(request)
            except (asyncio.CancelledError, ConnectionResetError):
                read_cancelled.set()
                raise

        async def stalled_body():
            yield b"RIFF"
            await asyncio.Event().wait()

        with patch("server.web.Request.read", new=observed_read):
            request = asyncio.create_task(self.client.post("/api/enhance", data=stalled_body()))
            try:
                await asyncio.wait_for(read_started.wait(), timeout=5)
            finally:
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request
            await asyncio.wait_for(read_cancelled.wait(), timeout=5)
        response = await self.client.post("/api/enhance", data=make_wav([0] * 480))
        self.assertEqual(response.status, 200)
        await response.read()

    async def test_concurrent_request_is_rejected_then_engine_becomes_available(self):
        started = asyncio.Event()
        release = asyncio.Event()
        original_spawn = asyncio.create_subprocess_exec

        async def paused_spawn(*args, **kwargs):
            started.set()
            await release.wait()
            return await original_spawn(*args, **kwargs)

        payload = make_wav([0] * 480)
        with patch("server.asyncio.create_subprocess_exec", new=paused_spawn):
            first_request = asyncio.create_task(self.client.post("/api/enhance", data=payload))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                second_response = await self.client.post("/api/enhance", data=payload)
                await self.assert_error(second_response, 429)
            finally:
                release.set()
                first_response = await asyncio.wait_for(first_request, timeout=30)
                await first_response.read()
            self.assertEqual(first_response.status, 200)
        third_response = await self.client.post("/api/enhance", data=payload)
        self.assertEqual(third_response.status, 200)
        await third_response.read()

    async def test_failed_engine_launch_cleans_upload_and_releases_engine(self):
        directories = []
        original_tempdir = tempfile.TemporaryDirectory

        def recorded_tempdir(*args, **kwargs):
            directory = original_tempdir(*args, **kwargs)
            directories.append(Path(directory.name))
            return directory

        with patch("server.tempfile.TemporaryDirectory", side_effect=recorded_tempdir):
            with patch("server.asyncio.create_subprocess_exec", side_effect=OSError("test launch failure")):
                with self.assertLogs("hush-lab", level="ERROR"):
                    response = await self.client.post("/api/enhance", data=make_wav([0] * 480))
                    await self.assert_error(response, 500)
            self.assertEqual(len(directories), 1)
            self.assertFalse(directories[0].exists(), "failed request retained uploaded audio")
            response = await self.client.post("/api/enhance", data=make_wav([0] * 480))
            self.assertEqual(response.status, 200)
            await response.read()
        self.assertEqual(len(directories), 2)
        self.assertTrue(all(not path.exists() for path in directories),
                        "completed request retained uploaded audio")


@unittest.skipUnless(EXECUTABLE.is_file(), "build release sao-denoise for native live checks")
class LiveApiTests(_ApiClient):
    """The live comparison with the real Hush worker and Pipecat's RNNoiseFilter."""

    async def connect(self, query=""):
        socket = await self.client.ws_connect(
            "/api/live" + query, headers={"Origin": str(self.client.make_url("/")).rstrip("/")})
        self.addAsyncCleanup(socket.close)
        return socket

    async def stream(self, pcm, query=""):
        """Send PCM in 20 ms messages; return the ready message and each version's audio."""
        socket = await self.connect(query)
        ready = await socket.receive_json(timeout=30)
        self.assertEqual(ready["type"], "ready", ready)
        versions = {name: bytearray() for name in ready["versions"]}
        for offset in range(0, len(pcm), 1_920):
            await socket.send_bytes(pcm[offset:offset + 1_920])
        while min(len(audio) for audio in versions.values()) < len(pcm):
            message = await socket.receive(timeout=30)
            if message.type == WSMsgType.TEXT:
                self.assertEqual(json.loads(message.data)["type"], "stats", message.data)
                continue
            self.assertEqual(message.type, WSMsgType.BINARY, message)
            span = len(message.data) // len(versions)
            self.assertEqual(span * len(versions), len(message.data), "versions differ in length")
            for index, audio in enumerate(versions.values()):
                audio += message.data[index * span:(index + 1) * span]
        return ready, {name: np.frombuffer(bytes(audio), "<i2").astype(np.int64) for name, audio in versions.items()}

    def fixture_pcm(self):
        with wave.open(str(DEMO), "rb") as reader:
            return reader.readframes(reader.getnframes())

    async def test_status_reports_the_live_comparison(self):
        status = await (await self.client.get("/api/status")).json()
        self.assertTrue(status["live_available"])
        self.assertTrue(status["rnnoise_available"], "install requirements-dev.txt for pipecat-ai[rnnoise]")
        self.assertEqual(status["live_max_seconds"], 600)

    async def test_versions_are_complete_and_time_aligned(self):
        pcm = self.fixture_pcm()
        ready, versions = await self.stream(pcm, "?attenuation=0")
        self.assertEqual(ready["versions"], ["original", "rnnoise", "hush"])
        delay = round(ready["delay_ms"]["hush"] * RATE / 1000)
        self.assertEqual(delay, 1_440)
        source = np.frombuffer(pcm, "<i2").astype(np.int64)
        self.assertTrue(np.array_equal(versions["original"],
                                       np.concatenate([np.zeros(delay, np.int64), source[:-delay]])))
        # At 0 dB Hush passes its delayed dry signal, so it must match the delayed original.
        self.assertLessEqual(int(np.abs(versions["hush"] - versions["original"]).max()), 1)
        # RNNoise shifts the phase of what it keeps by a few samples, so this catches frame misalignment only.
        voiced = versions["original"][4 * RATE:5 * RATE]
        lag = max(range(-480, 481), key=lambda shift: float(
            np.dot(voiced, versions["rnnoise"][4 * RATE + shift:5 * RATE + shift])))
        self.assertLessEqual(abs(lag), RATE // 1000)

    async def test_both_filters_reduce_the_noise_before_the_voice(self):
        _, versions = await self.stream(self.fixture_pcm())
        # The fixture has noise, hum and taps but no voice before 2 s.
        lead_in = slice(RATE // 5, RATE * 9 // 5)
        level = {name: math.sqrt(float(np.mean(audio[lead_in] ** 2))) for name, audio in versions.items()}
        for name in ("rnnoise", "hush"):
            self.assertLess(level[name], level["original"] / 2, f"{name} did not reduce noise: {level}")

    async def test_live_needs_the_page_origin(self):
        for headers in ({}, {"Origin": "https://example.com"}):
            with self.subTest(headers=headers), self.assertRaises(WSServerHandshakeError) as raised:
                await self.client.ws_connect("/api/live", headers=headers)
            self.assertEqual(raised.exception.status, 403)

    async def test_one_live_test_runs_at_a_time(self):
        first = await self.connect()
        self.assertEqual((await first.receive_json(timeout=30))["type"], "ready")
        second = await self.connect()
        message = await second.receive_json(timeout=10)
        self.assertEqual(message["type"], "error")
        self.assertIn("Another live test", message["message"])

    async def test_invalid_input_and_settings_end_the_session_with_an_error(self):
        cases = (("", "hello", "mono 48 kHz PCM16"), ("", b"\x01", "mono 48 kHz PCM16"),
                 ("?attenuation=nan", None, "between 0 and 100 dB"))
        for query, payload, expected in cases:
            with self.subTest(query=query, payload=payload):
                socket = await self.connect(query)
                if payload is not None:
                    self.assertEqual((await socket.receive_json(timeout=30))["type"], "ready")
                    await (socket.send_str(payload) if isinstance(payload, str) else socket.send_bytes(payload))
                message = await socket.receive_json(timeout=10)
                self.assertEqual(message["type"], "error")
                self.assertIn(expected, message["message"])
                self.assertIn((await socket.receive(timeout=10)).type, {WSMsgType.CLOSE, WSMsgType.CLOSED})

    async def test_a_silent_connection_is_closed(self):
        with patch("server.LIVE_IDLE_SECONDS", 0.2):
            socket = await self.connect()
            self.assertEqual((await socket.receive_json(timeout=30))["type"], "ready")
            message = await socket.receive_json(timeout=10)
        self.assertEqual(message["type"], "error")
        self.assertIn("No microphone audio", message["message"])

    async def test_page_may_open_a_websocket_to_its_own_origin(self):
        response = await self.client.get("/live.html")
        self.assertEqual(response.status, 200)
        origin = str(self.client.make_url("/")).rstrip("/").replace("http", "ws", 1)
        self.assertIn(f"connect-src 'self' {origin};", response.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
