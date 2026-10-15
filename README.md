# DeskSense

An on-device physical AI desk wellness companion for the **Arduino UNO Q**.

A webcam watches your desk. Everything is inferred **on the board** — no frame ever leaves the
device. The results drive real physical output: relays and a 16×2 LCD, switched by the
microcontroller, plus a live dashboard on your local network.

Built for the Arduino Physical AI Challenge India 2026.

---

## What it does

| Signal | How it's derived |
|---|---|
| **Presence / engagement** | YoloX-nano person detection, smoothed over a rolling window |
| **Phone distraction** | YoloX-nano `cell phone` class (COCO), asymmetric debounce |
| **Posture** | Bounding-box geometry proxy, calibrated per user |
| **Gaze** | OpenCV Haar cascade face detection |

Scores are written to SQLite, rolled up periodically, shown on a Flask dashboard, and summarised
at end of day by an on-device Qwen 3.5 0.8B language model.

## Physical output

| Output | Pin | Behaviour |
|---|---|---|
| Relay ch0 | D4 | ollows presence — switches a 5 V LED on when you sit down |
| Relay ch1 | D5 | Follows presence — no load attached|
| Relay ch2 | D6 | 	Fires when posture stays below 88 for 10 seconds — no load attached |
| Onboard LED | — | Mirrors relay channel 0 |
| 16×2 LCD | I2C | Line 1 `Posture:` · Line 2 `Focus:`, scores right-aligned to column 16 |

The LCD shows `--` in place of a score while calibrating or when a signal is unavailable.



---

## Hardware

| Component | Notes |
|---|---|
| Arduino UNO Q | QRB2210 quad-core Cortex-A53 (Debian Linux) + STM32U585 Cortex-M33 |
| USB webcam | Addressed by stable `/dev/v4l/by-id/...` path, never by index |
|8-channel relay module, 3 channels used | SRD-05VDC-SL-C, opto-isolated, **active-low** |
| 16×2 I2C LCD | PCF8574 backpack, `hd44780` library |

### Relay module wiring

The JD-VCC jumper is **removed** so the opto-isolation stays intact:

- `Vcc` → 3.3 V from the UNO Q
- `JD-VCC` → 5 V from a USB-C breakout — a separate supply for the relay coils
- `GND` → common ground
- `IN0 / IN1 / IN2` → D4 / D5 / D6

Relays are active-low: the sketch drives a pin LOW to energise that channel. If your board is
the opposite, flip `RELAY_ACTIVE_LOW` at the top of `sketch/sketch.ino`.

### LCD wiring

`VCC` → 3.3 V · `GND` → GND · `SDA` → SDA · `SCL` → SCL.

Set the contrast trimmer while watching through a phone camera, not with your eye. The green
backlight fools camera auto-exposure and crushes the characters to unreadable — get this right
before the enclosure closes up, because the pot is hard to reach afterwards.

---

## Architecture

```
┌─────────────────────── QRB2210 · Linux ─────────────────────────┐
│  camera.py   → capture, dual cadence (inference + view stream)   │
│  vision      → YoloX-nano detection · Haar cascade gaze          │
│  scoring     → engagement · posture · phone · smoothing          │
│  storage     → SQLite, periodic rollups, flush on shutdown       │
│  llm         → Qwen 3.5 0.8B daily summary                       │
│  main.py     → Flask app + dashboard on :7000                    │
└──────────────────────────┬───────────────────────────────────────┘
                           │  Bridge RPC · Arduino_RouterBridge.h
┌──────────────────────────┴───────────────────────────────────────┐
│                     STM32U585 · sketch.ino                        │
│  Bridge.provide("set_relay", …)   → D4 / D5 / D6, active-low      │
│  Bridge.provide("lcd_show", …)    → 16×2 hd44780, guarded by      │
│                                     lcdPresent so a missing       │
│                                     display never blocks the app  │
└───────────────────────────────────────────────────────────────────┘
```

Vision and scoring have to live on the Linux side; relays and I2C have to live on the MCU,
because App Lab containerises the Python process without GPIO or I2C access. The Bridge RPC
layer is what makes the split work — Python calls handlers the sketch registers with
`Bridge.provide()`, and calls return synchronously.

## Repository layout

```
python/          Flask app, vision pipeline, scoring, storage, LLM summary
python/config.py All tunable constants — start here
sketch/          MCU sketch: Bridge handlers, relays, LCD
assets/          Dashboard HTML, CSS, JavaScript
```

---

## Running it on your own UNO Q

**1. Install the Bricks** (App Lab → Add App Brick):

- WebUI-HTML — dashboard hosting
- Object Detection — select the general-purpose COCO model
- Large Language Model — download Qwen 3.5 0.8B (507 MB)

**2. Install the sketch library** (App Lab → Sketch Libraries): `hd44780@1.3.2`, which
auto-detects the LCD's I2C address.

**3. Copy the project** into `~/ArduinoApps/` on your board and open it in App Lab.

**4. Plug in a USB webcam** and confirm which node is the real capture device:

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --all | grep -A5 "Device Caps"
```

Look for `Video Capture`. Indices shuffle between reboots — the app resolves the camera by its
`by-id` path for exactly this reason.

**5. Wire** the relay module and LCD as above.

**6. Set `DASHBOARD_HOST`** in `python/config.py` to your board's LAN IP. The Python app runs in
a container, so an auto-detected address resolves to the Docker bridge network rather than your
actual LAN.

**7. Press Run**, then open `http://<board-ip>:7000` from any device on the same network.

### Calibration

Sit in your normal working position and use the calibrate control on the dashboard. The posture
proxy is measured relative to your own bounding-box geometry, so it needs one reference frame
from you before the score means anything.

---

## Configuration

Everything tunable lives in `python/config.py`:

| Constant | Purpose |
|---|---|
| `DASHBOARD_HOST` | Board LAN IP — must be set by hand |
| `PHYSICAL_ENABLED` | Master switch for relay and LCD output |
| `PHONE_ON_FRAMES` | Frames of phone detection before it counts |
| `PHONE_HOLD_SECONDS` | Hold time before a phone detection is released |
| `POSTURE_SMOOTHING_ALPHA` | Smoothing applied to the posture score |

---

## Design notes and known limitations

**MediaPipe does not run on this board.** The UNO Q's Cortex-A53 is ARMv8.0 and lacks the `lse`,
`dotprod`, `fp16`, `rdm` and `rcpc` extensions MediaPipe's prebuilt wheels assume — it crashes
with SIGILL. The fallback to `cv2.dnn` was then blocked by App Lab's pinned OpenCV build.
Bounding-box posture geometry and Haar-cascade gaze are the result of that constraint rather
than a first preference, and they run comfortably in real time on the CPU.

**Never list `opencv-python-headless` in `requirements.txt`.** App Lab pins a board-optimised
build through a global `UV_CONSTRAINT` that does not exist on PyPI, so declaring it yourself
produces an unsatisfiable resolution.

**Gaze degrades under backlight.** Haar cascades are sensitive to lighting; with a window behind
you the gaze signal reports face-not-visible. Front-facing light fixes it.

**The dashboard has no authentication.** Anyone on the same network can reach port 7000. Worth
knowing before running it on public Wi-Fi.

A 5 V DC fan cannot be driven from this setup. Switching a small DC motor through the relay board caused a supply brownout that put the UNO Q into a boot loop. The motor was removed and the demonstration load reduced to a 5 V LED. Driving inductive loads would need a separate supply with proper isolation and flyback protection.
---



## Demo

Video: _add link_


