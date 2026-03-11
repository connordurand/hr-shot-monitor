"""
Unit tests for audio-to-beat alignment utilities.

Tests cover:
  - Per-beat audio peak selection (the mic_db_buffer scan in hr_callback)
  - get_spike_times_on_hr_axis (projection of dB events onto HR time axis)
  - get_mic_db_window (rolling buffer windowing)
"""
import sys
from unittest.mock import MagicMock as _M

sys.modules.setdefault("bleak", _M())
sys.modules.setdefault("sounddevice", _M())

from collections import deque
from unittest.mock import patch

import numpy as np
import pytest

import src.state as state
from src.audio_stream import get_mic_db_window, get_spike_times_on_hr_axis
from src.polar_stream import PolarH10Stream


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fill_mic_buffer(samples: list[tuple[float, float]]) -> None:
    """Replace mic_db_buffer contents with the given (elapsed, dB) pairs."""
    state.mic_db_buffer.clear()
    state.mic_db_buffer.extend(samples)


def _make_packet(rr_ms: float) -> bytearray:
    rr_raw = int(round(rr_ms * 1024.0 / 1000.0))
    return bytearray([0x00, 72]) + rr_raw.to_bytes(2, "little")


# ── Per-beat audio peak tests ─────────────────────────────────────────────────

class TestPerBeatAudioPeak:
    """
    Validate that hr_callback selects the correct peak dB for each beat interval
    [beat_t - rr_ms/1000, beat_t].
    """

    def setup_method(self):
        state.mic_db_buffer.clear()
        self.streamer = PolarH10Stream("AA:BB:CC:DD:EE:FF")
        self.streamer.start_time = 0.0
        self.logged_beats: list[dict] = []

        mock_logger = _M()
        mock_logger.is_active = True

        def capture_beat(**kwargs):
            self.logged_beats.append(kwargs)

        mock_logger.log_beat.side_effect = capture_beat
        self.streamer.logger = mock_logger

    def _fire(self, rr_ms: float, elapsed: float):
        with patch("time.time", return_value=elapsed):
            self.streamer.hr_callback(None, _make_packet(rr_ms))

    # ----------------------------------------------------------------

    def test_audio_peak_inside_interval(self):
        """A mic sample that falls inside the beat interval is reflected in audio_db."""
        rr_ms = 1000.0
        elapsed = 5.0
        beat_t = elapsed - rr_ms / 1000.0          # = 4.0
        window_start = beat_t - rr_ms / 1000.0     # = 3.0
        # Place a sample in the middle of [window_start, beat_t] = [3.0, 4.0]
        mid = (window_start + beat_t) / 2          # 3.5 s
        _fill_mic_buffer([(mid, 85.0)])

        self._fire(rr_ms, elapsed)

        assert len(self.logged_beats) == 1
        assert abs(self.logged_beats[0]["audio_db"] - 85.0) < 1e-6

    def test_audio_peak_max_of_multiple(self):
        """When multiple mic samples fall in the interval, the max is used."""
        rr_ms = 1000.0
        elapsed = 5.0
        beat_t = elapsed - rr_ms / 1000.0  # 4.0 s; interval = [3.0, 4.0]
        samples = [(3.1, 65.0), (3.5, 90.0), (3.9, 72.0)]
        _fill_mic_buffer(samples)

        self._fire(rr_ms, elapsed)

        assert abs(self.logged_beats[0]["audio_db"] - 90.0) < 1e-6

    def test_no_mic_sample_in_interval(self):
        """No samples in the interval → audio_db logged as None."""
        rr_ms = 500.0
        elapsed = 5.0
        # Sample is well outside the beat interval
        _fill_mic_buffer([(0.1, 99.0)])

        self._fire(rr_ms, elapsed)

        assert self.logged_beats[0]["audio_db"] is None

    def test_over_threshold_flag_set(self):
        state.threshold.value = 70.0
        rr_ms = 1000.0
        elapsed = 5.0
        beat_t = elapsed - rr_ms / 1000.0         # 4.0; interval = [3.0, 4.0]
        _fill_mic_buffer([(beat_t - 0.5, 80.0)])  # 3.5 s — inside interval

        self._fire(rr_ms, elapsed)

        assert self.logged_beats[0]["over_thresh"] == 1

    def test_over_threshold_flag_clear(self):
        state.threshold.value = 70.0
        rr_ms = 1000.0
        elapsed = 5.0
        beat_t = elapsed - rr_ms / 1000.0
        _fill_mic_buffer([(beat_t + 0.1, 65.0)])

        self._fire(rr_ms, elapsed)

        assert self.logged_beats[0]["over_thresh"] == 0


