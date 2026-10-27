"""A bounded, asynchronous client for the sao-denoise PCM stream protocol."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
import json
import math
import os


class DenoiserError(RuntimeError):
    """Native startup, protocol, or audio processing failed."""


@dataclass(frozen=True)
class StreamInfo:
    sample_rate: int
    frame_samples: int
    algorithmic_delay_samples: int
    model: str
    model_sample_rate: int = 48_000


class RustDenoiser:
    """One native process and one audio timeline, for mono PCM16 LE at 8/48 kHz.

    ``process`` buffers incomplete 10 ms hops. It exchanges up to four
    already available hops together, without waiting for additional input.
    Each exchange stays below 4 KiB so stdin and stdout cannot fill one another
    while processing large inputs, including with small Windows pipe buffers.
    Cancellation or a failed exchange invalidates and closes the process; an
    explicit ``start`` is required before more audio can be submitted.

    Disabling suppression selects delayed dry audio while still advancing the
    model. Control changes follow the corresponding input samples through its
    delay. Neither toggling nor splitting a hop changes audio order.
    """

    SAMPLE_RATE = 48_000
    SUPPORTED_SAMPLE_RATES = (8_000, 48_000)
    # Compatibility defaults; active framing is reported in stream_info.
    FRAME_SAMPLES = 480
    FRAME_BYTES = FRAME_SAMPLES * 2
    MAX_EXCHANGE_BYTES = FRAME_BYTES * 4

    def __init__(
        self,
        executable: str | os.PathLike[str] | Sequence[str] = "sao-denoise",
        *,
        atten_lim_db: float = 24.0,
        startup_timeout: float = 60.0,
        response_timeout: float = 5.0,
        shutdown_timeout: float = 2.0,
    ) -> None:
        if isinstance(executable, (str, os.PathLike)):
            self._command = (os.fspath(executable),)
        else:
            self._command = tuple(executable)
        if not self._command or any(not isinstance(p, str) or not p for p in self._command):
            raise ValueError("executable must be a path or a nonempty sequence of arguments")
        if not math.isfinite(atten_lim_db) or not 0 <= atten_lim_db <= 100:
            raise ValueError("atten_lim_db must be finite and between 0 and 100 dB")
        for name, value in (
            ("startup_timeout", startup_timeout),
            ("response_timeout", response_timeout),
            ("shutdown_timeout", shutdown_timeout),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        self._atten_lim_db = atten_lim_db
        self._startup_timeout = startup_timeout
        self._response_timeout = response_timeout
        self._shutdown_timeout = shutdown_timeout
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail = bytearray()
        self._info: StreamInfo | None = None
        self._pending = bytearray()
        self._pending_modes = bytearray()
        self._dry_delay = bytearray()
        self._mode_delay = bytearray()
        self._enabled = True
        self._input_started = False

    @property
    def stream_info(self) -> StreamInfo | None:
        """Validated native metadata, or None before start/after cleanup."""
        return self._info

    @property
    def running(self) -> bool:
        return self._info is not None and self._process is not None and self._process.returncode is None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def stderr_tail(self) -> str:
        """Up to 8 KiB of native diagnostics, retained after shutdown."""
        return self._stderr_tail.decode("utf-8", errors="replace").strip()

    async def start(self, sample_rate: int = SAMPLE_RATE) -> None:
        if type(sample_rate) is not int or sample_rate not in self.SUPPORTED_SAMPLE_RATES:
            raise ValueError("sao-noise requires mono PCM16 LE at 8000 or 48000 Hz")
        async with self._lock:
            if self._process is not None:
                raise RuntimeError("denoiser is already started; call stop() before restarting")
            self._stderr_tail.clear()
            self._enabled = True
            self._input_started = False
            try:
                async with asyncio.timeout(self._startup_timeout):
                    arguments = [*self._command, "stream", "--atten-lim-db", str(self._atten_lim_db)]
                    if sample_rate != self.SAMPLE_RATE:
                        arguments.extend(("--sample-rate", str(sample_rate)))
                    # Shield process creation so cancellation cannot lose the
                    # handle to a process that the OS has already launched.
                    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                        *arguments,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        limit=65_536,
                    ))
                    try:
                        self._process = await asyncio.shield(spawn)
                    except asyncio.CancelledError:
                        self._process = await spawn
                        raise
                    assert self._process.stderr is not None
                    self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))
                    assert self._process.stdout is not None
                    header = await self._process.stdout.readline()
                    self._info = self._parse_header(header, sample_rate)
                    delay_bytes = self._info.algorithmic_delay_samples * 2
                    self._dry_delay = bytearray(delay_bytes)
                    self._mode_delay = bytearray([1]) * delay_bytes
            except asyncio.CancelledError:
                await self._cleanup_safely()
                raise
            except Exception as exc:
                await self._cleanup_safely()
                raise self._error("native denoiser startup failed", exc) from exc

    @classmethod
    def _parse_header(cls, header: bytes, sample_rate: int = SAMPLE_RATE) -> StreamInfo:
        if type(sample_rate) is not int or sample_rate not in cls.SUPPORTED_SAMPLE_RATES:
            raise ValueError("unsupported requested sample rate")
        if not header.endswith(b"\n") or len(header) > 4096:
            raise ValueError("missing or oversized startup JSON line")
        data = json.loads(header)
        if not isinstance(data, dict):
            raise ValueError("startup JSON must be an object")
        frame_samples = sample_rate // 100
        for key, expected in (("protocol", 1), ("sample_rate", sample_rate), ("frame_samples", frame_samples)):
            if type(data.get(key)) is not int or data[key] != expected:
                raise ValueError(f"unsupported native {key}: {data.get(key)!r}")
        delay = data.get("algorithmic_delay_samples")
        if type(delay) is not int or not 0 <= delay <= sample_rate:
            raise ValueError(f"invalid native algorithmic_delay_samples (expected 0..{sample_rate})")
        if data.get("model") != "DeepFilterNet3":
            raise ValueError(f"unsupported native model: {data.get('model')!r}")
        model_rate = data.get("model_sample_rate", cls.SAMPLE_RATE)
        if type(model_rate) is not int or model_rate != cls.SAMPLE_RATE:
            raise ValueError("unsupported native model_sample_rate")
        return StreamInfo(sample_rate, frame_samples, delay, data["model"], model_rate)

    async def set_enabled(self, enabled: bool) -> None:
        """Apply a mode to future input; already buffered samples keep their mode."""
        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool")
        async with self._lock:
            self._require_started()
            if not self._input_started:
                self._mode_delay[:] = bytes([int(enabled)]) * len(self._mode_delay)
            self._enabled = enabled

    async def process(self, audio: bytes) -> bytes:
        """Accept sample-aligned PCM; return whole output hops (possibly empty).

        Initial output contains the model's signal delay. No samples are
        silently removed to compensate for latency. Bypass has the same delay.
        """
        if not isinstance(audio, bytes):
            raise TypeError("audio must be bytes containing mono PCM16 LE")
        if len(audio) % 2:
            raise ValueError("PCM16 input must contain complete two-byte samples")
        async with self._lock:
            self._require_started()
            assert self._info is not None
            frame_bytes = self._info.frame_samples * 2
            exchange_bytes = frame_bytes * 4
            if audio:
                self._input_started = True
            self._pending.extend(audio)
            self._pending_modes.extend(bytes([int(self._enabled)]) * len(audio))
            complete_bytes = len(self._pending) // frame_bytes * frame_bytes
            output = bytearray()
            try:
                for offset in range(0, complete_bytes, exchange_bytes):
                    end = min(offset + exchange_bytes, complete_bytes)
                    batch = bytes(self._pending[offset:end])
                    batch_bytes = len(batch)
                    modes = self._pending_modes[offset:end]
                    async with asyncio.timeout(self._response_timeout):
                        wet = await self._exchange(batch)
                    self._dry_delay.extend(batch)
                    self._mode_delay.extend(modes)
                    dry = self._dry_delay[:batch_bytes]
                    delayed_modes = self._mode_delay[:batch_bytes]
                    del self._dry_delay[:batch_bytes]
                    del self._mode_delay[:batch_bytes]
                    if all(delayed_modes):
                        output.extend(wet)
                    elif not any(delayed_modes):
                        output.extend(dry)
                    else:
                        output.extend(w if enabled else d for w, d, enabled in zip(wet, dry, delayed_modes))
                del self._pending[:complete_bytes]
                del self._pending_modes[:complete_bytes]
                return bytes(output)
            except asyncio.CancelledError:
                await self._cleanup_safely()
                raise
            except Exception as exc:
                await self._cleanup_safely()
                raise self._error("native denoiser audio exchange failed", exc) from exc

    async def _exchange(self, batch: bytes) -> bytes:
        assert self._process is not None
        assert self._process.stdin is not None and self._process.stdout is not None
        self._process.stdin.write(batch)
        await self._process.stdin.drain()
        return await self._process.stdout.readexactly(len(batch))

    def _require_started(self) -> None:
        if self._process is None or self._info is None:
            raise RuntimeError("denoiser is not running; call start() before processing audio")
        if self._process.returncode is not None:
            raise DenoiserError(f"native denoiser exited with code {self._process.returncode}; call stop() and start()")

    async def _drain_stderr(self, reader: asyncio.StreamReader) -> None:
        while chunk := await reader.read(4096):
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > 8192:
                del self._stderr_tail[:-8192]

    def _error(self, message: str, cause: Exception) -> DenoiserError:
        detail = str(cause) or type(cause).__name__
        diagnostics = f"; stderr: {self.stderr_tail}" if self.stderr_tail else ""
        return DenoiserError(f"{message}: {detail}{diagnostics}")

    async def stop(self) -> None:
        """Release resources; discard incomplete input and the delayed model tail.

        Pipecat's stop hook cannot return audio. Stop at a silence boundary if
        the end of speech must be retained. A later start creates fresh state.
        """
        async with self._lock:
            await self._cleanup_safely()

    async def _cleanup_safely(self) -> None:
        # If a shutdown caller is canceled, finish reaping its process before
        # propagating cancellation. The process handle must never be lost.
        cleanup = asyncio.create_task(self._cleanup())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    @staticmethod
    async def _discard_output(reader: asyncio.StreamReader) -> None:
        while await reader.read(65_536):
            pass

    async def _cleanup(self) -> None:
        process, self._process = self._process, None
        stderr_task, self._stderr_task = self._stderr_task, None
        self._info = None
        self._input_started = False
        self._pending.clear()
        self._pending_modes.clear()
        self._dry_delay.clear()
        self._mode_delay.clear()
        stdout_task = None
        try:
            if process is not None:
                # A broken worker may have written too much stdout. Drain it
                # during shutdown: Process.wait() can otherwise remain blocked
                # on a full asyncio pipe even after the OS process exits.
                if process.stdout is not None:
                    stdout_task = asyncio.create_task(self._discard_output(process.stdout))
                if process.stdin is not None:
                    process.stdin.close()
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), self._shutdown_timeout)
                    except TimeoutError:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        await asyncio.wait_for(process.wait(), self._shutdown_timeout)
        finally:
            tasks = [task for task in (stdout_task, stderr_task) if task is not None]
            # Let the readers consume EOF even if the return code was already
            # available. Canceling before they resume can leave a paused pipe
            # transport open after an unexpected native exit.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), self._shutdown_timeout
                )
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
