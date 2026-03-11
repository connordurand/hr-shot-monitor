# Architecture and Signal Alignment

## Signal Acquisition and Event Alignment

### Time base

All streams share a single elapsed-time axis in **seconds since session start**.

```
Wall clock
│
│  Polar H10 first notification arrives
│  ────────────────────────────────────  start_time = time.time()
│
│  t = 0 s ──────────────────────────────────────────────────────►
│            │              │                          │
│            beat           beat                       beat
│            (HR + RR)      (HR + RR)                  ...
│
│  mic callback fires every 20 ms
│  ──────────────────────────────────────────────────────────────►
│   (t_mic_0, dB)  (t_mic_1, dB)  ...  (t_mic_N, dB)
```

The microphone callback computes elapsed time from the **same** `start_time` as
the HR stream, so both sit on identical elapsed-second coordinates.

---

### Heartbeat timing from RR intervals

Each BLE notification from the Polar H10 carries:

```
byte 0 : flags
byte 1 : HR in BPM  (8-bit format)
bytes 2–3 : RR₁  (1/1024-second units, little-endian)
bytes 4–5 : RR₂
...
```

RR raw → milliseconds:

```
rr_ms = rr_raw × 1000 / 1024
```

**Beat-time reconstruction (backward walk):**

```
notification arrives at elapsed = T

t = T
for rr_ms in reversed([RR₁, RR₂, ...]):
    t  -= rr_ms / 1000
    record beat at (t, rr_ms)
reverse list → chronological beat times
```

Example with two RR intervals arriving at T = 10.0 s:

```
RR₁ = 850 ms,  RR₂ = 900 ms

Walk:
  t = 10.0 − 0.900 = 9.100   → beat B₂ at 9.100 s
  t = 9.100 − 0.850 = 8.250  → beat B₁ at 8.250 s

Reversed → chronological:
  B₁: 8.250 s,  B₂: 9.100 s
```

Instantaneous HR per beat:

```
inst_hr = 60000 / rr_ms   [BPM]
```

---

### Per-beat audio peak

For each reconstructed beat `(beat_t, rr_ms)`, the beat interval is
`[beat_t − rr_ms/1000, beat_t]`.  The code iterates directly over
`state.mic_db_buffer` (no list copy), selecting all mic samples whose
timestamp falls in that interval and breaking early once past `beat_t`.
The **maximum dB** becomes `audio_db_peak` for that beat.

```
          beat interval
          ├─────────────────────┤
 mic: ○  ●  ○  ○  ●  ●  ○  ●  ○
            ↑              ↑
          inside         inside  → peak = max of these
```

This peak is written to `session_*.csv` alongside HR, and a binary flag
`audio_over_threshold` is set to 1 when the peak >= `state.threshold.value`.

---

### Per-event audio shots

Independently of the HR path, every mic callback checks whether
`dB >= state.threshold.value`.  When true, it writes a row immediately to
`audio_events_*.csv`:

```
timestamp_iso, timestamp_epoch, elapsed_sec, audio_db
```

These rows represent the **exact acoustic timing** of candidate shots on the
audio axis.

---

### Shot labels → nearest beat

When the operator presses **HIT** or **MISS**:

1. `get_spike_times_on_hr_axis()` finds all audio-event times currently above
   threshold across ALL available audio data (`get_mic_db_window(99999.0)`)
   and projects each onto the nearest HR beat time.
2. The most recent unlabeled spike is assigned the chosen label.
3. The label is appended to the slot's `dcc.Store("shot-store-N")` — this store
   is append-only, so labels persist even when the time window changes.
4. A row is written to `shot_labels_*.csv`:

```
timestamp_iso, timestamp_epoch, shot_time_elapsed, label,
inst_hr_bpm, audio_db_peak, db_threshold
```

For offline analysis, each labeled shot can be joined to:
- `session_*.csv` on the nearest `elapsed_sec`
- `audio_events_*.csv` on the nearest `elapsed_sec`

---

## Code Architecture

### `src/config.py`

Constants only — no imports from other `src/` modules. Referenced by all other
modules. Contains:

- **Audio hardware params**: `SAMPLE_RATE` (16 kHz), `FRAME_DURATION` (20 ms),
  `MIC_BUFFER_SECONDS` (30 s), `DEFAULT_AUDIO_THRESHOLD` (70 dB).
