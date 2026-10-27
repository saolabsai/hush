"""Local browser test bench for the Rust speech enhancement executable."""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlsplit
import wave

from aiohttp import web


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "python" / "src"))
from sao_noise.config import HushConfig, PROFILE_NAMES, SUPPORTED_SAMPLE_RATES

STATIC = Path(__file__).resolve().parent / "static"
MAX_SECONDS = 60
MAX_BODY = 6 * 1024 * 1024
UPLOAD_TIMEOUT_SECONDS = 30
ENGINE = web.AppKey("engine", Path)
DEMO = web.AppKey("demo", Path)
LOCK = web.AppKey("inference_lock", asyncio.Lock)
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
        "connect-src 'self'; media-src 'self' blob:; img-src 'self' data:; "
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
        "supported_sample_rates": list(SUPPORTED_SAMPLE_RATES),
        "profiles": {name: HushConfig.for_profile(name).to_dict() for name in PROFILE_NAMES},
        "max_duration_seconds": MAX_SECONDS,
        "default_attenuation_db": 24,
        "demo_available": request.app[DEMO].is_file(),
        "deployment_mode": "public" if request.app[PUBLIC_ORIGIN] else "local",
        "public_origin": request.app[PUBLIC_ORIGIN] or None,
    }
    if not ready:
        result["error"] = "Build the Rust engine first: cargo build --release --locked"
    return web.json_response(result)


async def demo(request: web.Request) -> web.Response:
    if not request.app[DEMO].is_file():
        return problem("The example recording is unavailable. Record your mic or upload a WAV.", 404)
    return web.FileResponse(request.app[DEMO], headers={"Content-Type": "audio/wav"})


def validate_wav(data: bytes) -> tuple[int, int]:
    """Return sample count and rate for a bounded, complete PCM stream."""
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            rate = audio.getframerate()
            if ((audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (1, 2, "NONE")
                    or rate not in SUPPORTED_SAMPLE_RATES):
                raise ValueError("Expected mono 8, 16 or 48 kHz PCM16 WAV. Reload the clip through this app.")
            count = audio.getnframes()
            if count > MAX_SECONDS * rate:
                raise OverflowError("Choose a clip of 60 seconds or less.")
            if count < 1:
                raise ValueError("The recording is empty. Record or choose a longer clip.")
            pcm = audio.readframes(count + 1)
            if len(pcm) != count * 2:
                raise ValueError("The WAV is incomplete or contains a partial sample.")
            return count, rate
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
    enabled_value = request.query.get("enabled", "true")
    if enabled_value not in {"true", "false"}:
        return problem("Enabled must be true or false.")
    enabled = enabled_value == "true"
    requested_rate = request.query.get("sample_rate")
    if requested_rate is not None:
        if requested_rate not in {str(rate) for rate in SUPPORTED_SAMPLE_RATES}:
            return problem("Sample rate must be 8000, 16000 or 48000 Hz.")
        requested_rate = int(requested_rate)
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
            samples, sample_rate = validate_wav(data)
        except OverflowError as exc:
            return problem(str(exc), 413)
        except ValueError as exc:
            return problem(str(exc))
        if requested_rate is not None and requested_rate != sample_rate:
            return problem("The requested sample rate does not match the WAV header. Convert the audio first.")
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
                    "--atten-lim-db", str(attenuation if enabled else 0.0),
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
                if validate_wav(output) != (samples, sample_rate):
                    return problem("The processed recording had an unexpected sample rate or length.", 500)
                return web.Response(body=output, content_type="audio/wav", headers={
                    "Content-Disposition": 'inline; filename="sao-cleaned.wav"',
                    "X-Processing-Ms": f"{elapsed_ms:.3f}",
                    "X-Inference-Ms": f"{timing['processing_compute_seconds'] * 1000:.3f}",
                    "X-Real-Time-Factor": f"{timing['real_time_factor']:.6f}",
                    "X-Audio-Duration": f"{samples / sample_rate:.6f}",
                    "X-Hush-Enabled": str(enabled).lower(),
                    "X-Attenuation-Limit-Db": str(attenuation),
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


async def asset(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "index.html")
    # A fixed list prevents file traversal and avoids exposing local recordings.
    if name not in {"index.html", "styles.css", "app.js", "recorder-worklet.js"}:
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
    app[PUBLIC_ORIGIN] = configured_public_origin(public_origin) if public_origin is not None else ""
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/demo", demo)
    app.router.add_post("/api/enhance", enhance)
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
