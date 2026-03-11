"""
Shared runtime state — imported by polar_stream, audio_stream, logging_utils,
and dashboard_app.  Centralising mutable globals here avoids circular imports.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from src.config import DEFAULT_AUDIO_THRESHOLD, FRAME_DURATION, MIC_BUFFER_SECONDS

if TYPE_CHECKING:
    from src.polar_stream import PolarH10Stream
    from src.logging_utils import SessionLogger, AudioEventLogger, ShotLabelLogger

# Per-device registries (keyed by device address string, e.g. "A0:9E:1A:DC:8B:34")
streamers: dict[str, "PolarH10Stream"] = {}
loggers: dict[str, "SessionLogger"] = {}
audio_event_loggers: dict[str, "AudioEventLogger"] = {}
shot_loggers: dict[str, "ShotLabelLogger"] = {}
collector_tasks: dict[str, asyncio.Task] = {}

# Shared rolling audio buffer — (elapsed_sec: float, dB: float) tuples.
_mic_maxlen = int(MIC_BUFFER_SECONDS / FRAME_DURATION)  # 30s / 0.02s = 1500
mic_db_buffer: deque[tuple[float, float]] = deque(maxlen=_mic_maxlen)


class _ThresholdBox:
    """Mutable container so all modules always see the current threshold value."""
    value: float = DEFAULT_AUDIO_THRESHOLD


threshold = _ThresholdBox()
