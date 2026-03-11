"""
USMA Polar H10 Shot Monitor — Dash dashboard.

Run with:
    python -m src.dashboard_app          (from the capstone/ root)

Architecture:
  - A dedicated asyncio event loop runs in a background daemon thread.
    All BLE coroutines (scan, connect) are scheduled into it from Dash
    callbacks via asyncio.run_coroutine_threadsafe().
  - Up to MAX_SLOTS (4) devices can stream simultaneously.
    Slots are pre-built in the layout; visibility is toggled via
    a single dcc.Store("slots-store") that maps slot index → device address.
  - Per-slot callbacks (generated in a loop) handle graph updates,
    recording toggle, and hit/miss labeling independently.
  - Two tabs: "LIVE MONITOR" (real-time streaming) and "SHOT ANALYTICS"
    (detailed performance metrics).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import numpy as np
import plotly.graph_objs as go
from dash import ALL, Dash, Input, Output, State, dcc, html, no_update, dash_table
from dash.exceptions import PreventUpdate

import src.state as state
import sounddevice as sd

from src.audio_stream import (
    get_mic_db_window,
    get_spike_times_on_hr_axis,
    list_input_devices,
    start_mic_stream,
)
from src.config import (
    BORDER_MID,
    BORDER_SUBTLE,
    BTN_BASE,
    COLOR_DANGER,
    COLOR_DANGER_DARK,
    COLOR_ERROR_TEXT,
    COLOR_HIT,
    COLOR_HIT_BG,
    COLOR_HIT_BRIGHT,
    COLOR_HIT_TEXT,
    COLOR_MISS,
    COLOR_MISS_BG,
    COLOR_MISS_BRIGHT,
    COLOR_MISS_TEXT,
    COLOR_WARNING,
    DEFAULT_AUDIO_THRESHOLD,
    DEVICE_COLORS,
    PLOTLY_DARK_LAYOUT,
    SECTION_LABEL_STYLE,
    SHOT_LABEL_COLORS,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_SECONDARY,
    USMA_BG_CONTROLS,
    USMA_BG_PAGE,
    USMA_CARD_BG,
    USMA_GOLD,
    USMA_GOLD_LIGHT,
    USMA_GRAY_LIGHT,
)
from src.logging_utils import start_recording_session, stop_recording_session
from src.polar_stream import PolarH10Stream, collect_h10_data, find_h10_devices

# ── Paths ────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent
ASSETS_DIR = str(_ROOT / "assets")
LOG_DIR = str(_ROOT / "logs")

# ── Background BLE event loop ────────────────────────────────────────────────
_ble_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
threading.Thread(
    target=_ble_loop.run_forever, daemon=True, name="ble-loop"
).start()

MAX_SLOTS = 4

# Guard against overlapping BLE scans
_scan_in_progress = False

# Microphone stream state
_mic_stream: sd.InputStream | None = None
_mic_device_index: int | None = None

# Performance: track last data sizes to skip unchanged re-renders
_last_rr_count: dict[int, int] = {}
_last_mic_count: dict[int, int] = {}


# ── Figure helpers ────────────────────────────────────────────────────────────

def _empty_fig(title: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(title=title, **PLOTLY_DARK_LAYOUT)
    return fig


def _render_hr_fig(
    addr: str, window_sec: float, shot_labels: list, db_threshold: float, color: str
) -> go.Figure:
    """Render the HR time-series with shot markers filtered to the visible window."""
    streamer = state.streamers.get(addr)
    if streamer is None:
        return _empty_fig("Heart Rate — no data yet")

    times, inst_hr = streamer.get_instantaneous_hr()
    if len(times) == 0:
        return _empty_fig("Heart Rate — waiting for data...")

    max_t = float(times[-1])
    window_start = max_t - window_sec
    mask = times >= window_start
    t_win = times[mask]
    hr_win = inst_hr[mask]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=list(t_win),
            y=list(hr_win),
            mode="lines",
            line=dict(color=color, width=2),
            name="HR",
            hovertemplate="t=%{x:.1f}s  %{y:.0f} bpm<extra></extra>",
        )
    )

    # Shot markers — only show those within the visible window
    for shot in shot_labels:
        if shot["time"] < window_start:
            continue
        fig.add_vline(
            x=shot["time"],
            line_width=1,
            line_dash="dash",
            line_color=SHOT_LABEL_COLORS.get(shot.get("label"), COLOR_DANGER),
        )

    fig.update_layout(
        title=f"Heart Rate ({addr[-8:]})",
        xaxis_title="Elapsed (s)",
        yaxis_title="BPM",
        showlegend=False,
        **PLOTLY_DARK_LAYOUT,
    )
    return fig


def _render_db_fig(window_sec: float, db_threshold: float, color: str) -> go.Figure:
    """Render the microphone dB time-series with threshold line."""
    db_times, db_values = get_mic_db_window(window_sec)
    fig = go.Figure()

    if db_times:
        point_colors = [
            COLOR_DANGER if v >= db_threshold else color for v in db_values
        ]
        fig.add_trace(
            go.Scatter(
                x=db_times,
                y=db_values,
                mode="lines+markers",
                marker=dict(color=point_colors, size=4),
                line=dict(color=color, width=1),
                name="Mic dB",
                hovertemplate="t=%{x:.1f}s  %{y:.1f} dB<extra></extra>",
            )
        )
        fig.add_hline(
            y=db_threshold,
            line_width=1.5,
            line_dash="dash",
            line_color=USMA_GOLD,
            annotation_text=f"Threshold {db_threshold:.0f} dB",
            annotation_position="top right",
            annotation_font_color=USMA_GOLD,
        )

    fig.update_layout(
        title="Microphone dB",
        xaxis_title="Elapsed (s)",
        yaxis_title="dB",
        showlegend=False,
        **PLOTLY_DARK_LAYOUT,
    )
    return fig


# ── Shared helpers ────────────────────────────────────────────────────────────

def _get_hr_at_time(streamer, shot_time: float) -> tuple[float | None, float | None]:
    """Return (instantaneous_hr, audio_peak) at the beat nearest to shot_time."""
    if not streamer or not streamer.rr_intervals:
        return None, None
    rr_times = np.array([t for t, _ in streamer.rr_intervals])
    rr_ms_arr = np.array([rr for _, rr in streamer.rr_intervals])
    idx = int(np.argmin(np.abs(rr_times - shot_time)))
    rr_ms = float(rr_ms_arr[idx])
    inst_hr = 60000.0 / rr_ms if rr_ms > 0 else None
    beat_t = float(rr_times[idx])
    ws = beat_t - rr_ms / 1000.0
    audio_peak = None
    for ts, db in state.mic_db_buffer:
        if ts < ws:
            continue
        if ts > beat_t:
            break
        if audio_peak is None or db > audio_peak:
            audio_peak = db
    return inst_hr, audio_peak


# ── BLE helpers ──────────────────────────────────────────────────────────────

async def _start_collecting(addr: str) -> None:
    """Create a PolarH10Stream and run the BLE connection loop."""
    if addr not in state.streamers:
        state.streamers[addr] = PolarH10Stream(addr)
    try:
        task = asyncio.ensure_future(collect_h10_data(addr))
        state.collector_tasks[addr] = task
        await task
    except Exception as e:
        logging.error("BLE collection error for %s: %s", addr, e)
        streamer = state.streamers.get(addr)
        if streamer:
            streamer.is_connected = False


def _stop_streaming_threadsafe(addr: str) -> None:
    task = state.collector_tasks.pop(addr, None)
    if task is not None:
        _ble_loop.call_soon_threadsafe(task.cancel)
    streamer = state.streamers.pop(addr, None)
    if streamer:
        streamer.is_connected = False


# ── Metric tile helper ────────────────────────────────────────────────────────

def _metric_tile(label: str, value: str, value_color: str = USMA_GOLD_LIGHT) -> html.Div:
    return html.Div(
        style={
            "padding": "12px 8px",
            "textAlign": "center",
            "borderRight": f"1px solid {BORDER_SUBTLE}",
        },
        children=[
            html.Div(
                label,
                style={
                    "color": TEXT_MUTED,
                    "fontSize": "0.62rem",
                    "letterSpacing": "0.12em",
                    "textTransform": "uppercase",
                    "marginBottom": "4px",
                },
            ),
            html.Div(
                value,
                style={
                    "color": value_color,
                    "fontSize": "1.1rem",
                    "fontWeight": 700,
                    "lineHeight": "1",
                },
            ),
        ],
    )


def _build_metrics_grid(streamer, window_sec: float, shots: list) -> html.Div:
    """Build the metrics tile grid for a slot card."""
    hit_count = sum(1 for s in shots if s.get("label") == "hit")
    miss_count = sum(1 for s in shots if s.get("label") == "miss")
    total = hit_count + miss_count

    tiles: list[html.Div] = []
    if streamer and streamer.rr_intervals:
        rr_vals = [rr for _, rr in streamer.rr_intervals]
        times_arr, hr_arr = streamer.get_instantaneous_hr()

        latest_rr = rr_vals[-1]
        if latest_rr > 0:
            tiles.append(_metric_tile("CURRENT", f"{60000.0/latest_rr:.0f} bpm", USMA_GOLD))

        if len(times_arr) > 0:
            max_t = float(times_arr[-1])
            mask = times_arr >= max_t - window_sec
            hr_win = hr_arr[mask]
            if len(hr_win) > 0:
                tiles.append(_metric_tile("AVG HR", f"{hr_win.mean():.0f} bpm"))
                tiles.append(_metric_tile("MAX HR", f"{hr_win.max():.0f} bpm"))
                tiles.append(_metric_tile("MIN HR", f"{hr_win.min():.0f} bpm"))
            m, s = divmod(int(max_t), 60)
            tiles.append(_metric_tile("SESSION", f"{m}:{s:02d}"))

        if len(rr_vals) > 4:
            sdnn = float(np.std(rr_vals[-min(len(rr_vals), 300):]))
            tiles.append(_metric_tile("HRV (SDNN)", f"{sdnn:.0f} ms"))

    tiles.append(_metric_tile("SHOTS", str(total)))
    tiles.append(_metric_tile("HITS", str(hit_count), COLOR_HIT_BRIGHT if hit_count else USMA_GOLD_LIGHT))
    tiles.append(_metric_tile("MISSES", str(miss_count), COLOR_MISS_BRIGHT if miss_count else USMA_GOLD_LIGHT))
    if total:
        pct = 100 * hit_count // total
        acc_color = COLOR_HIT_BRIGHT if pct >= 60 else COLOR_WARNING if pct >= 40 else COLOR_MISS_BRIGHT
        tiles.append(_metric_tile("ACCURACY", f"{pct}%", acc_color))
    else:
        tiles.append(_metric_tile("ACCURACY", "---"))

    n_cols = max(len(tiles), 1)
    return html.Div(
        tiles,
        style={
            "display": "grid",
            "gridTemplateColumns": f"repeat({n_cols}, 1fr)",
            "backgroundColor": USMA_CARD_BG,
            "borderTop": f"1px solid {BORDER_SUBTLE}",
        },
    )


# ── Step indicator ────────────────────────────────────────────────────────────

def _step_circle(num: int, label: str) -> html.Div:
    return html.Div(
        style={"display": "flex", "alignItems": "center", "gap": "8px"},
        children=[
            html.Div(
                str(num),
                id=f"step-circle-{num}",
                className="step-pending",
                style={
                    "width": "28px",
                    "height": "28px",
                    "borderRadius": "50%",
                    "display": "flex",
                    "alignItems": "center",
                    "justifyContent": "center",
                    "fontSize": "0.75rem",
                    "fontWeight": 700,
                    "transition": "all 0.3s ease",
                },
            ),
            html.Span(
                label,
                id=f"step-label-{num}",
                style={
                    "fontSize": "0.72rem",
                    "fontWeight": 600,
                    "letterSpacing": "0.1em",
                    "color": BORDER_MID,
                    "transition": "color 0.3s ease",
                },
            ),
        ],
    )


# ── Slot card builder ─────────────────────────────────────────────────────────

def _slot_card(slot_idx: int) -> html.Div:
    color = DEVICE_COLORS[slot_idx % len(DEVICE_COLORS)]
    return html.Div(
        id=f"slot-card-{slot_idx}",
        style={"display": "none"},
        children=[
            # Card header with left accent border
            html.Div(
                style={
                    "display": "flex",
                    "justifyContent": "space-between",
                    "alignItems": "center",
                    "padding": "12px 18px",
                    "backgroundColor": USMA_BG_CONTROLS,
                    "borderLeft": f"4px solid {color}",
                    "borderBottom": f"1px solid {BORDER_SUBTLE}",
                    "borderRadius": "8px 8px 0 0",
                },
                children=[
                    html.Div(
                        style={"display": "flex", "alignItems": "center"},
                        children=[
                            html.Span(
                                id=f"recording-indicator-{slot_idx}",
                                style={"display": "none"},
                            ),
                            html.Span(
                                id=f"device-name-{slot_idx}",
                                style={
                                    "color": USMA_GOLD_LIGHT,
                                    "fontWeight": 600,
                                    "fontSize": "0.95rem",
                                    "fontFamily": "monospace",
                                },
                            ),
                        ],
                    ),
                    html.Div(
                        id=f"hr-badge-{slot_idx}",
                        style={
                            "color": USMA_GOLD,
                            "fontWeight": 700,
                            "fontSize": "1.6rem",
                            "letterSpacing": "0.04em",
                        },
                    ),
                    html.Div(
                        [
                            html.Button(
                                "Start Recording",
                                id=f"record-btn-{slot_idx}",
                                n_clicks=0,
                                title="Start logging HR + audio data to CSV",
                                style={
                                    **BTN_BASE,
                                    "backgroundColor": USMA_GOLD,
                                    "color": "#000",
                                    "padding": "6px 14px",
                                    "marginRight": "8px",
                                },
                            ),
                            html.Button(
                                "Disconnect",
                                id=f"disconnect-btn-{slot_idx}",
                                n_clicks=0,
                                title="Disconnect this sensor",
                                style={
                                    **BTN_BASE,
                                    "backgroundColor": BORDER_MID,
                                    "color": USMA_GRAY_LIGHT,
                                    "border": f"1px solid {BORDER_MID}",
                                    "padding": "6px 14px",
                                },
                            ),
                        ]
                    ),
                ],
            ),
            # Plots
            html.Div(
                style={"padding": "10px 10px 0", "backgroundColor": USMA_CARD_BG},
                children=[
                    dcc.Graph(
                        id=f"hr-graph-{slot_idx}",
                        style={"height": "230px"},
                        config={"displayModeBar": False},
                        figure=_empty_fig("Heart Rate — waiting for connection..."),
                    ),
                    dcc.Graph(
                        id=f"db-graph-{slot_idx}",
                        style={"height": "175px"},
                        config={"displayModeBar": False},
                        figure=_empty_fig("Microphone dB — mic not started"),
                    ),
                ],
            ),
            # Metrics grid
            html.Div(
                id=f"summary-{slot_idx}",
                style={"borderTop": f"1px solid {BORDER_SUBTLE}", "backgroundColor": USMA_CARD_BG},
            ),
            # HIT / MISS row
            html.Div(
                style={
                    "display": "flex",
                    "justifyContent": "center",
                    "alignItems": "center",
                    "gap": "48px",
                    "padding": "20px 24px 22px",
                    "backgroundColor": USMA_BG_PAGE,
                    "borderRadius": "0 0 8px 8px",
                    "borderTop": f"1px solid {BORDER_SUBTLE}",
                },
                children=[
                    html.Button(
                        "HIT",
                        id=f"hit-btn-{slot_idx}",
                        n_clicks=0,
                        title="Label the most recent detected shot as a HIT",
                        style={
                            **BTN_BASE,
                            "backgroundColor": COLOR_HIT_BG,
                            "color": COLOR_HIT_TEXT,
                            "border": f"2px solid {COLOR_HIT}",
                            "padding": "18px 72px",
                            "fontSize": "1.3rem",
                            "fontWeight": 800,
                            "letterSpacing": "0.14em",
                            "borderRadius": "6px",
                        },
                    ),
                    html.Button(
                        "MISS",
                        id=f"miss-btn-{slot_idx}",
                        n_clicks=0,
                        title="Label the most recent detected shot as a MISS",
                        style={
                            **BTN_BASE,
                            "backgroundColor": COLOR_MISS_BG,
                            "color": COLOR_MISS_TEXT,
                            "border": f"2px solid {COLOR_MISS}",
                            "padding": "18px 72px",
                            "fontSize": "1.3rem",
                            "fontWeight": 800,
                            "letterSpacing": "0.14em",
                            "borderRadius": "6px",
                        },
                    ),
                ],
            ),
            # Per-slot stores
            dcc.Store(id=f"shot-store-{slot_idx}", data=[]),
        ],
    )


# ── Analytics tab layout ─────────────────────────────────────────────────────

def _analytics_layout() -> html.Div:
    """Build the Shot Analytics tab content."""
    card_style = {
        "backgroundColor": USMA_BG_CONTROLS,
        "borderRadius": "8px",
        "border": f"1px solid {BORDER_MID}",
        "padding": "16px 20px",
        "flex": "1",
        "minWidth": "140px",
    }
    return html.Div(
        style={"padding": "20px 24px"},
        children=[
            # Device selector
            html.Div(
                style={"marginBottom": "16px", "display": "flex", "alignItems": "center", "gap": "16px"},
                children=[
                    html.Div("DEVICE", style=SECTION_LABEL_STYLE),
                    dcc.Dropdown(
                        id="analytics-device-select",
                        placeholder="Select device...",
                        clearable=False,
                        style={"color": "#000", "width": "280px"},
                    ),
                ],
            ),
            # Summary cards row
            html.Div(
                id="analytics-summary",
                style={
                    "display": "flex",
                    "gap": "12px",
                    "marginBottom": "20px",
                    "flexWrap": "wrap",
                },
                children=[
                    html.Div(id="stat-total-shots", style=card_style),
                    html.Div(id="stat-hit-rate", style=card_style),
                    html.Div(id="stat-avg-hr", style=card_style),
                    html.Div(id="stat-session-dur", style=card_style),
                ],
            ),
            # Charts row
            html.Div(
                style={"display": "flex", "gap": "16px", "marginBottom": "20px", "flexWrap": "wrap"},
                children=[
                    html.Div(
                        style={"flex": "1", "minWidth": "400px"},
                        children=dcc.Graph(
                            id="analytics-ratio-graph",
                            style={"height": "300px"},
                            config={"displayModeBar": False},
                            figure=_empty_fig("Cumulative Hit Rate"),
                        ),
                    ),
                    html.Div(
                        style={"flex": "1", "minWidth": "400px"},
                        children=dcc.Graph(
                            id="analytics-hr-graph",
                            style={"height": "300px"},
                            config={"displayModeBar": False},
                            figure=_empty_fig("HR at Shot Time"),
                        ),
                    ),
                ],
            ),
            # Shot timeline table
            html.Div(
                [
                    html.Div("SHOT TIMELINE", style={**SECTION_LABEL_STYLE, "marginBottom": "10px"}),
                    dash_table.DataTable(
                        id="analytics-table",
                        columns=[
                            {"name": "#", "id": "shot_num"},
                            {"name": "Time (s)", "id": "time"},
                            {"name": "Label", "id": "label"},
                            {"name": "HR (bpm)", "id": "hr"},
                            {"name": "Audio dB", "id": "db"},
                        ],
                        data=[],
                        style_table={"overflowX": "auto"},
                        style_header={
                            "backgroundColor": USMA_BG_CONTROLS,
                            "color": USMA_GOLD,
                            "fontWeight": 700,
                            "fontSize": "0.75rem",
                            "letterSpacing": "0.1em",
                            "border": f"1px solid {BORDER_SUBTLE}",
                        },
                        style_cell={
                            "backgroundColor": USMA_CARD_BG,
                            "color": USMA_GOLD_LIGHT,
                            "fontSize": "0.82rem",
                            "border": f"1px solid {BORDER_SUBTLE}",
                            "padding": "8px 12px",
                            "textAlign": "center",
                        },
                        style_data_conditional=[
                            {
                                "if": {"filter_query": '{label} = "hit"'},
                                "backgroundColor": "#0a1a0a",
                                "color": COLOR_HIT_BRIGHT,
                            },
                            {
                                "if": {"filter_query": '{label} = "miss"'},
                                "backgroundColor": "#1a0a0a",
                                "color": COLOR_MISS_BRIGHT,
                            },
                        ],
                        sort_action="native",
                        page_size=20,
                    ),
                ],
                style={
                    "backgroundColor": USMA_BG_CONTROLS,
                    "borderRadius": "8px",
                    "border": f"1px solid {BORDER_MID}",
                    "padding": "16px",
                },
            ),
        ],
    )


# ── Tab styling ──────────────────────────────────────────────────────────────

_TAB_STYLE = {
    "backgroundColor": USMA_BG_PAGE,
    "color": TEXT_SECONDARY,
    "border": "none",
    "borderBottom": f"2px solid transparent",
    "padding": "10px 28px",
    "fontSize": "0.78rem",
    "fontWeight": 700,
    "letterSpacing": "0.16em",
}
_TAB_SELECTED = {
    **_TAB_STYLE,
    "backgroundColor": USMA_BG_CONTROLS,
    "color": USMA_GOLD,
    "borderBottom": f"2px solid {USMA_GOLD}",
}


# ── App layout ────────────────────────────────────────────────────────────────

app = Dash(__name__, assets_folder=ASSETS_DIR, title="H10 Shot Monitor")
app.layout = html.Div(
    style={
        "backgroundColor": USMA_BG_PAGE,
        "minHeight": "100vh",
        "fontFamily": "system-ui, -apple-system, 'Segoe UI', sans-serif",
    },
    children=[
        # ── Header ──────────────────────────────────────────────────────
        html.Div(
            style={
                "background": f"linear-gradient(180deg, {USMA_BG_CONTROLS} 0%, #000 100%)",
                "borderBottom": f"3px solid {USMA_GOLD}",
                "padding": "12px 28px",
                "display": "flex",
                "alignItems": "center",
                "justifyContent": "space-between",
            },
            children=[
                html.Img(
                    src="/assets/DSE_Logo_Color.png",
                    style={"height": "96px", "objectFit": "contain"},
                ),
                html.Div(
                    [
                        html.H1(
                            "POLAR H10 SHOT MONITOR",
                            style={
                                "color": USMA_GOLD,
                                "margin": "0",
                                "fontSize": "1.35rem",
                                "fontWeight": 800,
                                "letterSpacing": "0.18em",
                            },
                        ),
                        html.P(
                            "REAL-TIME BIOMETRIC SHOT ANALYSIS",
                            style={
                                "color": TEXT_DIM,
                                "margin": "4px 0 0",
                                "fontSize": "0.65rem",
                                "letterSpacing": "0.2em",
                                "textAlign": "center",
                            },
                        ),
                        html.P(
                            "U.S. Military Academy  \u00b7  West Point",
                            style={
                                "color": TEXT_DIM,
                                "margin": "2px 0 0",
                                "fontSize": "0.7rem",
                                "letterSpacing": "0.12em",
                                "textAlign": "center",
                            },
                        ),
                    ],
                    style={"textAlign": "center"},
                ),
                html.Img(
                    src="/assets/usma_logo.png",
                    style={"height": "96px", "objectFit": "contain", "filter": "brightness(0) invert(1)"},
                ),
            ],
        ),
        # ── Step indicator bar ─────────────────────────────────────────
        html.Div(
            style={
                "display": "flex",
                "justifyContent": "center",
                "gap": "36px",
                "padding": "10px 24px",
                "backgroundColor": USMA_CARD_BG,
                "borderBottom": f"1px solid {BORDER_SUBTLE}",
            },
            children=[
                _step_circle(1, "SCAN"),
                html.Span("\u25B6", style={"color": BORDER_MID, "fontSize": "0.6rem", "alignSelf": "center"}),
                _step_circle(2, "CONNECT"),
                html.Span("\u25B6", style={"color": BORDER_MID, "fontSize": "0.6rem", "alignSelf": "center"}),
                _step_circle(3, "START MIC"),
                html.Span("\u25B6", style={"color": BORDER_MID, "fontSize": "0.6rem", "alignSelf": "center"}),
                _step_circle(4, "RECORD"),
            ],
        ),
        # ── Controls bar ────────────────────────────────────────────────
        html.Div(
            style={
                "display": "flex",
                "gap": "16px",
                "padding": "14px 24px",
                "backgroundColor": USMA_BG_CONTROLS,
                "borderBottom": f"1px solid {BORDER_MID}",
                "flexWrap": "wrap",
            },
            children=[
                # Device scan panel
                html.Div(
                    style={
                        "flex": "1.2",
                        "minWidth": "220px",
                        "borderRight": f"1px solid {BORDER_SUBTLE}",
                        "paddingRight": "16px",
                    },
                    children=[
                        html.Div("DEVICES", style=SECTION_LABEL_STYLE),
                        dcc.Loading(
                            type="circle",
                            color=USMA_GOLD,
                            children=html.Button(
                                "Scan for Devices",
                                id="scan-btn",
                                n_clicks=0,
                                title="Search for nearby Polar H10 Bluetooth devices (~8s)",
                                style={
                                    **BTN_BASE,
                                    "backgroundColor": BORDER_SUBTLE,
                                    "color": USMA_GOLD_LIGHT,
                                    "border": f"1px solid {USMA_GOLD}",
                                    "padding": "7px 16px",
                                    "marginBottom": "8px",
                                },
                            ),
                        ),
                        html.Div(id="scan-results-div"),
                        html.Div(
                            id="connect-btn-wrap",
                            style={"display": "none"},
                            children=html.Button(
                                "Connect Selected",
                                id="connect-btn",
                                n_clicks=0,
                                title="Connect to the selected Polar H10 devices",
                                style={
                                    **BTN_BASE,
                                    "backgroundColor": USMA_GOLD,
                                    "color": "#000",
                                    "padding": "7px 18px",
                                    "marginTop": "8px",
                                },
                            ),
                        ),
                    ],
                ),
                # Microphone device selector + start/stop
                html.Div(
                    style={
                        "flex": "1.2",
                        "minWidth": "240px",
                        "borderRight": f"1px solid {BORDER_SUBTLE}",
                        "paddingRight": "16px",
                    },
                    children=[
                        html.Div("MICROPHONE", style=SECTION_LABEL_STYLE),
                        dcc.Dropdown(
                            id="mic-device-dropdown",
                            options=[
                                {"label": f"[{d['index']}] {d['name']}", "value": d["index"]}
                                for d in list_input_devices()
                            ],
                            placeholder="Select input device...",
                            value=None,
                            clearable=True,
                            style={"color": "#000", "marginBottom": "6px"},
                        ),
                        html.Div(
                            style={"display": "flex", "alignItems": "center", "gap": "10px"},
                            children=[
                                html.Button(
                                    "Start Mic",
                                    id="mic-btn",
                                    n_clicks=0,
                                    title="Start capturing audio from the selected microphone",
                                    style={
                                        **BTN_BASE,
                                        "backgroundColor": USMA_GOLD,
                                        "color": "#000",
                                        "padding": "6px 14px",
                                    },
                                ),
                                html.Span(
                                    id="mic-status",
                                    children="Stopped",
                                    style={"color": TEXT_MUTED, "fontSize": "0.85rem", "fontWeight": 600},
                                ),
                            ],
                        ),
                    ],
                ),
                # dB threshold
                html.Div(
                    style={
                        "flex": "1",
                        "minWidth": "200px",
                        "borderRight": f"1px solid {BORDER_SUBTLE}",
                        "paddingRight": "16px",
                    },
                    children=[
                        html.Div("dB THRESHOLD", style=SECTION_LABEL_STYLE),
                        dcc.Slider(
                            id="db-threshold-slider",
                            min=40,
                            max=100,
                            step=5,
                            value=DEFAULT_AUDIO_THRESHOLD,
                            marks={
                                40: {"label": "40", "style": {"color": TEXT_DIM}},
                                70: {"label": "70", "style": {"color": USMA_GOLD}},
                                100: {"label": "100", "style": {"color": TEXT_DIM}},
                            },
                            tooltip={"placement": "top", "always_visible": True},
                        ),
                    ],
                ),
                # Time window
                html.Div(
                    style={
                        "flex": "0.8",
                        "minWidth": "140px",
                        "borderRight": f"1px solid {BORDER_SUBTLE}",
                        "paddingRight": "16px",
                    },
                    children=[
                        html.Div("TIME WINDOW", style=SECTION_LABEL_STYLE),
                        dcc.Dropdown(
                            id="window-dropdown",
                            options=[
                                {"label": "30 s", "value": 30},
                                {"label": "60 s", "value": 60},
                                {"label": "120 s", "value": 120},
                                {"label": "300 s", "value": 300},
                            ],
                            value=60,
                            clearable=False,
                            style={"color": "#000"},
                        ),
                    ],
                ),
                # Update rate
                html.Div(
                    style={"flex": "0.8", "minWidth": "160px"},
                    children=[
                        html.Div("UPDATE RATE", style=SECTION_LABEL_STYLE),
                        dcc.Slider(
                            id="interval-slider",
                            min=500,
                            max=3000,
                            step=500,
                            value=1000,
                            marks={
                                500: {"label": "0.5s", "style": {"color": TEXT_DIM}},
                                1000: {"label": "1s", "style": {"color": USMA_GOLD}},
                                2000: {"label": "2s", "style": {"color": TEXT_DIM}},
                                3000: {"label": "3s", "style": {"color": TEXT_DIM}},
                            },
                            tooltip={"placement": "top", "always_visible": True},
                        ),
                    ],
                ),
            ],
        ),
        # ── Tabs ────────────────────────────────────────────────────────
        dcc.Tabs(
            id="main-tabs",
            value="live",
            children=[
                dcc.Tab(label="LIVE MONITOR", value="live", style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                dcc.Tab(label="SHOT ANALYTICS", value="analytics", style=_TAB_STYLE, selected_style=_TAB_SELECTED),
            ],
            style={"borderBottom": f"1px solid {BORDER_SUBTLE}"},
        ),
        # ── Live monitor tab content ────────────────────────────────────
        html.Div(
            id="live-tab-content",
            style={"display": "block"},
            children=[
                html.Div(id="comparison-panel", style={"display": "none"}),
                html.Div(
                    style={"padding": "16px 24px", "display": "flex", "flexDirection": "column", "gap": "16px"},
                    children=[_slot_card(i) for i in range(MAX_SLOTS)]
                    + [
                        html.Div(
                            id="no-sensors-msg",
                            children=html.P(
                                "No sensors connected. Use the controls above to scan for devices, connect, and start monitoring.",
                                style={
                                    "color": BORDER_MID,
                                    "textAlign": "center",
                                    "padding": "80px 0",
                                    "fontSize": "0.95rem",
                                    "letterSpacing": "0.08em",
                                },
                            ),
                        )
                    ],
                ),
            ],
        ),
        # ── Analytics tab content ───────────────────────────────────────
        html.Div(
            id="analytics-tab-content",
            style={"display": "none"},
            children=[_analytics_layout()],
        ),
        # ── Global stores & interval ─────────────────────────────────────
        dcc.Store(id="scan-store", data=[]),
        dcc.Store(id="slots-store", data={}),
        dcc.Store(id="threshold-sync", data=DEFAULT_AUDIO_THRESHOLD),
        dcc.Store(id="mic-active-store", data=False),
        dcc.Store(id="recording-active-store", data=False),
        dcc.Interval(id="main-interval", interval=1000, n_intervals=0),
    ],
)


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACKS
# ══════════════════════════════════════════════════════════════════════════════

# ── Tab visibility toggle ────────────────────────────────────────────────────

@app.callback(
    Output("live-tab-content", "style"),
    Output("analytics-tab-content", "style"),
    Input("main-tabs", "value"),
)
def toggle_tabs(tab):
    if tab == "live":
        return {"display": "block"}, {"display": "none"}
    return {"display": "none"}, {"display": "block"}


# ── Step indicator updates ───────────────────────────────────────────────────

@app.callback(
    *[Output(f"step-circle-{i}", "className") for i in range(1, 5)],
    *[Output(f"step-label-{i}", "style") for i in range(1, 5)],
    Input("main-interval", "n_intervals"),
    State("slots-store", "data"),
    State("mic-active-store", "data"),
    State("recording-active-store", "data"),
)
def update_step_indicator(n, slots_data, mic_active, rec_active):
    """Advance the step indicator based on system state."""
    slots_data = slots_data or {}
    has_devices = bool(slots_data)
    mic_on = bool(mic_active)
    recording = bool(rec_active)

    # Determine current step: always at least step 1 (scan)
    step = 1
    if has_devices:
        step = 2
    if has_devices and mic_on:
        step = 3
    if has_devices and mic_on and recording:
        step = 4

    classes = []
    label_styles = []
    gold_style = {"fontSize": "0.72rem", "fontWeight": 600, "letterSpacing": "0.1em", "color": USMA_GOLD, "transition": "color 0.3s ease"}
    dim_style = {"fontSize": "0.72rem", "fontWeight": 600, "letterSpacing": "0.1em", "color": BORDER_MID, "transition": "color 0.3s ease"}

    for i in range(1, 5):
        if i < step:
            classes.append("step-complete")
            label_styles.append(gold_style)
        elif i == step:
            classes.append("step-active")
            label_styles.append(gold_style)
        else:
            classes.append("step-pending")
            label_styles.append(dim_style)

    return *classes, *label_styles


# ── Interval and threshold sync ──────────────────────────────────────────────

@app.callback(
    Output("main-interval", "interval"),
    Input("interval-slider", "value"),
)
def update_interval(val):
    return int(val or 1000)


@app.callback(
    Output("threshold-sync", "data"),
    Input("db-threshold-slider", "value"),
)
def sync_threshold(val):
    state.threshold.value = float(val or DEFAULT_AUDIO_THRESHOLD)
    return val


# ── Microphone toggle ────────────────────────────────────────────────────────

@app.callback(
    Output("mic-btn", "children"),
    Output("mic-btn", "style"),
    Output("mic-status", "children"),
    Output("mic-status", "style"),
    Output("mic-active-store", "data"),
    Input("mic-btn", "n_clicks"),
    State("mic-device-dropdown", "value"),
    State("mic-btn", "children"),
    prevent_initial_call=True,
)
def toggle_mic(n_clicks, device_index, current_label):
    global _mic_stream, _mic_device_index

    btn_on = {**BTN_BASE, "backgroundColor": COLOR_DANGER_DARK, "color": "#fff", "padding": "6px 14px"}
    btn_off = {**BTN_BASE, "backgroundColor": USMA_GOLD, "color": "#000", "padding": "6px 14px"}
    status_on = {"color": COLOR_HIT, "fontSize": "0.85rem", "fontWeight": 600}
    status_off = {"color": TEXT_MUTED, "fontSize": "0.85rem", "fontWeight": 600}

    if "Start" in (current_label or ""):
        if _mic_stream is not None:
            try:
                _mic_stream.stop()
                _mic_stream.close()
            except Exception:
                pass
        state.mic_db_buffer.clear()
        device_idx = int(device_index) if device_index is not None else None
        try:
            _mic_stream = start_mic_stream(device=device_idx)
            _mic_device_index = device_idx
        except Exception as e:
            return "Start Mic", btn_off, f"Error: {e}", {"color": COLOR_ERROR_TEXT, "fontSize": "0.85rem", "fontWeight": 600}, False
        dev_name = sd.query_devices(_mic_stream.device[0])["name"] if _mic_stream else "default"
        return "Stop Mic", btn_on, f"Live: {dev_name[:28]}", status_on, True
    else:
        if _mic_stream is not None:
            try:
                _mic_stream.stop()
                _mic_stream.close()
            except Exception:
                pass
            _mic_stream = None
        return "Start Mic", btn_off, "Stopped", status_off, False


# ── Device scan ──────────────────────────────────────────────────────────────

@app.callback(
    Output("scan-results-div", "children"),
    Output("scan-store", "data"),
    Output("connect-btn-wrap", "style"),
    Input("scan-btn", "n_clicks"),
    State("slots-store", "data"),
    prevent_initial_call=True,
)
def handle_scan(n_clicks, slots_data):
    global _scan_in_progress
    if _scan_in_progress:
        return (
            html.P("Scan already running -- please wait...", style={"color": USMA_GOLD, "fontSize": "0.8rem"}),
            no_update,
            {"display": "none"},
        )
    _scan_in_progress = True
    try:
        future = asyncio.run_coroutine_threadsafe(find_h10_devices(timeout=8.0), _ble_loop)
        devices = future.result(timeout=12.0)
    except Exception as e:
        err = str(e)
        if "InProgress" in err or "in progress" in err.lower():
            msg = "BLE adapter busy -- wait a moment and try again."
        else:
            msg = f"Scan error: {err}"
        return (
            html.P(msg, style={"color": COLOR_ERROR_TEXT, "fontSize": "0.8rem"}),
            [],
            {"display": "none"},
        )
    finally:
        _scan_in_progress = False

    if not devices:
        return (
            html.P(
                "No Polar devices found. Is the strap charged and worn?",
                style={"color": TEXT_SECONDARY, "fontSize": "0.8rem"},
            ),
            [],
            {"display": "none"},
        )

    already = set((slots_data or {}).values())
    scan_data = [{"address": d.address, "name": d.name or "Polar H10"} for d in devices]

    rows = []
    for d in scan_data:
        connected = d["address"] in already
        rows.append(
            html.Div(
                style={
                    "display": "flex",
                    "justifyContent": "space-between",
                    "alignItems": "center",
                    "padding": "3px 0",
                    "borderBottom": f"1px solid {BORDER_SUBTLE}",
                },
                children=[
                    dcc.Checklist(
                        id={"type": "device-check", "index": d["address"]},
                        options=[
                            {
                                "label": f"  {d['name']}  {d['address'][-8:]}{'  (connected)' if connected else ''}",
                                "value": d["address"],
                                "disabled": connected,
                            }
                        ],
                        value=[d["address"]] if not connected else [],
                        style={"color": USMA_GOLD if connected else USMA_GOLD_LIGHT, "fontSize": "0.82rem"},
                        inputStyle={"marginRight": "6px"},
                    ),
                ],
            )
        )

    btn_style = {
        **BTN_BASE,
        "backgroundColor": USMA_GOLD,
        "color": "#000",
        "padding": "7px 18px",
        "marginTop": "8px",
        "display": "inline-block",
    }
    return html.Div(rows), scan_data, btn_style


# ── Connect / disconnect ─────────────────────────────────────────────────────

@app.callback(
    Output("slots-store", "data"),
    Input("connect-btn", "n_clicks"),
    *[Input(f"disconnect-btn-{i}", "n_clicks") for i in range(MAX_SLOTS)],
    State("scan-store", "data"),
    State({"type": "device-check", "index": ALL}, "value"),
    State("slots-store", "data"),
    prevent_initial_call=True,
)
def handle_connections(connect_clicks, *rest):
    from dash import callback_context as ctx

    disconnect_clicks = rest[:MAX_SLOTS]
    scan_data, all_check_values, slots_data = rest[MAX_SLOTS], rest[MAX_SLOTS + 1], rest[MAX_SLOTS + 2]

    slots_data = dict(slots_data or {})
    trigger = ctx.triggered_id

    if trigger == "connect-btn":
        selected: list[str] = []
        for v in (all_check_values or []):
            if v:
                selected.extend(v)
        already = set(slots_data.values())
        slot_idx = 0
        for addr in selected:
            if addr in already:
                continue
            while str(slot_idx) in slots_data and slot_idx < MAX_SLOTS:
                slot_idx += 1
            if slot_idx >= MAX_SLOTS:
                break
            slots_data[str(slot_idx)] = addr
            asyncio.run_coroutine_threadsafe(_start_collecting(addr), _ble_loop)
            slot_idx += 1

    elif isinstance(trigger, str) and trigger.startswith("disconnect-btn-"):
        slot_idx = int(trigger.rsplit("-", 1)[-1])
        addr = slots_data.pop(str(slot_idx), None)
        if addr:
            _stop_streaming_threadsafe(addr)
            class _Dev:
                address = addr
            stop_recording_session(_Dev())

    return slots_data


# ── Slot visibility and device names ─────────────────────────────────────────

@app.callback(
    *[Output(f"slot-card-{i}", "style") for i in range(MAX_SLOTS)],
    *[Output(f"device-name-{i}", "children") for i in range(MAX_SLOTS)],
    Output("no-sensors-msg", "style"),
    Input("slots-store", "data"),
)
def update_slot_visibility(slots_data):
    slots_data = slots_data or {}
    card_styles, names = [], []
    for i in range(MAX_SLOTS):
        addr = slots_data.get(str(i))
        if addr:
            card_styles.append({
                "borderRadius": "8px",
                "border": f"1px solid {BORDER_MID}",
                "overflow": "hidden",
            })
            names.append(addr)
        else:
            card_styles.append({"display": "none"})
            names.append("")

    no_msg = {"display": "none"} if slots_data else {}
    return *card_styles, *names, no_msg


# ── Per-slot: graph updates + HR badge + summary ─────────────────────────────

for _slot in range(MAX_SLOTS):

    @app.callback(
        Output(f"hr-graph-{_slot}", "figure"),
        Output(f"db-graph-{_slot}", "figure"),
        Output(f"hr-badge-{_slot}", "children"),
        Output(f"summary-{_slot}", "children"),
        Input("main-interval", "n_intervals"),
        State("slots-store", "data"),
        State("window-dropdown", "value"),
        State("db-threshold-slider", "value"),
        State(f"shot-store-{_slot}", "data"),
    )
    def _update_slot(n, slots_data, window_sec, db_thresh, shots, _i=_slot):
        addr = (slots_data or {}).get(str(_i))
        if not addr:
            return no_update, no_update, no_update, no_update

        # Performance: skip re-render if no new data
        streamer = state.streamers.get(addr)
        rr_count = len(streamer.rr_intervals) if streamer else 0
        mic_count = len(state.mic_db_buffer)
        if rr_count == _last_rr_count.get(_i, -1) and mic_count == _last_mic_count.get(_i, -1):
            return no_update, no_update, no_update, no_update
        _last_rr_count[_i] = rr_count
        _last_mic_count[_i] = mic_count

        color = DEVICE_COLORS[_i % len(DEVICE_COLORS)]
        window_sec = float(window_sec or 60)
        db_thresh = float(db_thresh or DEFAULT_AUDIO_THRESHOLD)
        shots = shots or []

        # Build complete spike list: ALL dB peaks above threshold on the HR axis,
        # merged with existing labels from the shot store.
        times_arr, _ = streamer.get_instantaneous_hr() if streamer else (np.array([]), np.array([]))
        db_times, db_values = get_mic_db_window(99999.0)
        all_spike_times = get_spike_times_on_hr_axis(
            list(times_arr), db_times, db_values, db_thresh
        ) if len(times_arr) > 0 and db_times else []

        # Merge: use labels from store where available, None for unlabeled
        label_lookup = {round(s["time"], 3): s.get("label") for s in shots}
        all_shots = []
        seen = set()
        for st in all_spike_times:
            key = round(st, 3)
            all_shots.append({"time": st, "label": label_lookup.get(key)})
            seen.add(key)
        # Keep any labeled shots from the store that weren't in current spikes
        for s in shots:
            key = round(s["time"], 3)
            if key not in seen and s.get("label") is not None:
                all_shots.append(s)

        hr_fig = _render_hr_fig(addr, window_sec, all_shots, db_thresh, color)
        db_fig = _render_db_fig(window_sec, db_thresh, color)

        # Live HR badge
        badge = ""
        if streamer and streamer.rr_intervals:
            latest_rr = streamer.rr_intervals[-1][1]
            if latest_rr > 0:
                badge = f"{60000.0 / latest_rr:.0f} BPM"

        metrics_div = _build_metrics_grid(streamer, window_sec, shots)
        return hr_fig, db_fig, badge, metrics_div


# ── Multi-sensor comparison panel ────────────────────────────────────────────

@app.callback(
    Output("comparison-panel", "children"),
    Output("comparison-panel", "style"),
    Input("main-interval", "n_intervals"),
    State("slots-store", "data"),
    State("window-dropdown", "value"),
)
def update_comparison_panel(n, slots_data, window_sec):
    slots_data = slots_data or {}
    active = [(int(k), v) for k, v in slots_data.items() if v]
    if len(active) < 2:
        return no_update, {"display": "none"}

    window_sec = float(window_sec or 60)

    sensor_cards = []
    for slot_i, addr in sorted(active):
        color = DEVICE_COLORS[slot_i % len(DEVICE_COLORS)]
        streamer = state.streamers.get(addr)
        if not streamer or not streamer.rr_intervals:
            sensor_cards.append(html.Div(
                style={"flex": 1, "padding": "12px 16px", "borderRight": f"1px solid {BORDER_MID}"},
                children=[
                    html.Span(addr[-11:], style={"color": color, "fontFamily": "monospace", "fontSize": "0.8rem"}),
                    html.P("Waiting for data...", style={"color": BORDER_MID, "fontSize": "0.8rem", "margin": "4px 0 0"}),
                ],
            ))
            continue

        rr_vals = [rr for _, rr in streamer.rr_intervals]
        times_arr, hr_arr = streamer.get_instantaneous_hr()
        current_hr = 60000.0 / rr_vals[-1] if rr_vals[-1] > 0 else None
        avg_hr = min_hr = max_hr = sdnn = None
        session_str = "---"
        if len(times_arr) > 0:
            max_t = float(times_arr[-1])
            mask = times_arr >= max_t - window_sec
            hr_win = hr_arr[mask]
            m, s = divmod(int(max_t), 60)
            session_str = f"{m}:{s:02d}"
            if len(hr_win) > 0:
                avg_hr = float(hr_win.mean())
                min_hr = float(hr_win.min())
                max_hr = float(hr_win.max())
        if len(rr_vals) > 4:
            sdnn = float(np.std(rr_vals[-min(len(rr_vals), 300):]))

        def _stat(label, val, unit=""):
            v = f"{val:.0f}{unit}" if val is not None else "---"
            return html.Div(
                style={"display": "flex", "justifyContent": "space-between", "margin": "2px 0"},
                children=[
                    html.Span(label, style={"color": TEXT_MUTED, "fontSize": "0.72rem"}),
                    html.Span(v, style={"color": USMA_GOLD_LIGHT, "fontWeight": 600, "fontSize": "0.78rem"}),
                ],
            )

        sensor_cards.append(html.Div(
            style={"flex": 1, "padding": "10px 18px", "borderRight": f"1px solid {BORDER_MID}"},
            children=[
                html.Div(
                    style={"display": "flex", "alignItems": "baseline", "gap": "10px", "marginBottom": "6px"},
                    children=[
                        html.Span(addr[-11:], style={"color": color, "fontFamily": "monospace", "fontSize": "0.78rem", "fontWeight": 700}),
                        html.Span(
                            f"{current_hr:.0f} bpm" if current_hr else "---",
                            style={"color": USMA_GOLD, "fontWeight": 800, "fontSize": "1.1rem", "marginLeft": "auto"},
                        ),
                    ],
                ),
                _stat("Avg HR", avg_hr, " bpm"),
                _stat("Max HR", max_hr, " bpm"),
                _stat("Min HR", min_hr, " bpm"),
                _stat("HRV (SDNN)", sdnn, " ms"),
                _stat("Session", None) if session_str == "---" else html.Div(
                    style={"display": "flex", "justifyContent": "space-between", "margin": "2px 0"},
                    children=[
                        html.Span("Session", style={"color": TEXT_MUTED, "fontSize": "0.72rem"}),
                        html.Span(session_str, style={"color": USMA_GOLD_LIGHT, "fontWeight": 600, "fontSize": "0.78rem"}),
                    ],
                ),
            ],
        ))

    panel = html.Div(
        style={
            "margin": "0 24px",
            "marginTop": "12px",
            "backgroundColor": USMA_BG_CONTROLS,
            "border": f"1px solid {USMA_GOLD}",
            "borderRadius": "8px",
            "overflow": "hidden",
        },
        children=[
            html.Div(
                "SENSOR COMPARISON",
                style={
                    "backgroundColor": "#1a1500",
                    "color": USMA_GOLD,
                    "fontSize": "0.65rem",
                    "letterSpacing": "0.18em",
                    "fontWeight": 700,
                    "padding": "6px 18px",
                    "borderBottom": f"1px solid {USMA_GOLD}",
                },
            ),
            html.Div(sensor_cards, style={"display": "flex"}),
        ],
    )
    return panel, {"display": "block"}


# ── Per-slot: recording toggle ───────────────────────────────────────────────

for _slot in range(MAX_SLOTS):

    @app.callback(
        Output(f"record-btn-{_slot}", "children"),
        Output(f"record-btn-{_slot}", "style"),
        Output(f"recording-indicator-{_slot}", "style"),
        Output("recording-active-store", "data", allow_duplicate=True),
        Input(f"record-btn-{_slot}", "n_clicks"),
        State("slots-store", "data"),
        State(f"record-btn-{_slot}", "children"),
        prevent_initial_call=True,
    )
    def _toggle_recording(n_clicks, slots_data, current_label, _i=_slot):
        if not n_clicks:
            raise PreventUpdate
        addr = (slots_data or {}).get(str(_i))
        if not addr:
            raise PreventUpdate

        class _Dev:
            address = addr

        rec_on_style = {**BTN_BASE, "backgroundColor": COLOR_DANGER_DARK, "color": "#fff", "padding": "6px 14px", "marginRight": "8px"}
        rec_off_style = {**BTN_BASE, "backgroundColor": USMA_GOLD, "color": "#000", "padding": "6px 14px", "marginRight": "8px"}
        indicator_on = {"display": "inline-block", "marginRight": "8px"}
        indicator_off = {"display": "none"}

        if "Start" in (current_label or ""):
            start_recording_session(_Dev(), log_dir=LOG_DIR)
            return "Stop Recording", rec_on_style, indicator_on, True
        else:
            stop_recording_session(_Dev())
            return "Start Recording", rec_off_style, indicator_off, False


# ── Per-slot: hit / miss labeling (FIXED: persistent labels) ────────────────

for _slot in range(MAX_SLOTS):

    @app.callback(
        Output(f"shot-store-{_slot}", "data"),
        Input(f"hit-btn-{_slot}", "n_clicks"),
        Input(f"miss-btn-{_slot}", "n_clicks"),
        State(f"shot-store-{_slot}", "data"),
        State("slots-store", "data"),
        State("db-threshold-slider", "value"),
        prevent_initial_call=True,
    )
    def _handle_label(hit_clicks, miss_clicks, shot_store, slots_data, db_thresh, _i=_slot):
        """Label the most recent audio spike as hit or miss.

        Uses the FULL audio buffer (not window-limited) so labels persist
        across time window changes.
        """
        from dash import callback_context as ctx

        trigger = ctx.triggered_id
        if not trigger:
            raise PreventUpdate

        label = "hit" if trigger == f"hit-btn-{_i}" else "miss"
        addr = (slots_data or {}).get(str(_i))
        if not addr:
            raise PreventUpdate

        db_thresh = float(db_thresh or DEFAULT_AUDIO_THRESHOLD)
        shot_store = list(shot_store or [])

        streamer = state.streamers.get(addr)
        if not streamer:
            raise PreventUpdate

        # Use ALL available audio data — not limited by the display window
        times_arr, _ = streamer.get_instantaneous_hr()
        db_times, db_values = get_mic_db_window(99999.0)
        all_spike_times = get_spike_times_on_hr_axis(
            list(times_arr), db_times, db_values, db_thresh
        )

        # Merge new spikes into existing store (never prune old entries)
        existing_times = {round(s["time"], 3) for s in shot_store}
        for st in all_spike_times:
            if round(st, 3) not in existing_times:
                shot_store.append({"time": st, "label": None})

        # Label the most recent unlabeled spike
        unlabeled = [i for i, s in enumerate(shot_store) if s["label"] is None]
        if not shot_store:
            raise PreventUpdate
        target_idx = unlabeled[-1] if unlabeled else len(shot_store) - 1

        shot_time = shot_store[target_idx]["time"]
        shot_store[target_idx]["label"] = label

        # Log to CSV with HR and audio context
        inst_hr_at_shot, audio_peak_at_shot = _get_hr_at_time(streamer, shot_time)

        # Persist HR and audio in the store so analytics can read them later
        # (the mic_db_buffer is a rolling window and old samples get evicted)
        shot_store[target_idx]["hr"] = inst_hr_at_shot
        shot_store[target_idx]["db"] = audio_peak_at_shot

        shot_logger = state.shot_loggers.get(addr)
        if shot_logger is not None:
            shot_logger.log_label(
                shot_time,
                label,
                inst_hr=inst_hr_at_shot,
                audio_db_peak=audio_peak_at_shot,
                db_threshold=db_thresh,
            )

        return shot_store


# ── Analytics tab callbacks ──────────────────────────────────────────────────

@app.callback(
    Output("analytics-device-select", "options"),
    Output("analytics-device-select", "value"),
    Input("slots-store", "data"),
    State("analytics-device-select", "value"),
)
def update_analytics_device_options(slots_data, current_val):
    """Keep the analytics device dropdown in sync with connected sensors."""
    slots_data = slots_data or {}
    options = [{"label": f"Sensor {k} ({v[-8:]})", "value": v} for k, v in slots_data.items() if v]
    values = [o["value"] for o in options]
    val = current_val if current_val in values else (values[0] if values else None)
    return options, val


@app.callback(
    Output("stat-total-shots", "children"),
    Output("stat-hit-rate", "children"),
    Output("stat-avg-hr", "children"),
    Output("stat-session-dur", "children"),
    Output("analytics-ratio-graph", "figure"),
    Output("analytics-hr-graph", "figure"),
    Output("analytics-table", "data"),
    Input("main-interval", "n_intervals"),
    *[State(f"shot-store-{i}", "data") for i in range(MAX_SLOTS)],
    State("slots-store", "data"),
    State("analytics-device-select", "value"),
)
def update_analytics(n, *args):
    """Update all analytics tab content."""
    shot_stores = args[:MAX_SLOTS]
    slots_data = args[MAX_SLOTS]
    selected_addr = args[MAX_SLOTS + 1]

    if not selected_addr:
        empty = _analytics_stat_card("TOTAL SHOTS", "---")
        return (
            empty,
            _analytics_stat_card("HIT RATE", "---"),
            _analytics_stat_card("AVG HR AT SHOT", "---"),
            _analytics_stat_card("SESSION", "---"),
            _empty_fig("Cumulative Hit Rate — no data"),
            _empty_fig("HR at Shot Time — no data"),
            [],
        )

    # Find which slot has the selected device
    slots_data = slots_data or {}
    slot_idx = None
    for k, v in slots_data.items():
        if v == selected_addr:
            slot_idx = int(k)
            break

    shots = list(shot_stores[slot_idx] or []) if slot_idx is not None else []
    streamer = state.streamers.get(selected_addr)

    labeled = [s for s in shots if s.get("label")]
    hits = [s for s in labeled if s["label"] == "hit"]
    misses = [s for s in labeled if s["label"] == "miss"]
    total = len(labeled)

    # Summary cards
    total_card = _analytics_stat_card("TOTAL SHOTS", str(total))
    hit_rate = f"{100 * len(hits) / total:.0f}%" if total else "---"
    rate_card = _analytics_stat_card("HIT RATE", hit_rate, COLOR_HIT_BRIGHT if len(hits) > len(misses) else COLOR_MISS_BRIGHT if total else USMA_GOLD_LIGHT)

    # Average HR at shot time
    hr_values = []
    for s in labeled:
        hr = s.get("hr") or _get_hr_at_time(streamer, s["time"])[0]
        if hr is not None:
            hr_values.append(hr)
    avg_hr_str = f"{np.mean(hr_values):.0f} bpm" if hr_values else "---"
    hr_card = _analytics_stat_card("AVG HR AT SHOT", avg_hr_str)

    # Session duration
    session_str = "---"
    if streamer and streamer.rr_intervals:
        times_arr, _ = streamer.get_instantaneous_hr()
        if len(times_arr) > 0:
            m, s = divmod(int(float(times_arr[-1])), 60)
            session_str = f"{m}:{s:02d}"
    dur_card = _analytics_stat_card("SESSION", session_str)

    # Cumulative hit rate chart
    ratio_fig = go.Figure()
    if total > 0:
        sorted_labeled = sorted(labeled, key=lambda s: s["time"])
        cum_hits = []
        cum_rate = []
        times_list = []
        h = 0
        for idx_s, s in enumerate(sorted_labeled, 1):
            if s["label"] == "hit":
                h += 1
            cum_hits.append(h)
            cum_rate.append(100 * h / idx_s)
            times_list.append(s["time"])

        colors = [COLOR_HIT if s["label"] == "hit" else COLOR_MISS_BRIGHT for s in sorted_labeled]
        ratio_fig.add_trace(go.Scatter(
            x=times_list, y=cum_rate,
            mode="lines+markers",
            line=dict(color=USMA_GOLD, width=2),
            marker=dict(color=colors, size=8, line=dict(width=1, color="#000")),
            hovertemplate="t=%{x:.1f}s  %{y:.0f}%<extra></extra>",
        ))
        ratio_fig.add_hline(y=50, line_dash="dot", line_color=TEXT_MUTED, line_width=1)

    ratio_fig.update_layout(
        title="Cumulative Hit Rate (%)",
        xaxis_title="Elapsed (s)",
        yaxis_title="Hit Rate (%)",
        yaxis=dict(range=[0, 105]),
        showlegend=False,
        **PLOTLY_DARK_LAYOUT,
    )

    # HR at shot time: grouped bar
    hr_fig = go.Figure()
    hit_hrs = []
    miss_hrs = []
    for s in labeled:
        hr = s.get("hr") or _get_hr_at_time(streamer, s["time"])[0]
        if hr is not None:
            if s["label"] == "hit":
                hit_hrs.append(hr)
            else:
                miss_hrs.append(hr)

    categories = []
    means = []
    bar_colors = []
    if hit_hrs:
        categories.append("HITS")
        means.append(float(np.mean(hit_hrs)))
        bar_colors.append(COLOR_HIT)
    if miss_hrs:
        categories.append("MISSES")
        means.append(float(np.mean(miss_hrs)))
        bar_colors.append(COLOR_MISS_BRIGHT)

    if categories:
        hr_fig.add_trace(go.Bar(
            x=categories, y=means,
            marker_color=bar_colors,
            text=[f"{m:.0f}" for m in means],
            textposition="outside",
            textfont=dict(color=USMA_GOLD_LIGHT),
        ))

    hr_fig.update_layout(
        title="Avg HR at Shot Time (bpm)",
        yaxis_title="BPM",
        showlegend=False,
        **PLOTLY_DARK_LAYOUT,
    )

    # Shot timeline table
    table_data = []
    for idx_s, s in enumerate(sorted(shots, key=lambda x: x["time"]), 1):
        if not s.get("label"):
            continue
        hr = s.get("hr") or _get_hr_at_time(streamer, s["time"])[0]
        db = s.get("db") or _get_hr_at_time(streamer, s["time"])[1]
        table_data.append({
            "shot_num": idx_s,
            "time": f"{s['time']:.1f}",
            "label": s["label"],
            "hr": f"{hr:.0f}" if hr else "---",
            "db": f"{db:.1f}" if db else "---",
        })

    return total_card, rate_card, hr_card, dur_card, ratio_fig, hr_fig, table_data


def _analytics_stat_card(label: str, value: str, value_color: str = USMA_GOLD) -> list:
    """Return children for an analytics summary card."""
    return [
        html.Div(label, style={
            "color": TEXT_MUTED,
            "fontSize": "0.62rem",
            "letterSpacing": "0.12em",
            "marginBottom": "6px",
            "fontWeight": 600,
        }),
        html.Div(value, style={
            "color": value_color,
            "fontSize": "1.4rem",
            "fontWeight": 800,
            "lineHeight": "1",
        }),
    ]


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    app.run(debug=False, host="0.0.0.0", port=8050)