- **BLE constants**: `HR_UUID`, `HR_BUFFER_MINUTES` (10 min).
- **USMA color palette**: ~30 semantic constants organized into groups:
  - Base palette: `USMA_BLACK`, `USMA_GOLD`, `USMA_GOLD_LIGHT`, `USMA_GRAY`, `USMA_GRAY_LIGHT`
  - Layout backgrounds: `USMA_BG_PAGE`, `USMA_BG_HEADER`, `USMA_BG_CONTROLS`, `USMA_CARD_BG`, `USMA_BG_PLOT`
  - Borders: `BORDER_SUBTLE`, `BORDER_MID`, `BORDER_STRONG`
  - Text: `TEXT_MUTED`, `TEXT_DIM`, `TEXT_SECONDARY`
  - Semantic accents: `COLOR_HIT`, `COLOR_HIT_BG`, `COLOR_MISS`, `COLOR_MISS_BG`, `COLOR_DANGER`, `COLOR_WARNING`, etc.
  - `SHOT_LABEL_COLORS` — color map for HR plot markers (`hit` → green, `miss` → gray, `None` → red)
  - `DEVICE_COLORS` — per-device trace colors (up to 4 simultaneous sensors)
- **Reusable style dicts**: `BTN_BASE`, `SECTION_LABEL_STYLE`, `PLOTLY_DARK_LAYOUT`.

### `src/state.py`

Shared mutable runtime state. Holds the per-device dictionaries
(`streamers`, `loggers`, `audio_event_loggers`, `shot_loggers`,
`collector_tasks`), the rolling `mic_db_buffer` deque (maxlen = `MIC_BUFFER_SECONDS / FRAME_DURATION` = 1500), and the `threshold`
box that all modules write to / read from.

**Why it exists:** `polar_stream` needs to read `mic_db_buffer` (owned by the
audio path) and `audio_stream` needs `streamers` (owned by the BLE path).
Importing `state` in both breaks the circular-import cycle.

### `src/polar_stream.py`

- `PolarH10Stream` — stateful per-device object. `hr_callback` does the
  RR reconstruction and per-beat audio peak calculation described above.
  Iterates directly over `mic_db_buffer` with early-break (no list copy).
  `get_instantaneous_hr()` returns NumPy arrays for plotting.
- `find_h10_devices(timeout)` — BLE scan, returns list of bleak BLEDevice.
- `collect_h10_data(addr)` — async BLE connection loop with **auto-reconnect**.
  An outer `while True` loop checks `client.is_connected` every second. When
  the connection drops, it logs the event and automatically retries (up to 3
  attempts per reconnect cycle, 2-second pause between cycles). `start_time` is
  preserved across reconnects so the elapsed time axis stays consistent.
- `start_streaming(device)` / `stop_streaming(device)` — for use from Jupyter.
  Dashboard uses `_start_collecting` / `_stop_streaming_threadsafe` instead
  (thread-safe wrappers).

### `src/audio_stream.py`

- `db_from_audio_frame(frame)` — converts PCM to a shifted dB value.
- `start_mic_stream(device_index)` — launches a `sounddevice.InputStream` that fills
  `state.mic_db_buffer` and calls `AudioEventLogger.log_event` when above threshold.
- `list_input_devices()` — returns available mic input devices for the UI dropdown.
- `get_mic_db_window(window_seconds)` — returns recent samples for plotting.
- `get_spike_times_on_hr_axis(hr_times, db_times, db_values, threshold)` —
  maps audio spikes onto the HR time axis for shot marker drawing.

### `src/logging_utils.py`

Three logger classes with identical lifecycle (`start_session` → `log_*` →
`stop_session`):

- **`SessionLogger`** — queues rows in a deque and auto-flushes every 10 seconds
  (periodic flush in `log_beat()` prevents data loss on crash). Driven by
  `PolarH10Stream.hr_callback`.
- **`AudioEventLogger`** — flushes on every write.
- **`ShotLabelLogger`** — logs hit/miss labels with full context: `inst_hr_bpm`,
  `audio_db_peak`, and `db_threshold`. Flushes on every write.

`start_recording_session(device, log_dir)` and `stop_recording_session(device)`
are convenience wrappers that create / destroy all three loggers atomically and
attach/detach the `SessionLogger` to the active streamer.

### `src/dashboard_app.py`

Dash application with a two-tab layout. Key design points:

- **BLE event loop** runs in a daemon thread (`_ble_loop`). All BLE
  coroutines are submitted from Dash callback threads via
  `asyncio.run_coroutine_threadsafe()`.

- **Slot-based layout**: up to `MAX_SLOTS` (4) pre-built device card slots,
  controlled by a `dcc.Store("slots-store")` that maps slot index → device
  address. Visibility is toggled by a single callback on `slots-store` change.

- **Per-slot callbacks** are generated in a loop so each slot's graph update,
  recording toggle, and hit/miss labeling are independent.

- **Two-tab architecture**: `dcc.Tabs` with "LIVE MONITOR" and "SHOT ANALYTICS".
  Both tab contents exist in the DOM simultaneously (visibility toggled via
  `display: block/none`) so per-slot interval callbacks keep firing even when
  the analytics tab is active.