# ── get_spike_times_on_hr_axis tests ─────────────────────────────────────────

class TestGetSpikeTimesOnHRAxis:
    def test_no_hr_data_returns_empty(self):
        result = get_spike_times_on_hr_axis([], [1.0], [80.0], 70.0)
        assert result == []

    def test_no_db_data_returns_empty(self):
        result = get_spike_times_on_hr_axis([1.0, 2.0], [], [], 70.0)
        assert result == []

    def test_spike_maps_to_nearest_beat(self):
        hr_times = [1.0, 2.0, 3.0]
        db_times = [1.9]
        db_values = [80.0]
        result = get_spike_times_on_hr_axis(hr_times, db_times, db_values, 70.0)
        assert result == [2.0], f"Expected [2.0], got {result}"

    def test_below_threshold_ignored(self):
        hr_times = [1.0, 2.0]
        db_times = [1.5]
        db_values = [60.0]  # below 70 dB
        result = get_spike_times_on_hr_axis(hr_times, db_times, db_values, 70.0)
        assert result == []

    def test_duplicate_beats_deduplicated(self):
        """Multiple dB spikes mapping to the same HR beat → one entry."""
        hr_times = [1.0, 2.0]
        db_times = [1.8, 1.9]
        db_values = [80.0, 85.0]
        result = get_spike_times_on_hr_axis(hr_times, db_times, db_values, 70.0)
        assert result == [2.0]
        assert len(result) == 1

    def test_multiple_distinct_spikes(self):
        hr_times = [1.0, 2.0, 3.0, 4.0]
        db_times = [1.1, 3.1]
        db_values = [90.0, 90.0]
        result = get_spike_times_on_hr_axis(hr_times, db_times, db_values, 70.0)
        assert result == [1.0, 3.0]

    def test_result_is_sorted(self):
        hr_times = [1.0, 2.0, 3.0]
        db_times = [2.9, 1.1]   # out of time order
        db_values = [80.0, 80.0]
        result = get_spike_times_on_hr_axis(hr_times, db_times, db_values, 70.0)
        assert result == sorted(result)


# ── get_mic_db_window tests ────────────────────────────────────────────────────

class TestGetMicDbWindow:
    def setup_method(self):
        state.mic_db_buffer.clear()

    def _mock_elapsed(self, val: float):
        """Patch _get_elapsed_time inside audio_stream."""
        return patch("src.audio_stream._get_elapsed_time", return_value=val)

    def test_empty_buffer_returns_empty(self):
        with self._mock_elapsed(10.0):
            times, values = get_mic_db_window(30.0)
        assert times == [] and values == []

    def test_returns_samples_within_window(self):
        _fill_mic_buffer([(5.0, 60.0), (8.0, 65.0), (10.0, 70.0)])
        with self._mock_elapsed(10.0):
            times, values = get_mic_db_window(5.0)  # window: [5.0, 10.0]
        assert set(times) == {5.0, 8.0, 10.0}

    def test_excludes_old_samples(self):
        _fill_mic_buffer([(1.0, 55.0), (8.0, 65.0)])
        with self._mock_elapsed(10.0):
            times, values = get_mic_db_window(5.0)  # cutoff = 5.0
        assert 1.0 not in times
        assert 8.0 in times

    def test_all_samples_in_window(self):
        _fill_mic_buffer([(7.0, 60.0), (8.0, 65.0), (9.0, 70.0)])
        with self._mock_elapsed(10.0):
            times, values = get_mic_db_window(60.0)
        assert len(times) == 3

    def test_window_of_zero_returns_nothing(self):
        _fill_mic_buffer([(5.0, 60.0)])
        with self._mock_elapsed(10.0):
            times, values = get_mic_db_window(0.0)
        assert times == []
