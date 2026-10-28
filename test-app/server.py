"""Local browser test bench for the Rust speech enhancement executable."""
from __future__ import annotations

import argparse
import asyncio
from importlib.util import find_spec
import io
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit
import wave

from aiohttp import WSMsgType, web

try:
    from sao_noise import DenoiserError, RustDenoiser
except ImportError:  # The clip lab only needs the executable; the live comparison also needs the client.
    RustDenoiser = None


PROJECT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
MAX_SECONDS = 60
MAX_BODY = 6 * 1024 * 1024
UPLOAD_TIMEOUT_SECONDS = 30
LIVE_MAX_SECONDS = 600
LIVE_MAX_MESSAGE_BYTES = 19_200
LIVE_IDLE_SECONDS = 10
# Pipecat's RNNoiseFilter (pyrnnoise 0.4) returns each 10 ms frame one frame late at 48 kHz.
RNNOISE_DELAY_SAMPLES = 960
RNNOISE_AVAILABLE = find_spec("pyrnnoise") is not None and find_spec("pipecat") is not None
ENGINE = web.AppKey("engine", Path)
DEMO = web.AppKey("demo", Path)
LOCK = web.AppKey("inference_lock", asyncio.Lock)
LIVE_LOCK = web.AppKey("live_lock", asyncio.Lock)
PUBLIC_ORIGIN = web.AppKey("public_origin", str)
LOGGER = logging.getLogger("hush-lab")