- **Step indicator**: 4-step onboarding bar at the top of the dashboard:
  Scan → Connect → Start Mic → Record. Each step has three visual states
  (pending/active/complete) styled via CSS classes `.step-pending`,
  `.step-active`, `.step-complete`. A callback watches `slots-store`,
  `mic-active-store`, and `recording-active-store` to auto-advance.

- **Persistent shot store** (`dcc.Store(id="shot-store-N")`): Append-only list
  of `{time, label, hr, db}` dicts per slot. The `_handle_label` callback uses
  `get_mic_db_window(99999.0)` to scan ALL available audio data (not just the
  current display window), ensuring labels survive time window changes. When a
  shot is labeled, `hr` (instantaneous HR) and `db` (audio peak dB) are
  persisted in the store entry so analytics can display them even after the
  rolling `mic_db_buffer` evicts old samples.

- **Performance guards**: `_last_rr_count` and `_last_mic_count` dicts track
  data sizes per slot. If counts haven't changed since the last interval tick,
  `_update_slot` returns `no_update` for all 4 outputs (HR fig, dB fig,
  metrics grid, shot store), preventing unnecessary Plotly figure re-renders.

- **Key helpers**:
  - `_get_hr_at_time(streamer, shot_time)` — returns `(instantaneous_hr, audio_peak)`
    at the beat nearest to a given time. Used at label time to persist values
    into the shot store; analytics reads stored `hr`/`db` values first, falling
    back to this helper only for legacy entries without persisted values.
  - `_build_metrics_grid(streamer, addr, window_sec, color)` — builds per-slot
    metrics tiles (latest HR, avg HR, min/max HR, beat count, last RR).
  - `_render_hr_fig(addr, window_sec, shot_labels, db_threshold, color)` —
    renders the HR time-series with shot markers filtered to the visible window.
  - `_render_db_fig(addr, window_sec, db_threshold)` — renders the audio dB
    time-series with threshold line.
  - `_analytics_layout()` — builds the Shot Analytics tab content.

- **Stores**: `slots-store`, `shot-store-{i}` (per slot), `mic-active-store`,
  `recording-active-store`, `onboarding-step`.

- **Analytics tab contents**:
  1. Summary cards: total shots (hits + misses only), hit rate, avg HR at shot, session duration.
  2. Cumulative hit rate chart: X=elapsed time, Y=running hit %, scatter
     points colored green (hit) / red (miss).
  3. HR at shot comparison: bar chart of mean HR at hits vs misses.
  4. Shot timeline table (`dash_table.DataTable`): shot #, time, label, HR,
     dB — sortable, color-coded rows.
  5. Device selector dropdown for multi-sensor analysis.

---

## Data Artifacts

### `session_<ADDR>_<TS>.csv`

One row per reconstructed heartbeat.

| Column | Type | Description |
|---|---|---|
| `timestamp_iso` | str | ISO 8601 wall-clock timestamp |
| `timestamp_epoch` | float | UNIX epoch of the beat |
| `elapsed_sec` | float | Seconds since session start |
| `rr_ms` | float | Beat interval in milliseconds |
| `inst_hr_bpm` | float | Instantaneous HR = 60000 / rr_ms |
| `audio_db_peak` | float\|empty | Max dB in beat interval (empty if no mic data) |
| `audio_over_threshold` | 0\|1 | 1 if audio_db_peak >= threshold |

```
timestamp_iso,timestamp_epoch,elapsed_sec,rr_ms,inst_hr_bpm,audio_db_peak,audio_over_threshold
2026-02-17T09:22:28.921622,1771338148.92,1.625,841.796875,71.276,,0
```

### `audio_events_<ADDR>_<TS>.csv`

One row per mic sample that crossed the dB threshold.

| Column | Type | Description |
|---|---|---|
| `timestamp_iso` | str | Wall-clock timestamp |
| `timestamp_epoch` | float | UNIX epoch |
| `elapsed_sec` | float | Seconds since session start |
| `audio_db` | float | dB value at this sample |

### `shot_labels_<ADDR>_<TS>.csv`

One row per operator-labeled shot.

| Column | Type | Description |
|---|---|---|
| `timestamp_iso` | str | Wall-clock timestamp of label action |
| `timestamp_epoch` | float | UNIX epoch of label action |
| `shot_time_elapsed` | float | Elapsed seconds of the shot event |
| `label` | str | `"hit"` or `"miss"` |
| `inst_hr_bpm` | float\|empty | HR at nearest beat |
| `audio_db_peak` | float\|empty | Peak dB in that beat's interval |
| `db_threshold` | float | Threshold in use at label time |

**Alignment in offline analysis:**

```python
# Nearest beat for a labeled shot:
idx = (session_df["elapsed_sec"] - shot_time).abs().idxmin()
beat_row = session_df.loc[idx]
```
