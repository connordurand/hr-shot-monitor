"""
Polar H10 BLE streaming.

Key responsibilities:
  - PolarH10Stream: decodes HR notifications, reconstructs per-beat times from
    RR intervals, computes per-beat audio peak, drives SessionLogger.
  - find_h10_devices: BLE scan returning Polar devices.
  - collect_h10_data: async BLE connection loop (runs inside the BLE event loop).
  - start_streaming / stop_streaming: thread-safe wrappers called from Dash.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import numpy as np
from bleak import BleakClient, BleakScanner

import src.state as state
from src.config import HR_BUFFER_MINUTES, HR_UUID


class PolarH10Stream:
    """
    One instance per connected Polar H10 device.

    Buffers:
      hr_values   — raw BPM as reported in each BLE notification
      timestamps  — elapsed seconds for each notification
      rr_intervals — (beat_time_elapsed, rr_ms) for every reconstructed beat
    """

    def __init__(self, device_address: str, buffer_minutes: int = HR_BUFFER_MINUTES):
        self.device_address = device_address
        self.buffer_size = buffer_minutes * 60 * 10

        self.hr_values: deque[float] = deque(maxlen=self.buffer_size)
        self.timestamps: deque[float] = deque(maxlen=self.buffer_size)
        self.rr_intervals: deque[tuple[float, float]] = deque(maxlen=self.buffer_size)

        self.client: BleakClient | None = None
        self.is_connected: bool = False
        self.start_time: float | None = None
        self.logger = None  # SessionLogger | None, set by start_recording_session

    # ------------------------------------------------------------------
    # BLE notification handler
    # ------------------------------------------------------------------

    def hr_callback(self, sender, data: bytearray) -> None:
        """
        Called on every Heart Rate Measurement notification.

        Packet layout (simplified 8-bit HR):
          byte 0: flags (ignored — we assume 8-bit HR)
          byte 1: HR in BPM
          bytes 2+: RR intervals, 2 bytes each, little-endian, 1/1024 s units

        Beat times are reconstructed by walking backward from the notification
        arrival time, one RR interval per beat.
        """
        if self.start_time is None:
            self.start_time = time.time()

        now = time.time()
        elapsed = now - self.start_time

        # Raw HR from device
        if len(data) > 1:
            self.hr_values.append(data[1])
            self.timestamps.append(elapsed)

        # RR intervals → beat times
        if len(data) > 2:
            rr_list: list[float] = []
            for i in range(2, len(data) - 1, 2):
                rr_raw = int.from_bytes(data[i : i + 2], byteorder="little")
                rr_ms = rr_raw * 1000.0 / 1024.0
                rr_list.append(rr_ms)

            # Walk backward from `elapsed` to assign a timestamp to each beat
            beat_times: list[tuple[float, float]] = []
            t = elapsed
            for rr_ms in reversed(rr_list):
                t -= rr_ms / 1000.0
                beat_times.append((t, rr_ms))
            beat_times.reverse()  # chronological order

            for beat_t, rr_ms in beat_times:
                self.rr_intervals.append((beat_t, rr_ms))

                # Per-beat audio peak: max dB in [beat_t - rr_ms/1000, beat_t]
                audio_peak: float | None = None
                if state.mic_db_buffer:
                    window_start = beat_t - rr_ms / 1000.0
                    # Iterate directly instead of copying the entire deque
                    for ts, db in state.mic_db_buffer:
                        if ts < window_start:
                            continue
                        if ts > beat_t:
                            break
                        if audio_peak is None or db > audio_peak:
                            audio_peak = db

                if self.logger is not None:
                    inst_hr = 60000.0 / rr_ms
                    over_flag = (
                        1
                        if audio_peak is not None
                        and audio_peak >= state.threshold.value
                        else 0
                    )
                    self.logger.log_beat(
                        absolute_ts=time.time(),
                        elapsed=beat_t,
                        rr_ms=rr_ms,
                        inst_hr=inst_hr,
                        audio_db=audio_peak,
                        over_thresh=over_flag,
                    )

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    def get_instantaneous_hr(self) -> tuple[np.ndarray, np.ndarray]:
        """Return (times, inst_hr_bpm) arrays derived from stored RR intervals."""
        if not self.rr_intervals:
            return np.array([]), np.array([])
        times = np.array([t for t, _ in self.rr_intervals])
        rr_ms = np.array([rr for _, rr in self.rr_intervals])
        return times, 60000.0 / rr_ms


# ------------------------------------------------------------------
# BLE discovery and connection management
# ------------------------------------------------------------------


async def find_h10_devices(timeout: float = 10.0) -> list:
    """Scan for BLE devices and return those with 'polar' in their name."""
    devices = await BleakScanner.discover(timeout=timeout)
    return [d for d in devices if d.name and "polar" in d.name.lower()]


async def collect_h10_data(device_address: str) -> None:
    """
    Create (if needed) a PolarH10Stream, connect via BLE, and stream HR
    notifications indefinitely.  Runs inside the dedicated BLE event loop;
    cancel the task to disconnect.

    Auto-reconnects on connection loss (up to 3 attempts per reconnect cycle).
    """
    if device_address not in state.streamers:
        state.streamers[device_address] = PolarH10Stream(device_address)
    streamer = state.streamers[device_address]

    while True:  # outer reconnect loop
        # Retry connection up to 3 times
        last_err = None
        client = None
        for attempt in range(3):
            try:
                client = BleakClient(device_address, timeout=20.0)
                await client.connect()
                break
            except Exception as e:
                last_err = e
                logging.warning("BLE connect attempt %d/3 failed for %s: %s", attempt + 1, device_address, e)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                client = None
                if attempt < 2:
                    await asyncio.sleep(2.0)
        else:
            logging.error("BLE connection failed after 3 attempts for %s: %s", device_address, last_err)
            streamer.is_connected = False
            return

        try:
            streamer.client = client
            streamer.is_connected = True
            if streamer.start_time is None:
                streamer.start_time = time.time()

            await client.start_notify(HR_UUID, streamer.hr_callback)

            try:
                while client.is_connected:
                    await asyncio.sleep(1.0)
                # Connection dropped — log and reconnect
                logging.warning("BLE connection lost for %s, reconnecting...", device_address)
            finally:
                try:
                    await client.stop_notify(HR_UUID)
                except Exception:
                    pass
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            streamer.is_connected = False
            streamer.client = None

        # Brief pause before reconnect attempt
        await asyncio.sleep(2.0)


def start_streaming(device) -> None:
    """Schedule collect_h10_data in the caller's running event loop."""
    addr = device.address
    task = state.collector_tasks.get(addr)
    if task is not None and not task.done():
        return
    loop = asyncio.get_event_loop()
    t = loop.create_task(collect_h10_data(addr))
    state.collector_tasks[addr] = t


def stop_streaming(device) -> None:
    """Cancel the BLE task for this device."""
    addr = device.address
    task = state.collector_tasks.pop(addr, None)
    if task is not None:
        task.cancel()
    streamer = state.streamers.pop(addr, None)
    if streamer:
        streamer.is_connected = False