def problem(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def configured_public_origin(value: str) -> str:
    """Require one HTTPS origin, with no path, credentials, or URL suffixes."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Public origin must be a complete HTTPS origin.") from exc
    if (parsed.scheme != "https" or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            parsed.path not in {"", "/"} or parsed.query or parsed.fragment or
            any(character.isspace() for character in value) or
            "\\" in value or parsed.netloc.endswith(":") or
            (port is not None and not 1 <= port <= 65535)):
        raise ValueError("Public origin must be HTTPS with a host and optional port only.")
    return f"https://{parsed.netloc.lower()}"


@web.middleware
async def local_requests(request: web.Request, handler):
    """Allow localhost or the configured HTTPS origin behind a loopback proxy.

    Host and Origin are checked directly. Forwarded/X-Forwarded-* never grant
    access or override the configured HTTPS origin of the public deployment.
    """
    try:
        authority = request.host
        parsed_host = urlsplit("http://" + authority)
        hostname = parsed_host.hostname
        port = parsed_host.port
        if (parsed_host.netloc != authority or parsed_host.username is not None or
                parsed_host.password is not None or parsed_host.path or
                parsed_host.query or parsed_host.fragment or "\\" in authority or
                authority.endswith(":") or
                (port is not None and not 1 <= port <= 65535)):
            return problem("Invalid host.", 403)
    except ValueError:
        return problem("Invalid host.", 403)

    public_origin = request.app[PUBLIC_ORIGIN]
    public_request = bool(public_origin and authority.lower() == urlsplit(public_origin).netloc)
    if public_request:
        expected_origin = public_origin
    elif hostname in {"127.0.0.1", "localhost", "::1"}:
        expected_origin = f"{request.scheme}://{authority}"
    else:
        return problem("Use the configured app address or localhost.", 403)
    origin = request.headers.get("Origin")
    if (origin is not None and origin != expected_origin) or (
            public_request and request.method not in {"GET", "HEAD", "OPTIONS"} and not origin):
        return problem("Use the test app from its configured address.", 403)
    try:
        response = await handler(request)
    except web.HTTPRequestEntityTooLarge:
        response = problem("Audio is too large. Choose a clip of 60 seconds or less.", 413)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "microphone=(self), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        f"connect-src 'self' {expected_origin.replace('http', 'ws', 1)}; media-src 'self' blob:; img-src 'self' data:; "
        "object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    )
    return response


async def status(request: web.Request) -> web.Response:
    ready = request.app[ENGINE].is_file()
    result = {
        "app": "hush-lab",
        "ready": ready,
        "model": "DeepFilterNet3",
        "sample_rate": 48000,
        "max_duration_seconds": MAX_SECONDS,
        "default_attenuation_db": 24,
        "demo_available": request.app[DEMO].is_file(),
        "deployment_mode": "public" if request.app[PUBLIC_ORIGIN] else "local",
        "public_origin": request.app[PUBLIC_ORIGIN] or None,
        "live_available": ready and RustDenoiser is not None,
        "rnnoise_available": RNNOISE_AVAILABLE,
        "live_max_seconds": LIVE_MAX_SECONDS,
    }
    if not ready:
        result["error"] = "Build the Rust engine first: cargo build --release --locked"
    return web.json_response(result)


async def demo(request: web.Request) -> web.Response:
    if not request.app[DEMO].is_file():
        return problem("The example recording is unavailable. Record your mic or upload a WAV.", 404)
    return web.FileResponse(request.app[DEMO], headers={"Content-Type": "audio/wav"})


def validate_wav(data: bytes) -> int:
    """Return the sample count after validating a bounded, complete PCM stream."""
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 48000, "NONE"):
                raise ValueError("Expected mono 48 kHz PCM16 WAV. Reload the clip through this app.")
            count = audio.getnframes()
            if count > MAX_SECONDS * 48000:
                raise OverflowError("Choose a clip of 60 seconds or less.")
            if count < 1:
                raise ValueError("The recording is empty. Record or choose a longer clip.")
            pcm = audio.readframes(count + 1)
            if len(pcm) != count * 2:
                raise ValueError("The WAV is incomplete or contains a partial sample.")
            return count
    except (wave.Error, EOFError) as exc:
        raise ValueError("This file is not a supported WAV recording.") from exc


async def enhance(request: web.Request) -> web.Response:
    if not request.app[ENGINE].is_file():
        return problem("The Rust engine is missing. Build it before processing audio.", 503)
    try:
        attenuation = float(request.query.get("attenuation", "24"))
        if not math.isfinite(attenuation) or not 0 <= attenuation <= 100:
            raise ValueError
    except ValueError:
        return problem("Suppression strength must be a number between 0 and 100 dB.")
    if request.content_type not in {"audio/wav", "audio/x-wav", "application/octet-stream"}:
        return problem("Send the recording as an audio/wav request.", 415)
    if request.app[LOCK].locked():
        return problem("The engine is processing another clip. Try again shortly.", 429)
    async with request.app[LOCK]:
        try:
            data = await asyncio.wait_for(request.read(), timeout=UPLOAD_TIMEOUT_SECONDS)
        except TimeoutError:
            return problem("Uploading took too long. Try a shorter clip or a faster connection.", 408)
        except ConnectionResetError:
            return problem("The upload was interrupted. Try sending the recording again.", 400)
        try:
            samples = validate_wav(data)
        except OverflowError as exc:
            return problem(str(exc), 413)
        except ValueError as exc:
            return problem(str(exc))
        process = None
        with tempfile.TemporaryDirectory(prefix="hush-lab-") as directory:
            source = Path(directory) / "input.wav"
            target = Path(directory) / "output.wav"
            source.write_bytes(data)
            start = time.perf_counter()
            try:
                # Shield OS process creation so a disconnected request cannot
                # lose the process handle before cleanup.
                spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                    str(request.app[ENGINE]), "wav", str(source), str(target),
                    "--atten-lim-db", str(attenuation),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                ))
                try:
                    process = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    process = await spawn
                    raise
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
                elapsed_ms = (time.perf_counter() - start) * 1000
                if process.returncode != 0:
                    LOGGER.error("Rust worker exited %s: %s", process.returncode,
                                 stderr.decode("utf-8", errors="replace")[-2000:])
                    return problem("The Rust engine could not process this recording.", 500)
                timing = json.loads(stderr.decode("utf-8").strip().splitlines()[-1])
                output = target.read_bytes()
                if validate_wav(output) != samples:
                    return problem("The processed recording had an unexpected length.", 500)
                return web.Response(body=output, content_type="audio/wav", headers={
                    "Content-Disposition": 'inline; filename="sao-cleaned.wav"',
                    "X-Processing-Ms": f"{elapsed_ms:.3f}",
                    "X-Inference-Ms": f"{timing['processing_compute_seconds'] * 1000:.3f}",
                    "X-Real-Time-Factor": f"{timing['real_time_factor']:.6f}",
                    "X-Audio-Duration": f"{samples / 48000:.6f}",
                })
            except TimeoutError:
                return problem("Processing took too long. Try a shorter clip.", 504)
            except (OSError, ValueError, KeyError, IndexError):
                LOGGER.exception("The local worker failed")
                return problem("The local engine returned an invalid result. Check the server terminal.", 500)
            finally:
                if process is not None and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.communicate()


def new_rnnoise_filter():
    """Pipecat's RNNoise filter, the usual noise filter in Pipecat bots, when installed."""
    if not RNNOISE_AVAILABLE:
        return None
    from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter

    return RNNoiseFilter()


async def receive_microphone(socket: web.WebSocketResponse, incoming: asyncio.Queue) -> None:
    """Queue microphone messages, then one final item: an error message, or None after a close."""
    received = 0
    ending = None
    try:
        while True:
            message = await socket.receive(timeout=LIVE_IDLE_SECONDS)
            if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED}:
                break
            if message.type != WSMsgType.BINARY or not message.data or len(message.data) % 2:
                ending = "Send mono 48 kHz PCM16 microphone audio."
                break
            received += len(message.data)
            if received > LIVE_MAX_SECONDS * 96_000:
                ending = "A live test lasts up to 10 minutes. Start again to continue."
                break
            incoming.put_nowait(message.data)
    except TimeoutError:
        ending = "No microphone audio arrived for 10 seconds. Start again."
    finally:
        incoming.put_nowait(ending)


