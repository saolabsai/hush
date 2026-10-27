"""Pipecat transport filter, applied before voice activity detection and STT."""

from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import FilterControlFrame, FilterEnableFrame, FilterUpdateSettingsFrame

from .stream import RustDenoiser


class DeepFilterNetFilter(BaseAudioFilter):
    """A mono, 8/16/48 kHz Pipecat filter backed by the persistent Rust worker.

    Constructor arguments are forwarded to :class:`RustDenoiser`. Create a
    separate instance per meeting microphone/participant audio stream.
    """

    def __init__(self, executable="sao-denoise", **kwargs):
        self._denoiser = RustDenoiser(executable, **kwargs)

    @property
    def denoiser(self) -> RustDenoiser:
        """Expose stream metadata and bounded diagnostics for observability."""
        return self._denoiser

    async def start(self, sample_rate: int):
        await self._denoiser.start(sample_rate)

    async def stop(self):
        await self._denoiser.stop()

    async def process_frame(self, frame: FilterControlFrame):
        if isinstance(frame, FilterEnableFrame):
            await self._denoiser.set_enabled(frame.enable)
        elif isinstance(frame, FilterUpdateSettingsFrame):
            if {"sample_rate", "atten_lim_db"} & frame.settings.keys():
                raise ValueError("sample_rate and atten_lim_db require a new filter after stopping the session")
            # Pipecat 1.12's BaseInputTransport dispatches settings updates to
            # the input filter, but does not route FilterEnableFrame to it.
            if "enabled" in frame.settings:
                await self._denoiser.set_enabled(frame.settings["enabled"])

    async def filter(self, audio: bytes) -> bytes:
        return await self._denoiser.process(audio)
