"""
CSV loggers for per-session data.

Three logger classes, one per output file:

  SessionLogger     — one row per heartbeat
                      schema: timestamp_iso, timestamp_epoch, elapsed_sec,
                               rr_ms, inst_hr_bpm, audio_db_peak,
                               audio_over_threshold

  AudioEventLogger  — one row per mic sample that crossed the dB threshold
                      schema: timestamp_iso, timestamp_epoch, elapsed_sec,
                               audio_db

  ShotLabelLogger   — one row per manually labeled shot (hit/miss), including
                      HR and audio context at the shot time.
                      schema: timestamp_iso, timestamp_epoch,
                               shot_time_elapsed, label,
                               inst_hr_bpm, audio_db_peak, db_threshold

  NOTE: ShotLabelLogger consolidates the old ShotLabelLogger +
  ShotSummaryLogger into a single file.

Helper functions:
  start_recording_session(device, log_dir) — create and attach all three loggers
  stop_recording_session(device)           — flush, close, and detach
"""
from __future__ import annotations

import csv
import os
import time
from collections import deque
from datetime import datetime

import src.state as state


class SessionLogger:
    """Per-beat CSV writer, driven by PolarH10Stream.hr_callback."""

    HEADER = [
        "timestamp_iso",
        "timestamp_epoch",
        "elapsed_sec",
        "rr_ms",
        "inst_hr_bpm",
        "audio_db_peak",
        "audio_over_threshold",
    ]

    def __init__(self) -> None:
        self.file_path: str | None = None
        self._file = None
        self._writer = None
        self._queue: deque = deque()
        self.is_active: bool = False
        self._last_flush_time: float = 0.0

    def start_session(self, file_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        self.file_path = file_path
        self._file = open(file_path, mode="w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(self.HEADER)
        self.is_active = True

    def log_beat(
        self,
        absolute_ts: float,
        elapsed: float,
        rr_ms: float,
        inst_hr: float,
        audio_db: float | None,
        over_thresh: int,
    ) -> None:
        if not self.is_active:
            return
        self._queue.append((absolute_ts, elapsed, rr_ms, inst_hr, audio_db, over_thresh))
        # Periodic flush every 10 seconds to prevent data loss on crash
        now = time.time()
        if now - self._last_flush_time >= 10.0:
            self.flush()
            self._last_flush_time = now

    def flush(self) -> None:
        if not self.is_active or self._writer is None:
            return
        while self._queue:
            absolute_ts, elapsed, rr_ms, inst_hr, audio_db, over_thresh = (
                self._queue.popleft()
            )
            self._writer.writerow(
                [
                    datetime.fromtimestamp(absolute_ts).isoformat(),
                    absolute_ts,
                    elapsed,
                    rr_ms,
                    inst_hr,
                    audio_db if audio_db is not None else "",
                    over_thresh,
                ]
            )
        if self._file:
            self._file.flush()

    def stop_session(self) -> None:
        if not self.is_active:
            return
        self.flush()
        if self._file:
            self._file.close()
        self._file = None
        self._writer = None
        self.is_active = False


class AudioEventLogger:
    """Logs only mic samples that cross the dB threshold."""

    HEADER = ["timestamp_iso", "timestamp_epoch", "elapsed_sec", "audio_db"]

    def __init__(self) -> None:
        self._file = None
        self._writer = None
        self.is_active: bool = False

    def start_session(self, file_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        self._file = open(file_path, mode="w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(self.HEADER)
        self.is_active = True

    def log_event(self, absolute_ts: float, elapsed: float, audio_db: float) -> None:
        if not self.is_active or self._writer is None:
            return
        self._writer.writerow(
            [datetime.fromtimestamp(absolute_ts).isoformat(), absolute_ts, elapsed, audio_db]
        )
        self._file.flush()

    def stop_session(self) -> None:
        if not self.is_active:
            return
        if self._file:
            self._file.close()
        self._file = None
        self._writer = None
        self.is_active = False


class ShotLabelLogger:
    """
    Logs hit/miss labels with HR and audio context.
    One row per labeled shot; written immediately on log_label() call.
    """

    HEADER = [
        "timestamp_iso",
        "timestamp_epoch",
        "shot_time_elapsed",
        "label",
        "inst_hr_bpm",
        "audio_db_peak",
        "db_threshold",
    ]

    def __init__(self) -> None:
        self._file = None
        self._writer = None
        self.is_active: bool = False

    def start_session(self, file_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        self._file = open(file_path, mode="w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(self.HEADER)
        self.is_active = True

    def log_label(
        self,
        shot_time: float,
        label: str,
        inst_hr: float | None = None,
        audio_db_peak: float | None = None,
        db_threshold: float | None = None,
    ) -> None:
        if not self.is_active or self._writer is None:
            return
        now = time.time()
        self._writer.writerow(
            [
                datetime.fromtimestamp(now).isoformat(),
                now,
                shot_time,
                label,
                inst_hr if inst_hr is not None else "",
                audio_db_peak if audio_db_peak is not None else "",
                float(db_threshold) if db_threshold is not None else "",
            ]
        )
        self._file.flush()

    def stop_session(self) -> None:
        if not self.is_active:
            return
        if self._file:
            self._file.close()
        self._file = None
        self._writer = None
        self.is_active = False


# ------------------------------------------------------------------
# Session management helpers
# ------------------------------------------------------------------


def start_recording_session(device, log_dir: str = "logs") -> None:
    """
    Create SessionLogger, AudioEventLogger, and ShotLabelLogger for `device`,
    start each one, attach the SessionLogger to the streamer, and register
    all three in state.*_loggers dicts.
    """
    addr = device.address
    addr_clean = addr.replace(":", "")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    beat_logger = SessionLogger()
    beat_logger.start_session(os.path.join(log_dir, f"session_{addr_clean}_{ts}.csv"))
    state.loggers[addr] = beat_logger

    streamer = state.streamers.get(addr)
    if streamer is not None:
        streamer.logger = beat_logger

    audio_logger = AudioEventLogger()
    audio_logger.start_session(
        os.path.join(log_dir, f"audio_events_{addr_clean}_{ts}.csv")
    )
    state.audio_event_loggers[addr] = audio_logger

    shot_logger = ShotLabelLogger()
    shot_logger.start_session(
        os.path.join(log_dir, f"shot_labels_{addr_clean}_{ts}.csv")
    )
    state.shot_loggers[addr] = shot_logger


def stop_recording_session(device) -> None:
    """Stop and close all loggers for `device`."""
    addr = device.address

    for registry in (state.loggers, state.audio_event_loggers, state.shot_loggers):
        obj = registry.get(addr)
        if obj is not None:
            obj.stop_session()
            registry[addr] = None

    streamer = state.streamers.get(addr)
    if streamer is not None:
        streamer.logger = None