async def live_session(socket: web.WebSocketResponse, engine: Path, attenuation: float) -> None:
    """Return the original, RNNoise and Hush versions of the microphone audio.

    Every reply concatenates equal-length mono 48 kHz PCM16 spans, one per
    version in the order announced by the ready message. Original and RNNoise
    are delayed to Hush's algorithmic delay, so playback can switch versions
    without a jump; that delay is reported rather than hidden.
    """
    denoiser = RustDenoiser(str(engine), atten_lim_db=attenuation)
    rnnoise = new_rnnoise_filter()
    versions = ["original", "hush"] if rnnoise is None else ["original", "rnnoise", "hush"]
    compute_seconds = dict.fromkeys(versions[1:], 0.0)
    incoming: asyncio.Queue = asyncio.Queue()
    receiver = None
    try:
        await denoiser.start(48000)
        if rnnoise is not None:
            await rnnoise.start(48000)
        delay = denoiser.stream_info.algorithmic_delay_samples
        pending = {"original": bytearray(delay * 2), "rnnoise": bytearray(max(0, delay - RNNOISE_DELAY_SAMPLES) * 2),
                   "hush": bytearray()}
        await socket.send_json({
            "type": "ready", "sample_rate": 48000, "versions": versions, "attenuation_db": attenuation,
            "delay_ms": {"original": 0, "rnnoise": RNNOISE_DELAY_SAMPLES / 48, "hush": delay / 48},
            "max_seconds": LIVE_MAX_SECONDS,
        })
        receiver = asyncio.create_task(receive_microphone(socket, incoming))
        processed = 0
        ended = False
        while not ended:
            # Take everything that queued while the last batch ran, so a slow moment is
            # caught up in one larger batch instead of delaying the rest of the test.
            batch = [await incoming.get()]
            while not incoming.empty():
                batch.append(incoming.get_nowait())
            ended = not isinstance(batch[-1], bytes)
            audio = b"".join(batch[:-1] if ended else batch)
            if audio:
                pending["original"] += audio
                if rnnoise is not None:
                    started = time.perf_counter()
                    pending["rnnoise"] += await rnnoise.filter(audio)
                    compute_seconds["rnnoise"] += time.perf_counter() - started
                started = time.perf_counter()
                pending["hush"] += await denoiser.process(audio)
                compute_seconds["hush"] += time.perf_counter() - started
                ready = min(len(pending[name]) for name in versions)
                if ready:
                    await socket.send_bytes(b"".join(bytes(pending[name][:ready]) for name in versions))
                    for name in versions:
                        del pending[name][:ready]
                # Report compute about twice a second of audio; each second is 96,000 bytes.
                if processed // 48_000 != (processed + len(audio)) // 48_000:
                    frames = (processed + len(audio)) / 960
                    await socket.send_json({"type": "stats", "audio_seconds": (processed + len(audio)) / 96_000,
                                            "compute_ms_per_10ms": {name: seconds * 1000 / frames
                                                                    for name, seconds in compute_seconds.items()}})
                processed += len(audio)
            if ended and batch[-1] is not None:
                await socket.send_json({"type": "error", "message": batch[-1]})
    except DenoiserError:
        LOGGER.exception("The live worker failed")
        await socket.send_json({"type": "error", "message": "The Rust engine stopped. Check the server terminal and start again."})
    finally:
        if receiver is not None:
            receiver.cancel()
        await denoiser.stop()
        if rnnoise is not None:
            await rnnoise.stop()


