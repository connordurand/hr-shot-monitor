# Capstone Diagrams

Use these with any Mermaid renderer (mermaid.live, VS Code plugin, or export to PNG/SVG for the paper).

---

## Diagram 1 — System Architecture (High-Level Data Flow)

```mermaid
flowchart LR
    subgraph Hardware
        H10[Polar H10<br/>Chest Strap]
        MIC[Laptop<br/>Microphone]
    end

    subgraph Acquisition["Data Acquisition Layer"]
        BLE[BLE Stream<br/><i>RR intervals + HR</i>]
        AUD[Audio Stream<br/><i>PCM → dB per frame</i>]
    end

    subgraph Alignment["Signal Alignment Engine"]
        TA[Shared Elapsed-Time Axis<br/><i>t = now − start_time</i>]
        RR[Beat-Time<br/>Reconstruction<br/><i>backward walk<br/>from RR intervals</i>]
        PK[Per-Beat<br/>Audio Peak<br/><i>max dB in<br/>beat interval</i>]
        TH[Threshold<br/>Detection<br/><i>dB ≥ threshold?</i>]
    end

    subgraph Output["Outputs"]
        DASH[Real-Time<br/>Dashboard]
        CSV[CSV Logs<br/><i>session / events / labels</i>]
        ANA[Offline<br/>Analysis]
    end

    H10 -->|Bluetooth LE| BLE
    MIC -->|sounddevice| AUD
    BLE --> TA
    AUD --> TA
    TA --> RR
    TA --> TH
    RR --> PK
    PK --> DASH
    PK --> CSV
    TH --> CSV
    TH --> DASH
    CSV --> ANA
    DASH -->|Hit/Miss Labels| CSV
```

---

## Diagram 2 — Beat-Time Reconstruction from RR Intervals

```mermaid
flowchart TD
    N["BLE Notification arrives<br/>at elapsed T = 10.0 s"]
    P["Parse RR intervals<br/>RR₁ = 850 ms, RR₂ = 900 ms"]
    W1["Walk backward from T:<br/>t = 10.0 − 0.900 = <b>9.100 s</b> → Beat B₂"]
    W2["Continue walking:<br/>t = 9.100 − 0.850 = <b>8.250 s</b> → Beat B₁"]
    R["Reverse → chronological order:<br/>B₁ @ 8.250 s, B₂ @ 9.100 s"]
    HR["Compute instantaneous HR:<br/>B₁: 60000/850 = 70.6 BPM<br/>B₂: 60000/900 = 66.7 BPM"]

    N --> P --> W1 --> W2 --> R --> HR
```

---

## Diagram 3 — Audio-to-Heartbeat Mapping Pipeline

```mermaid
flowchart TD
    subgraph Audio Path
        MIC["Microphone Stream<br/><i>20 ms frames → dB</i>"]
        BUF["Rolling Buffer<br/><i>mic_db_buffer<br/>30 s / 1500 samples</i>"]
        THR{"dB ≥ threshold?"}
        EVT["Audio Event Log<br/><i>audio_events.csv</i>"]
    end

    subgraph HR Path
        BEAT["Reconstructed Beat<br/><i>(beat_t, rr_ms)</i>"]
        INT["Beat Interval<br/><i>[beat_t − rr_ms/1000, beat_t]</i>"]
        SCAN["Scan buffer for<br/>samples in interval"]
        PEAK["audio_db_peak =<br/>max(dB in interval)"]
    end

    subgraph Label Path
        USR["Operator presses<br/>HIT or MISS"]
        MAP["Map shot time →<br/>nearest beat"]
        LBL["Shot Label Log<br/><i>shot_labels.csv<br/>+ HR + dB at beat</i>"]
    end

    MIC --> BUF
    BUF --> THR
    THR -->|Yes| EVT
    BUF --> SCAN
    BEAT --> INT --> SCAN --> PEAK
    EVT --> MAP
    USR --> MAP --> LBL
```

---

## Diagram 4 — Dashboard Session Workflow

```mermaid
flowchart LR
    S1["① Scan<br/><i>BLE discovery</i>"]
    S2["② Connect<br/><i>Select device(s)</i>"]
    S3["③ Start Mic<br/><i>Audio capture</i>"]
    S4["④ Record<br/><i>Logging active</i>"]

    S1 --> S2 --> S3 --> S4

    S4 --> LIVE["Live Monitor Tab<br/><i>HR trace + dB trace<br/>+ shot markers</i>"]
    S4 --> ANA["Analytics Tab<br/><i>Hit rate, HR comparison,<br/>shot timeline</i>"]
    S4 --> CSV["CSV Export<br/><i>3 files per session</i>"]
```

---

## Diagram 5 — CSV Data Schema Relationships

```mermaid
erDiagram
    SESSION_CSV {
        string timestamp_iso
        float timestamp_epoch
        float elapsed_sec PK
        float rr_ms
        float inst_hr_bpm
        float audio_db_peak
        int audio_over_threshold
    }

    AUDIO_EVENTS_CSV {
        string timestamp_iso
        float timestamp_epoch
        float elapsed_sec PK
        float audio_db
    }

    SHOT_LABELS_CSV {
        string timestamp_iso
        float timestamp_epoch
        float shot_time_elapsed PK
        string label
        float inst_hr_bpm
        float audio_db_peak
        float db_threshold
    }

    SESSION_CSV ||--o{ SHOT_LABELS_CSV : "nearest elapsed_sec"
    AUDIO_EVENTS_CSV ||--o{ SHOT_LABELS_CSV : "nearest elapsed_sec"
    SESSION_CSV ||--o{ AUDIO_EVENTS_CSV : "same time axis"
```

---

## Diagram 6 — Threading & Concurrency Model

```mermaid
flowchart TD
    subgraph Main["Main Thread"]
        DASH["Dash Web Server<br/><i>Flask / callbacks</i>"]
    end

    subgraph BLE_Thread["Daemon Thread"]
        LOOP["asyncio Event Loop"]
        SCAN["BLE Scan"]
        CONN["BLE Connect +<br/>Auto-Reconnect"]
        CB["hr_callback<br/><i>RR parsing<br/>+ beat logging</i>"]
    end

    subgraph Audio_Thread["sounddevice Thread"]
        AUD["Audio Callback<br/><i>PCM → dB<br/>+ threshold check</i>"]
    end

    subgraph Shared["Shared State (src/state.py)"]
        BUF["mic_db_buffer"]
        STR["streamers{}"]
        LOG["loggers{}"]
        THR["threshold"]
    end

    DASH -->|"run_coroutine_threadsafe()"| LOOP
    LOOP --> SCAN
    LOOP --> CONN --> CB
    CB -->|read| BUF
    CB -->|write| STR
    CB -->|write| LOG
    AUD -->|write| BUF
    AUD -->|read| THR
    DASH -->|read| STR
    DASH -->|write| THR
```
