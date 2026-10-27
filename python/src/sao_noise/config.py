"""Validated defaults for phone and video interview input streams."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from typing import Any


SUPPORTED_SAMPLE_RATES = (8_000, 16_000, 48_000)
PROFILE_NAMES = ("phone_call", "video_interview")
_PROFILES = {
    "phone_call": {"sample_rate": 8_000, "atten_lim_db": 24.0, "enabled": True},
    "video_interview": {"sample_rate": 48_000, "atten_lim_db": 24.0, "enabled": True},
}


@dataclass(frozen=True)
class HushConfig:
    sample_rate: int = 48_000
    atten_lim_db: float = 24.0
    enabled: bool = True

    def __post_init__(self) -> None:
        if type(self.sample_rate) is not int or self.sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise ValueError("sample_rate must be 8000, 16000 or 48000 Hz")
        if (type(self.atten_lim_db) not in (int, float)
                or not 0 <= self.atten_lim_db <= 100 or not math.isfinite(self.atten_lim_db)):
            raise ValueError("atten_lim_db must be finite and between 0 and 100 dB")
        if type(self.enabled) is not bool:
            raise TypeError("enabled must be a bool")

    @classmethod
    def for_profile(cls, name: str, **overrides: Any) -> HushConfig:
        if not isinstance(name, str) or name not in _PROFILES:
            raise ValueError("profile must be phone_call or video_interview")
        unknown = set(overrides) - {"sample_rate", "atten_lim_db", "enabled"}
        if unknown:
            raise ValueError("unknown Hush settings: " + ", ".join(sorted(unknown)))
        return cls(**(_PROFILES[name] | overrides))

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> HushConfig:
        if not isinstance(values, Mapping):
            raise TypeError("Hush settings must be an object")
        settings = dict(values)
        profile = settings.pop("profile", "video_interview")
        unknown = set(settings) - {"sample_rate", "atten_lim_db", "enabled"}
        if unknown:
            raise ValueError("unknown Hush settings: " + ", ".join(sorted(map(str, unknown))))
        return cls.for_profile(profile, **settings)

    @classmethod
    def from_json(cls, path: str | Path) -> HushConfig:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8-sig")))

    def to_dict(self) -> dict[str, int | float | bool]:
        return asdict(self)

    @property
    def frame_samples(self) -> int:
        return self.sample_rate // 100

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * 2

    def packet_bytes(self, duration_ms: int = 20) -> int:
        """PCM sizing helper; the call transport controls actual packetization."""
        if type(duration_ms) is not int or duration_ms <= 0:
            raise ValueError("duration_ms must be a positive integer")
        return self.sample_rate * duration_ms // 1000 * 2