async def live(request: web.Request) -> web.StreamResponse:
    # Browsers always send Origin on WebSockets, and the middleware has checked its value.
    if "Origin" not in request.headers:
        return problem("Open the live comparison from the Hush Lab page.", 403)
    socket = web.WebSocketResponse(max_msg_size=LIVE_MAX_MESSAGE_BYTES)
    await socket.prepare(request)
    try:
        attenuation = float(request.query.get("attenuation", "24"))
        if not math.isfinite(attenuation) or not 0 <= attenuation <= 100:
            raise ValueError
    except ValueError:
        await socket.send_json({"type": "error", "message": "Suppression strength must be a number between 0 and 100 dB."})
    else:
        if RustDenoiser is None or not request.app[ENGINE].is_file():
            await socket.send_json({"type": "error", "message": "Build the Rust engine and install ./python before a live test."})
        elif request.app[LIVE_LOCK].locked():
            await socket.send_json({"type": "error", "message": "Another live test is running. Stop it first."})
        else:
            async with request.app[LIVE_LOCK]:
                await live_session(socket, request.app[ENGINE], attenuation)
    await socket.close()
    return socket


async def asset(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "index.html")
    # A fixed list prevents file traversal and avoids exposing local recordings.
    if name not in {"index.html", "styles.css", "app.js", "recorder-worklet.js", "live.html", "live.js",
                    "live-worklet.js"}:
        raise web.HTTPNotFound()
    path = STATIC / name
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


def create_app(executable: Path | None = None, demo_path: Path | None = None,
               public_origin: str | None = None) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY, middlewares=[local_requests])
    binary_name = "sao-denoise.exe" if os.name == "nt" else "sao-denoise"
    app[ENGINE] = (executable or PROJECT / "target" / "release" / binary_name).resolve()
    app[DEMO] = (demo_path or PROJECT / "output" / "demo" / "noisy.wav").resolve()
    app[LOCK] = asyncio.Lock()
    app[LIVE_LOCK] = asyncio.Lock()
    app[PUBLIC_ORIGIN] = configured_public_origin(public_origin) if public_origin is not None else ""
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/demo", demo)
    app.router.add_post("/api/enhance", enhance)
    app.router.add_get("/api/live", live)
    app.router.add_get("/", asset)
    app.router.add_get("/{name}", asset)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--public-origin", type=configured_public_origin,
                        help="Exact HTTPS origin served by a reverse proxy; listener stays on 127.0.0.1")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    print(f"Hush Lab: http://127.0.0.1:{args.port}", flush=True)
    if args.public_origin:
        print(f"Public proxy origin: {args.public_origin}", flush=True)
    web.run_app(create_app(args.executable, public_origin=args.public_origin), host="127.0.0.1", port=args.port,
                access_log=None, handler_cancellation=True)


if __name__ == "__main__":
    main()
