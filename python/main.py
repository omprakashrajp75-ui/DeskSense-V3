"""
DeskSense - main entry point.

Runs as an Arduino App Lab App:
    from arduino.app_utils import App
    def loop():
        ...one iteration of work...
    App.run(user_loop=loop)
App Lab calls `loop()` repeatedly.

DUAL CADENCE (new in this revision): the live video panel felt choppy
because the old version only grabbed one frame per full second - the same
frame used for the (comparatively slow) scoring pass. This version splits
that into two independent loops:
  - A background thread continuously grabs frames and pushes them to the
    dashboard at VIDEO_PUSH_INTERVAL_SECONDS (~6-7fps) for a smooth preview.
  - loop() itself (App Lab's callback) only runs the heavier AI scoring
    pass - Object Detection Brick call + gaze + posture - every
    SAMPLE_INTERVAL_SECONDS (1s), same cadence as before.
Both read from the same camera via SharedState's latest-raw-frame slot, so
there's only ever one process reading the USB device.

Mood has been removed entirely - see README for why (hardware limitation,
not a design choice). Posture now comes from BboxPostureEstimator (a
calibrated bounding-box proxy) and gaze from OpenCVFaceGazeEstimator - both
chosen specifically because they don't touch MediaPipe, which crashes on
this board's CPU.
"""
import atexit
import math
import signal
import time
import logging
import threading

import cv2

from arduino.app_utils import App
from arduino.app_bricks.web_ui import WebUI

import config
from core.camera import Camera
from core.state import SharedState
from core.storage import Storage
from core.scoring import ScoringEngine
from core.actuators import PhysicalLayer
from core.pomodoro import Pomodoro, default_settings_path
from vision.bbox_posture_estimator import BboxPostureEstimator
from vision.opencv_face_gaze import OpenCVFaceGazeEstimator
from vision.brick_object_detector import BrickObjectDetector
from web.server import register_web_ui, push_live_sample, push_pomodoro_state
from web.llm_summary import generate_summary
from web.qr import _lan_ip as get_lan_ip

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("workspace_ai.main")


