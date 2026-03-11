# Audio capture
SAMPLE_RATE = 16000        # Hz
FRAME_DURATION = 0.02      # seconds per audio callback block
MIC_BUFFER_SECONDS = 30    # seconds of rolling audio kept in memory
DEFAULT_AUDIO_THRESHOLD = 70.0  # dB — default shot-detection threshold

# BLE
HR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
HR_BUFFER_MINUTES = 10     # minutes of HR data kept in memory per device

# ── USMA color palette ──────────────────────────────────────────────────────
USMA_BLACK = "#000000"
USMA_GOLD = "#CFB53B"
USMA_GOLD_LIGHT = "#f2e4b3"
USMA_GRAY = "#444444"
USMA_GRAY_LIGHT = "#aaaaaa"

# Layout backgrounds
USMA_BG_PAGE = "#0d0d0d"
USMA_BG_HEADER = "#000000"
USMA_BG_CONTROLS = "#111111"
USMA_CARD_BG = "#0a0a0a"
USMA_BG_PLOT = "#050505"

# Borders and separators
BORDER_SUBTLE = "#1a1a1a"
BORDER_MID = "#2a2a2a"
BORDER_STRONG = "#444"

# Text
TEXT_MUTED = "#555"
TEXT_DIM = "#666"
TEXT_SECONDARY = "#888888"

# Semantic accent colors
COLOR_HIT = "#2ca02c"
COLOR_HIT_BG = "#1a4d1a"
COLOR_HIT_TEXT = "#6ee86e"
COLOR_HIT_BRIGHT = "#4dbb4d"
COLOR_MISS = "#cc2222"
COLOR_MISS_BG = "#4d1a1a"
COLOR_MISS_TEXT = "#e87070"
COLOR_MISS_BRIGHT = "#dd4444"
COLOR_DANGER = "#ff4136"
COLOR_DANGER_DARK = "#8B0000"
COLOR_WARNING = "#dd8800"
COLOR_ERROR_TEXT = "#f66"
COLOR_INFO = "#4da6ff"

# Shot label color map (used in HR plot markers)
SHOT_LABEL_COLORS = {"hit": COLOR_HIT, "miss": COLOR_MISS, None: USMA_GOLD}

# Per-device trace colors (up to 4 simultaneous sensors)
DEVICE_COLORS = ["#CFB53B", "#4da6ff", "#ff7f0e", "#2ca02c"]

# ── Reusable style dicts ────────────────────────────────────────────────────
BTN_BASE = {
    "border": "none",
    "borderRadius": "4px",
    "fontWeight": 700,
    "cursor": "pointer",
    "fontSize": "0.85rem",
}

SECTION_LABEL_STYLE = {
    "color": USMA_GOLD,
    "fontSize": "0.68rem",
    "letterSpacing": "0.14em",
    "marginBottom": "6px",
    "fontWeight": 600,
}

PLOTLY_DARK_LAYOUT = dict(
    template="plotly_dark",
    paper_bgcolor=USMA_CARD_BG,
    plot_bgcolor=USMA_BG_PLOT,
    font=dict(color=USMA_GOLD_LIGHT, size=11),
    margin=dict(l=48, r=16, t=32, b=36),
)
