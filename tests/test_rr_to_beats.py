"""
Unit tests for the RR-interval → beat-time reconstruction logic inside
PolarH10Stream.hr_callback.

The core algorithm:
  1. Walk backward from notification_elapsed, subtracting each rr_ms/1000.0.
  2. Reverse the list to obtain chronological beat times.
  3. Instantaneous HR per beat = 60000 / rr_ms.
"""
import time
from collections import deque
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Stub out bleak so the import doesn't require hardware
import sys
from unittest.mock import MagicMock as _M
sys.modules.setdefault("bleak", _M())
sys.modules.setdefault("bleak.BleakScanner", _M())
sys.modules.setdefault("bleak.BleakClient", _M())

import src.state as state  # noqa: E402
from src.polar_stream import PolarH10Stream  # noqa: E402


def _make_packet(hr_bpm: int, rr_values_ms: list[float]) -> bytearray:
    """
    Build a minimal Heart Rate Measurement BLE packet.
    byte 0: flags (0x00 — 8-bit HR, RR present flag = 0x10 if needed, ignored by parser)
    byte 1: HR in BPM
    bytes 2+: RR intervals in 1/1024-s units, 2 bytes little-endian each
    """
    data = bytearray([0x00, hr_bpm])
    for rr_ms in rr_values_ms:
        rr_raw = int(round(rr_ms * 1024.0 / 1000.0))
        data += rr_raw.to_bytes(2, byteorder="little")
    return data


class TestRRToBeatReconstruction:
    def setup_method(self):
        state.mic_db_buffer.clear()
        self.streamer = PolarH10Stream("AA:BB:CC:DD:EE:FF")
        self.streamer.start_time = 0.0  # freeze epoch so elapsed = now

    def _fire_callback(self, rr_list_ms: list[float], elapsed: float):
        """Simulate a BLE notification arriving at `elapsed` seconds."""
        packet = _make_packet(72, rr_list_ms)
        with patch("time.time", return_value=elapsed):
            self.streamer.hr_callback(None, packet)

    # ------------------------------------------------------------------

    def test_single_rr_produces_one_beat(self):
        self._fire_callback([800.0], elapsed=10.0)
        assert len(self.streamer.rr_intervals) == 1

    def test_single_rr_beat_time(self):
        """beat_time ≈ notification_elapsed - rr_ms / 1000.
        Tolerance of 2 ms accounts for 1/1024-s quantization in the BLE packet."""
        rr_ms = 800.0
        elapsed = 10.0
        self._fire_callback([rr_ms], elapsed=elapsed)
        beat_t, stored_rr = self.streamer.rr_intervals[0]
        expected_beat_t = elapsed - rr_ms / 1000.0
        assert abs(beat_t - expected_beat_t) < 0.002  # 2 ms quantization tolerance

    def test_single_rr_stored_value(self):
        rr_raw = 820  # 1/1024-s units
        rr_ms_expected = rr_raw * 1000.0 / 1024.0
        data = bytearray([0x00, 72]) + rr_raw.to_bytes(2, "little")
        with patch("time.time", return_value=5.0):
            self.streamer.hr_callback(None, data)
        _, stored_rr = self.streamer.rr_intervals[0]
        assert abs(stored_rr - rr_ms_expected) < 1e-6

    def test_multiple_rr_chronological_order(self):
        """Two RR values must produce two beats in increasing time order."""
        rr_list = [900.0, 850.0]
        elapsed = 20.0
        self._fire_callback(rr_list, elapsed=elapsed)
        assert len(self.streamer.rr_intervals) == 2
        t0 = self.streamer.rr_intervals[-2][0]
        t1 = self.streamer.rr_intervals[-1][0]
        assert t0 < t1, "Beat times must be strictly increasing"

    def test_multiple_rr_last_beat_time(self):
        """Last beat in packet ends at notification_elapsed - last_rr/1000 (±2 ms)."""
        rr_list = [900.0, 850.0]
        elapsed = 20.0
        self._fire_callback(rr_list, elapsed=elapsed)
        last_t, last_rr = self.streamer.rr_intervals[-1]
        expected = elapsed - rr_list[-1] / 1000.0
        assert abs(last_t - expected) < 0.002

    def test_instantaneous_hr_formula(self):
        rr_ms = 750.0  # 80 bpm
        self._fire_callback([rr_ms], elapsed=5.0)
        _, hr_arr = self.streamer.get_instantaneous_hr()
        assert abs(hr_arr[0] - 60000.0 / rr_ms) < 1e-6

    def test_consecutive_notifications_append(self):
        """Two consecutive notifications should accumulate beats."""
        self._fire_callback([800.0], elapsed=1.0)
        self._fire_callback([810.0], elapsed=2.0)
        assert len(self.streamer.rr_intervals) == 2

    def test_no_rr_bytes_no_beat(self):
        """Packet with only 2 bytes (flags + HR) must not append any beat."""
        data = bytearray([0x00, 72])  # no RR bytes
        with patch("time.time", return_value=3.0):
            self.streamer.hr_callback(None, data)
        assert len(self.streamer.rr_intervals) == 0

    def test_get_instantaneous_hr_empty(self):
        times, hr = self.streamer.get_instantaneous_hr()
        assert len(times) == 0 and len(hr) == 0

    def test_inst_hr_range(self):
        """HR derived from plausible RR intervals should be in [40, 230] bpm.
        Upper bound is 230 (not 220) to accommodate 1/1024-s quantization error."""
        for rr_ms in [270.0, 400.0, 600.0, 800.0, 1000.0, 1500.0]:
            streamer = PolarH10Stream("TEST")
            streamer.start_time = 0.0
            with patch("time.time", return_value=10.0):
                streamer.hr_callback(None, _make_packet(60, [rr_ms]))
            _, hr_arr = streamer.get_instantaneous_hr()
            assert 40 <= hr_arr[0] <= 230, f"Unexpected HR {hr_arr[0]} for rr={rr_ms}"