def draw_overlay(frame, sample):
    """Draws the live-feed overlay: status text so the dashboard's real-time
    panel shows what the pipeline currently thinks is happening."""
    state_colors = {"engaged": (0, 200, 0), "distracted": (0, 165, 255), "away": (0, 0, 200)}
    color = state_colors.get(sample.state, (255, 255, 255))
    cv2.putText(frame, f"State: {sample.state.upper()}", (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
    cv2.putText(frame, f"Posture: {sample.posture_score:.0f} ({sample.posture_label})", (10, 50),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    cv2.putText(frame, f"Engagement: {sample.engagement_score:.0f}", (10, 72),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    if sample.phone_detected:
        cv2.putText(frame, "PHONE DETECTED", (10, 96),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    return frame


# ---------------------------------------------------------------------
# Module-level state, set up once and closed over by loop() + the pump thread.
# ---------------------------------------------------------------------
storage = Storage(config.DB_PATH)
shared_state = SharedState()
scoring_engine = ScoringEngine(config.ROLLUP_PERIOD_SECONDS, on_rollup=storage.insert_hourly_summary)

# The in-progress scoring period only gets written to the DB at rollover, so
# without this a stop just before rollover silently drops it. Both paths
# reuse ScoringEngine.force_flush(), which itself only writes when there's an
# active (person-present) period to flush.
atexit.register(scoring_engine.force_flush)


def _handle_sigterm(signum, frame):
    scoring_engine.force_flush()
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _handle_sigterm)

camera = Camera(config.CAMERA_INDEX, config.FRAME_WIDTH, config.FRAME_HEIGHT)
posture_estimator = BboxPostureEstimator()
gaze_estimator = OpenCVFaceGazeEstimator(
    config.FACE_DETECTOR_PROTOTXT, config.FACE_DETECTOR_MODEL,
    config.FACE_DETECTION_CONF_THRESHOLD, config.FACE_CENTER_TOLERANCE,
)
object_detector = BrickObjectDetector(
    config.PERSON_DETECTION_CONF_THRESHOLD, config.PHONE_DETECTION_CONF_THRESHOLD,
)
relay = PhysicalLayer(config)


def _pomodoro_eye_mood(mood: str):
    """End-of-focus eye mood. The sketch's face takes the same accent index
    the rest of the theme uses, where 0 is the happy/green expression, so
    "happy" maps to 0. Note this is advisory: the next periodic push for the
    Buddy tab re-asserts the live state's expression, so the happy face shows
    until the state changes rather than sticking permanently."""
    if mood == "happy":
        relay.draw_face(0)


def _pomodoro_buzzer(reason: str):
    """Two short beeps when a focus phase completes. Runs inside the
    pomodoro's own lock (_fire_focus_end_hooks_locked), so the second beep
    is scheduled via a timer rather than slept here - beep() itself is a
    fast, non-blocking Bridge call either way, but this keeps the hook from
    holding that lock for the gap between the two beeps."""
    relay.beep(300)
    threading.Timer(0.4, relay.beep, args=(300,)).start()


pomodoro = Pomodoro(
    storage,
    default_settings_path(config.DB_PATH),
    shared_state=shared_state,
    eye_mood_hook=_pomodoro_eye_mood,
    buzzer_hook=_pomodoro_buzzer,
)

ui = WebUI()
register_web_ui(ui, shared_state, storage, config.WEB_PORT, scoring_engine, pomodoro=pomodoro)

_stop_event = threading.Event()

# Session clock for the TFT's SESSION field: elapsed time since the person
# currently at the desk arrived, reset once they leave.
_session_start = None

# Last tab index seen from the MCU's touch pads - logged on change only.
_last_tab_seen = None

# TFT content push cadence: every TFT_UPDATE_INTERVAL_SECONDS, or immediately
# on a tab change so switching tabs never waits for the next tick.
_last_tft_tab = None
_last_tft_push = 0.0

# Serialises TFT pushes between the fast tab poller and the slow scoring
# loop. The panel is one SPI device on the MCU side, so two overlapping
# draw_* calls could interleave mid-paint.
_tft_lock = threading.Lock()

# Today-view stats push cadence, driven from the tab poller.
_last_stats_push = 0.0

# Summary tab (3) state machine: idle -> generating -> done/error. Generation
# runs in a background thread since it can take up to LLM_SUMMARY_MAX_WAIT_SECONDS -
# the 1s scoring loop can't block on it.
_summary_lock = threading.Lock()
_summary_state = "idle"
_summary_text = ""
SUMMARY_MAX_CHARS = 160  # roughly 6 lines x ~27 chars at the TFT's text size

# The TFT's Focus (pomodoro) tab. Appended as tab 5 so every existing tab
# index keeps its meaning - must match TAB_FOCUS in sketch.ino.
POMODORO_TAB = 5

_APP_START_MONOTONIC = time.monotonic()


def _session_elapsed_str(sample) -> str:
    global _session_start
    if sample is None or not sample.person_present:
        _session_start = None
        return "00:00"
    if _session_start is None:
        _session_start = time.monotonic()
    elapsed = int(time.monotonic() - _session_start)
    return f"{elapsed // 60:02d}:{elapsed % 60:02d}"


def _fmt_mmss(seconds) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _fmt_hhmmss(seconds) -> str:
    seconds = int(seconds or 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _compute_accent(sample) -> int:
    """0=green (FOCUSED), 1=amber (DISTRACTED), 2=red (DROWSY/poor posture),
    3=blue-grey (AWAY). Order must match accentColor() in sketch.ino.
    Poor posture takes priority over distraction - it's the stronger,
    hardware-observable signal."""
    if sample is None or not sample.person_present:
        return 3
    if sample.posture_label == "poor":
        return 2
    if sample.state == "distracted":
        return 1
    return 0


def _state_word(sample) -> str:
    if sample is None or not sample.person_present:
        return "AWAY"
    if sample.posture_label == "poor":
        return "DROWSY"
    if sample.state == "distracted":
        return "DISTRACTED"
    return "FOCUSED"


def _generate_summary_async():
    """Runs off the scoring loop - .chat() can take up to
    LLM_SUMMARY_MAX_WAIT_SECONDS and must never block loop()."""
    global _summary_state, _summary_text
    try:
        result = generate_summary(storage.get_today())
    except Exception as exc:  # noqa: BLE001
        result = {"status": "error", "message": str(exc)}
    with _summary_lock:
        if result.get("status") == "ok":
            _summary_state = "done"
            _summary_text = result.get("summary", "")
        else:
            _summary_state = "error"
            _summary_text = result.get("message", "Summary generation failed.")


def _js_round(x: float) -> int:
    """JavaScript's Math.round (half away from zero for positives), so the
    TFT shows exactly what the dashboard shows. Python's round() does
    banker's rounding and would disagree on .5 cases."""
    return int(math.floor(x + 0.5))


def _today_stats() -> list:
    """Computes the Today view's numbers for the TFT.

    This is a line-by-line port of assets/app.js - renderStatCards() and
    renderTodayBreakdown() - because that is where the dashboard's Today
    maths actually lives; Python only ever served the raw rows via
    /api/today. No formula is changed here, including the two quirks worth
    knowing about:
      - the DISTRACTION card uses phone_seconds alone, while the breakdown
        row uses the distracted_seconds column (app.js does the same; its
        max(phone, lookingAway) value is computed but never displayed),
      - the averages are weighted by each period's active (engaged +
        distracted) seconds, not by a plain mean.

    Returns the 13 ints tft_stats() expects, in order. -1 means "no scored
    samples" for the two averages, which the sketch renders as "--".
    """
    rows = storage.get_today() + [scoring_engine.current_totals()]

    # --- Row A cards (renderStatCards) ---
    engaged_c = sum(r.get("engaged_seconds") or 0 for r in rows)
    phone = sum(r.get("phone_seconds") or 0 for r in rows)
    posture_poor = sum(r.get("posture_poor_seconds") or 0 for r in rows)
    away_c = sum(r.get("away_seconds") or 0 for r in rows)

    # --- Panel B breakdown (renderTodayBreakdown) ---
    engaged = distracted = away = 0
    posture_weighted = posture_weight = 0.0
    engagement_weighted = engagement_weight = 0.0
    for r in rows:
        e = r.get("engaged_seconds") or 0
        d = r.get("distracted_seconds") or 0
        a = r.get("away_seconds") or 0
        engaged += e
        distracted += d
        away += a

        active = e + d
        if active > 0 and r.get("avg_posture_score") is not None:
            posture_weighted += r["avg_posture_score"] * active
            posture_weight += active
        if active > 0 and r.get("avg_engagement_score") is not None:
            engagement_weighted += r["avg_engagement_score"] * active
            engagement_weight += active

    total = engaged + distracted + away

    def pct(secs):
        return _js_round((secs / total) * 100) if total > 0 else 0

    return [
        _js_round(engaged_c / 60), _js_round(phone / 60),
        _js_round(posture_poor / 60), _js_round(away_c / 60),
        _js_round(engaged / 60), pct(engaged),
        _js_round(distracted / 60), pct(distracted),
        _js_round(away / 60), pct(away),
        _js_round(posture_weighted / posture_weight) if posture_weight > 0 else -1,
        _js_round(engagement_weighted / engagement_weight) if engagement_weight > 0 else -1,
        _js_round(total / 60) if total > 0 else 0,
    ]


def _push_stats():
    """Sends the Today numbers to the MCU. Shares _tft_lock with the other
    TFT pushes so two threads never draw at once, and never raises."""
    try:
        with _tft_lock:
            relay.tft_stats(_today_stats())
    except Exception:  # noqa: BLE001
        logger.exception("Stats push failed - continuing.")


def _push_tab_content(tab: int, sample):
    """Builds and sends the content for whichever tab is currently showing.
    Tabs 0/1/2/4 use draw_tab's fixed title+4-row layout; tab 3 (Summary) is
    free text via draw_text_block.

    Tab 5 (Focus) is deliberately absent: the sketch renders it from the
    pomo_state pushes made by _pomodoro_ticker, so there is no draw_tab
    content for it and this function is a no-op for that index."""
    if tab == POMODORO_TAB:
        return

    accent = _compute_accent(sample)

    if tab == 0:
        # accent already matches the face-expression convention 1:1
        # (0=engaged, 1=distracted, 2=bad posture, 3=absent - see
        # _compute_accent's docstring and the mood IDs in sketch.ino).
        # The face alone (not draw_tab's accent/status-stripe colour) gets
        # the dedicated "Focused" mood (4) instead of plain "Engaged" (0)
        # while a pomodoro focus phase is actively running.
        face_mood = 4 if (accent == 0 and pomodoro.is_focus_running()) else accent
        relay.draw_face(face_mood)
        relay.draw_tab(0, "BUDDY", _state_word(sample), "", "", "", accent)

    elif tab == 1:
        posture_unavailable = sample is None or sample.posture_label in ("no_person", "calibrating")
        posture_value = "--" if posture_unavailable else str(int(round(sample.posture_score)))

        engagement_unavailable = sample is None or sample.state == "away"
        engagement_value = "--" if engagement_unavailable else str(int(round(sample.engagement_score)))

        relay.draw_tab(
            1, "NOW",
            f"STATE {_state_word(sample)}",
            f"POSTURE {posture_value}",
            f"ENGAGE {engagement_value}",
            f"SESSION {_session_elapsed_str(sample)}",
            accent,
        )

    elif tab == 2:
        rows = storage.get_today() + [scoring_engine.current_totals()]
        focused = sum(r.get("engaged_seconds") or 0 for r in rows)
        distracted = sum((r.get("phone_seconds") or 0) + (r.get("looking_away_seconds") or 0) for r in rows)
        slouch = sum(r.get("posture_poor_seconds") or 0 for r in rows)
        away = sum(r.get("away_seconds") or 0 for r in rows)

        relay.draw_tab(
            2, "SESSION",
            f"FOCUSED {_fmt_mmss(focused)}",
            f"DISTRACT {_fmt_mmss(distracted)}",
            f"SLOUCH {_fmt_mmss(slouch)}",
            f"AWAY {_fmt_mmss(away)}",
            accent,
        )

    elif tab == 3:
        with _summary_lock:
            state, text = _summary_state, _summary_text
        if state == "idle":
            body = "Press action pad"
        elif state == "generating":
            body = "Generating..."
        else:
            body = text[:SUMMARY_MAX_CHARS]
        relay.draw_text_block("SUMMARY", body, accent)

    elif tab == 4:
        uptime = _fmt_hhmmss(time.monotonic() - _APP_START_MONOTONIC)
        cam = "OK" if shared_state.is_camera_ok() else "FAIL"
        lamp_on = relay.snapshot().get("relays", {}).get("lamp", False)

        relay.draw_tab(
            4, "SYSTEM",
            f"IP {get_lan_ip()}",
            f"UPTIME {uptime}",
            f"CAM {cam}",
            f"RELAY {'ON' if lamp_on else 'OFF'}",
            accent,
        )

try:
    camera.open()
    shared_state.set_camera_ok(True)
except RuntimeError as exc:
    logger.error("%s", exc)
    shared_state.set_camera_ok(False)


def _video_pump():
    """Background thread: grabs frames continuously and pushes an unscored
    preview to the dashboard at ~6-7fps. Also keeps SharedState's raw-frame
    slot fresh for loop() to read for scoring - only this thread touches the
    camera object, avoiding any risk of two readers on one USB device."""
    while not _stop_event.is_set():
        loop_start = time.monotonic()

        if not shared_state.is_camera_ok():
            time.sleep(config.VIDEO_PUSH_INTERVAL_SECONDS)
            continue

        ok, frame = camera.read()
        if not ok:
            time.sleep(0.2)
            continue

        shared_state.update_raw_frame(frame)

        # Draw the *last known* sample's overlay onto this fresh frame, so the
        # preview looks live even between scoring passes (overlay text may
        # lag up to ~1s behind the video itself - a reasonable tradeoff).
        latest_sample = shared_state.get_sample()
        display_frame = draw_overlay(frame.copy(), latest_sample) if latest_sample else frame

        ok, jpeg = cv2.imencode(".jpg", display_frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            shared_state.update_frame(jpeg.tobytes())
            push_live_sample(ui, latest_sample, shared_state, jpeg.tobytes())

        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, config.VIDEO_PUSH_INTERVAL_SECONDS - elapsed))


_pump_thread = threading.Thread(target=_video_pump, daemon=True)
_pump_thread.start()


def _tab_poller():
    """Background thread: reads the MCU's touch-pad tab index every
    TAB_POLL_INTERVAL_SECONDS and pushes that tab's content the instant it
    changes, so a pad press repaints in ~200ms instead of waiting on the 1s
    scoring loop. The slower periodic content refresh stays in loop() - this
    thread only reacts to tab *changes*."""
    global _last_tab_seen, _last_tft_tab, _last_tft_push, _last_stats_push
    while not _stop_event.is_set():
        try:
            tab = relay.poll_tab()
            if tab is not None and tab != _last_tab_seen:
                logger.info("Tab changed: %s -> %s", _last_tab_seen, tab)
                _last_tab_seen = tab
            if tab is not None and tab != _last_tft_tab:
                with _tft_lock:
                    _push_tab_content(tab, shared_state.get_sample())
                    _last_tft_tab = tab
                    _last_tft_push = time.monotonic()
                # Switching onto the Today tab: send its numbers straight
                # away so the frame it just drew is never briefly stale.
                if tab == 1:
                    _push_stats()
                    _last_stats_push = time.monotonic()

            # Periodic stats refresh, independent of the tab-content cadence.
            if (time.monotonic() - _last_stats_push) >= config.TFT_STATS_INTERVAL_SECONDS:
                _push_stats()
                _last_stats_push = time.monotonic()
        except Exception:  # noqa: BLE001
            # Never let a Bridge hiccup kill the poller - the relay layer
            # already fails soft, this is the belt-and-braces catch.
            logger.exception("Tab poller iteration failed - continuing.")
        _stop_event.wait(config.TAB_POLL_INTERVAL_SECONDS)


_tab_thread = threading.Thread(target=_tab_poller, daemon=True)
_tab_thread.start()


def _pomodoro_ticker():
    """Background thread, 1 Hz: the only thing that advances the pomodoro
    state machine, then fans the new state out to both displays.

    Deliberately its own thread rather than part of loop(): loop() is the
    heavy scoring pass and can overrun a second, and the timer must not drift
    with it. The timer itself is monotonic-deadline based, so even if a tick
    is late the remaining time stays correct."""
    while not _stop_event.is_set():
        try:
            pomodoro.tick()
            # Web: authenticated sids only, handled inside the push helper.
            push_pomodoro_state(ui, pomodoro)
            # TFT: store-only on the MCU side; the sketch repaints the Focus
            # tab's digits and bar from it when that tab is on screen.
            payload = pomodoro.tft_payload()
            with _tft_lock:
                relay.pomo_state(payload["phase"], payload["remaining_s"],
                                 payload["total_s"], payload["running"])
        except Exception:  # noqa: BLE001 - a display hiccup must not stop the clock
            logger.exception("Pomodoro tick failed - continuing.")
        _stop_event.wait(1.0)


_pomodoro_thread = threading.Thread(target=_pomodoro_ticker, daemon=True)
_pomodoro_thread.start()


def loop():
    """Called repeatedly by the App Lab runtime. One call = one scoring pass."""
    global _last_tft_tab, _last_tft_push, _summary_state
    loop_start = time.monotonic()

    # The tab index itself is polled by _tab_poller at
    # TAB_POLL_INTERVAL_SECONDS, which also handles the immediate push on a
    # change; here we just read what it last saw.
    tab = _last_tab_seen

    # Always drain the one-shot action flag so a stale press on another tab
    # doesn't fire the moment you switch to the Summary tab; only act on it
    # there.
    action = relay.poll_action()
    if action and tab == 3 and _summary_state == "idle":
        _summary_state = "generating"
        threading.Thread(target=_generate_summary_async, daemon=True).start()
    elif action and tab == POMODORO_TAB:
        # Short tap on the Focus tab = start/pause. The sketch only arms this
        # flag on release of a press shorter than its 3 s recovery hold, so a
        # recovery hold never reaches here.
        pomodoro.toggle()

    # TFT content: the slow periodic refresh that keeps live values current -
    # independent of camera health, since e.g. the System tab needs to show
    # CAM FAIL live. Tab *changes* are the poller's job, not this one's.
    now = time.monotonic()
    if tab is not None and (now - _last_tft_push) >= config.TFT_UPDATE_INTERVAL_SECONDS:
        with _tft_lock:
            _push_tab_content(tab, shared_state.get_sample())
            _last_tft_tab = tab
            _last_tft_push = now

    if not shared_state.is_camera_ok():
        time.sleep(config.SAMPLE_INTERVAL_SECONDS)
        return

    frame = shared_state.get_raw_frame()
    if frame is None:
        time.sleep(0.2)
        return

    detection = object_detector.detect(frame)

    # Buddy tab eye tracking: the detected person's bbox centre, mapped onto
    # the sketch's -100..100 look_at() range - existing detection data, no
    # new inference. Sent once per scoring pass (~1 Hz, the only cadence at
    # which a fresh bbox exists); the sketch's own idle motion covers the gap
    # between updates and falls back to idle saccades 2s after the last one
    # (see look_at() in sketch.ino).
    if detection.person_bbox_xyxy is not None:
        x1, y1, x2, y2 = detection.person_bbox_xyxy
        frame_w, frame_h = frame.shape[1], frame.shape[0]
        look_x = int(max(-100, min(100, (((x1 + x2) / 2.0 - frame_w / 2) / (frame_w / 2)) * 100)))
        look_y = int(max(-100, min(100, (((y1 + y2) / 2.0 - frame_h / 2) / (frame_h / 2)) * 100)))
        relay.look_at(look_x, look_y)

    gaze_result = gaze_estimator.process(frame)
    posture_result = posture_estimator.process(detection.person_bbox_xyxy, frame.shape[1], frame.shape[0])

    sample = scoring_engine.add_sample(
        posture_result, gaze_result, detection.phone_detected, detection.person_confidence,
    )
    shared_state.update_sample(sample)

    # Physical layer - act on the current sample.
    relay.update_lamp(sample.person_present)
    relay.update_fan(sample.person_present)
    relay.update_posture_alert(sample.posture_score, sample.posture_label, sample.person_present)
    relay.update_feedback(sample)
    relay.update_lcd(sample)
    shared_state.update_physical(relay.snapshot())

    elapsed = time.monotonic() - loop_start
    time.sleep(max(0.0, config.SAMPLE_INTERVAL_SECONDS - elapsed))


App.run(user_loop=loop)
