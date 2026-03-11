"""
Microphone capture and dB utilities.

Responsibilities:
  - db_from_audio_frame: convert a float32 PCM frame to a dB value.
  - start_mic_stream: start a sounddevice background stream that continuously
    fills state.mic_db_buffer and logs audio events above threshold.
  - get_mic_db_window: return recent (time, dB) samples for plotting.
  - get_spike_times_on_hr_axis: project dB spikes onto the nearest HR beat time.
"""
from __future__ import annotations

import time

import numpy as np
import sounddevice as sd

import src.state as state
from src.config import FRAME_DURATION, SAMPLE_RATE


def db_from_audio_frame(frame: np.ndarray, eps: float = 1e-10) -> float:
    """
    Convert a mono float32 PCM frame (values in [-1, 1]) to a dB level.
    Shifted by +100 so a quiet room reads ~40–60 dB.
    """
    rms = float(np.sqrt(np.mean(frame**2) + eps))
    return 20.0 * np.log10(rms + eps) + 100.0


def _get_elapsed_time() -> float:
    """Elapsed seconds from the first active streamer's start_time."""
    for streamer in state.streamers.values():
        if streamer.start_time is not None:
            return time.time() - streamer.start_time
    return 0.0


def _get_primary_device_address() -> str | None:
    """Return the address of the first active streamer (for audio event logging)."""
    return next(iter(state.streamers), None)


def list_input_devices() -> list[dict]:
    """Return a list of dicts with 'index', 'name' for all devices that have inputs."""
    result = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            result.append({"index": i, "name": d["name"]})
    return result


def start_mic_stream(device: int | None = None) -> sd.InputStream:
    """
    Start a continuous sounddevice input stream.

    Parameters
    ----------
    device : int or None
        sounddevice device index.  None uses the system default.

    Each audio callback:
      1. Computes dB from the frame RMS.
      2. Appends (elapsed_sec, dB) to state.mic_db_buffer.
      3. If dB >= state.threshold.value, logs an audio event to the active
         device's AudioEventLogger.

    Returns the InputStream so the caller can stop it later.
    """
    dev_info = sd.query_devices(device if device is not None else sd.default.device[0])
    n_channels = min(dev_info["max_input_channels"], 1) or 1

    def audio_callback(indata, frames, time_info, status) -> None:
        mono = indata.mean(axis=1).astype(np.float32)
        db = db_from_audio_frame(mono)
        t_elapsed = _get_elapsed_time()
        state.mic_db_buffer.append((t_elapsed, db))

        addr = _get_primary_device_address()
        if addr is not None:
            audio_logger = state.audio_event_loggers.get(addr)
            if audio_logger is not None and db >= state.threshold.value:
                audio_logger.log_event(time.time(), t_elapsed, db)

    stream = sd.InputStream(
        device=device,
        samplerate=SAMPLE_RATE,
        channels=n_channels,
        callback=audio_callback,
        blocksize=int(SAMPLE_RATE * FRAME_DURATION),
    )
    stream.start()
    return stream


def get_mic_db_window(window_seconds: float) -> tuple[list[float], list[float]]:
    """
    Return all (time, dB) samples from the rolling buffer that fall within
    the last `window_seconds` on the shared elapsed-time axis.
    """
    if not state.mic_db_buffer:
        return [], []
    now = _get_elapsed_time()
    cutoff = now - window_seconds
    pairs = [(ts, db) for ts, db in state.mic_db_buffer if ts >= cutoff]
    if not pairs:
        return [], []
    times, values = zip(*pairs)
    return list(times), list(values)


def get_spike_times_on_hr_axis(
    hr_times: list[float],
    db_times: list[float],
    db_values: list[float],
    db_threshold: float,
) -> list[float]:
    """
    For each audio sample at or above `db_threshold`, find the nearest HR
    beat time and return a deduplicated, sorted list of those HR times.

    This projects audio events onto the HR time axis so shot markers
    can be drawn on the HR plot at meaningful heartbeat positions.
    """
    if not hr_times or not db_times:
        return []
    hr_arr = np.array(hr_times, dtype=float)
    spike_times: list[float] = []
    for t_db, v_db in zip(db_times, db_values):
        if v_db >= db_threshold:
            idx = int(np.argmin(np.abs(hr_arr - t_db)))
            spike_times.append(float(hr_arr[idx]))
    return sorted(set(spike_times))
