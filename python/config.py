"""
Central configuration for DeskSense.
Tweak these values instead of hunting through the codebase.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- Camera ----
CAMERA_INDEX = int(os.environ.get("WAI_CAMERA_INDEX", "0"))
FRAME_WIDTH = 640
FRAME_HEIGHT = 480

# ---- Timing (dual cadence) ----
# The live video panel streams at VIDEO_PUSH_INTERVAL_SECONDS (smooth-ish
# preview), while the actual AI scoring - a Brick network call plus local
# inference - only runs every SAMPLE_INTERVAL_SECONDS. Decoupling these two
# is what fixes the "choppy live view" - the video no longer waits on the
# heavier scoring pass to advance.
VIDEO_PUSH_INTERVAL_SECONDS = 0.15   # ~6-7fps live preview
SAMPLE_INTERVAL_SECONDS = 1.0        # AI scoring cadence, per the original spec

# How many per-second samples make up one stored row.
ROLLUP_PERIOD_SECONDS = 300

# ---- Presence ----
# If no person is detected for this many consecutive seconds, we treat the
# desk as unoccupied and pause scoring (avoids polluting stats with "away" time).
ABSENCE_TIMEOUT_SECONDS = 10

# ---- Object detection (Brick) ----
# Separate, independently-tunable confidence thresholds - "person" and
# "cell phone" have very different sizes/appearances in frame, so one
# threshold for both was too blunt an instrument.
#
# Phone confidence was lowered from the original 0.45, then again from 0.28 -
# clearly visible phones were still being missed, so we trade further
# false-positive risk for better recall. Re-tune upward if false positives
# become the bigger problem instead.
PERSON_DETECTION_CONF_THRESHOLD = 0.55
PHONE_DETECTION_CONF_THRESHOLD = 0.15

# ---- Gaze (OpenCV DNN face detector - NOT MediaPipe, CPU-safe on this board) ----
# "Looking at screen" is approximated as "a frontal face was found, roughly
# centered". A person turned away/in profile typically defeats a frontal
# face detector, which is what we lean on here - coarse but real, and
# doesn't touch the ARM instructions MediaPipe's wheels require.
#
# Confidence lowered from an initial 0.6 - live testing on this board's
# webcam (lower light, some noise) showed faces going undetected far more
# than expected at the stricter threshold, capping engagement artificially.
FACE_DETECTOR_PROTOTXT = os.path.join(BASE_DIR, "models", "face_detector.prototxt")
FACE_DETECTOR_MODEL = os.path.join(BASE_DIR, "models", "face_detector.caffemodel")
FACE_DETECTION_CONF_THRESHOLD = 0.4
FACE_CENTER_TOLERANCE = 0.32  # face center within this fraction of frame center counts as "facing screen"

# ---- Engagement scoring weights ----
# Applied in core/scoring.py - kept here so they're easy to re-tune without
# hunting through logic.
#
# "Confirmed looking away" (a face WAS detected, but off-center) is a strong
# signal - full penalty. "Face not visible at all" is genuinely ambiguous -
# could mean looking away, could just mean the detector missed a real face
# (angle/lighting) - so it gets a smaller penalty instead of being treated
# the same as a confirmed away-look. This is what was capping engagement at
# exactly 60 before: both cases were getting the full penalty.
ENGAGEMENT_PENALTY_LOOKING_AWAY_CONFIRMED = 40   # face detected, but not centered
ENGAGEMENT_PENALTY_FACE_UNCERTAIN = 15            # no face detected at all - ambiguous, lighter touch
ENGAGEMENT_PENALTY_PHONE = 45

# ---- Stability (debounce + smoothing) ----
# Raw per-second detections are noisy (motion blur, brief bad angle, webcam
# grain in low light) - without this, booleans like phone_detected flicker
# and scores jump around. DEBOUNCE_FRAMES requires that many consecutive
# consistent readings before a boolean state officially flips (in EITHER
# direction), and SCORE_SMOOTHING_ALPHA exponentially smooths the numeric
# scores so they move steadily rather than jumping frame to frame.
DEBOUNCE_FRAMES = 2
SCORE_SMOOTHING_ALPHA = 0.4  # higher = more responsive/less smooth, lower = smoother/more lag

# Phone must be seen this many consecutive frames before it counts as present.
PHONE_ON_FRAMES = 1
# Once seen, hold the phone-present state this long after it stops being detected.
# Asymmetric on purpose: detection is flaky when the phone is partly occluded.
PHONE_HOLD_SECONDS = 15.0
# EMA weight for the posture score. Lower = smoother, slower to react.
POSTURE_SMOOTHING_ALPHA = 0.25

# ---- Storage ----
DB_PATH = os.path.join(BASE_DIR, "data", "workspace_ai.db")

# ---- Web server ----
# The WebUI-HTML Brick defaults to port 7000 and manages its own server -
# this constant exists only so other code (the QR code generator) knows
# what URL to point at. If you've overridden the Brick's port in app.yaml,
# update this to match.
WEB_PORT = 7000

# The board's LAN IP as seen by phones/laptops on the WiFi. Must be set
# manually: the Python app runs containerised and can only discover its
# own docker-bridge address (192.168.48.x), which is unreachable from
# other devices. Set to None to fall back to auto-detection.
DASHBOARD_HOST = "10.33.41.227"

# ---- Daily summary (local LLM Brick) ----
# On-device inference, no cloud. Qwen 3.5 0.8B (507MB) is chosen over
# Gemma 3 1B (722MB) to stay light on this board's shared 4GB RAM - the
# camera pipeline, Object Detection Brick and web server are all resident
# too. Download the model from the Brick's "AI models" tab in App Lab.
#
# Set LLM_MODEL to None to let the Brick use whatever model is configured
# in App Lab's Brick settings instead of pinning one here. If you pin one
# and the identifier doesn't match what the Brick expects, initialization
# fails gracefully and the dashboard reports the feature as unavailable -
# check the app logs for the exact string it wanted.
LLM_MODEL = None
LLM_SUMMARY_MAX_WAIT_SECONDS = 150

# ---- Physical actuation (relay/LCD/sensors via core/actuators.py) ----
# Master switch for relay/LCD/sensor actuation via the MCU sketch.
# Fails soft when the Bridge or sketch is unavailable (no-ops + one log line).
PHYSICAL_ENABLED = True

# How long the desk lamp stays on after presence drops before releasing -
# avoids flicker when leaning out of frame briefly.
LAMP_OFF_DELAY_SECONDS = 15.0

# Posture score must stay below this threshold, continuously, for
# POSTURE_ALERT_SUSTAIN_SECONDS before the alert light fires - a momentary
# slouch while reaching for something shouldn't trigger it. Releases as soon
# as the score is back at/above the threshold.
POSTURE_ALERT_THRESHOLD = 85
POSTURE_ALERT_SUSTAIN_SECONDS = 10

# Posture buzzer/LEDs (separate from the sustained alert relay above): one
# beep immediately on crossing below POSTURE_ALERT_THRESHOLD, then a repeat
# beep every POSTURE_BEEP_REPEAT_SECONDS while it stays below. Both the LED
# colour and the beep read POSTURE_ALERT_THRESHOLD - no separate cut-off.
POSTURE_BEEP_MS = 300
POSTURE_BEEP_REPEAT_SECONDS = 15.0

# Minimum time between LCD refreshes over the Bridge.
LCD_UPDATE_INTERVAL_SECONDS = 1.0

# Minimum time between TFT status-screen refreshes over the Bridge - half the
# 1s scoring cadence, so the display updates every other scoring pass.
TFT_UPDATE_INTERVAL_SECONDS = 2.0

# How often the touch-pad tab index is read from the MCU. Deliberately much
# faster than TFT_UPDATE_INTERVAL_SECONDS: this is what decides how quickly a
# pad press shows up, and it only costs one cheap Bridge round-trip.
TAB_POLL_INTERVAL_SECONDS = 0.2

# How often the Today view's numbers are pushed to the MCU. Slower than the
# tab poll because these are day-scale totals - the MCU repaints only the
# value boxes that actually changed.
TFT_STATS_INTERVAL_SECONDS = 5.0
