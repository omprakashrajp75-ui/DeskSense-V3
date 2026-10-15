"""
Pomodoro timer. Python is the single source of truth: the web Focus tab and
the TFT both only display what this module reports and send commands back.

Design:
  - The clock is monotonic. While running we hold a deadline; while paused we
    hold the frozen remainder. Nothing depends on wall-clock time, on a web
    page being open, or on how often anyone polls - closing the browser has
    no effect on the timer at all.
  - tick() is called once a second from a background thread in main.py. It is
    the only thing that advances the state machine, so every observer (REST,
    socket push, TFT push) sees the same state.
  - focused_seconds for a session is sampled from the existing engagement
    tracker: each tick during a focus phase, if the live sample's state is
    "engaged", one second is credited. That is the same classification the
    scoring engine already produces - no new scoring logic, and scoring.py is
    untouched.
  - Settings live in their own JSON file in the app data folder (next to the
    SQLite DB and auth.json), so durations survive restarts without touching
    config.py.
  - End of a focus phase fires two hooks, both optional and both wrapped:
    the "happy" eye mood, and a buzzer that is deliberately a no-op until the
    hardware is wired.
"""
import os
import json
import time
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger("workspace_ai.pomodoro")

PHASE_IDLE = "idle"
PHASE_FOCUS = "focus"
PHASE_SHORT = "short_break"
PHASE_LONG = "long_break"

STATE_PAUSED = "paused"

DEFAULT_SETTINGS = {
    "focus_minutes": 25,
    "short_break_minutes": 5,
    "long_break_minutes": 15,
    "sessions_before_long_break": 4,
    "auto_start_next": False,
}

# Guard rails on user-supplied settings so a bad POST can't wedge the timer.
LIMITS = {
    "focus_minutes": (1, 180),
    "short_break_minutes": (1, 60),
    "long_break_minutes": (1, 120),
    "sessions_before_long_break": (1, 12),
}


def default_settings_path(db_path: str) -> str:
    """Settings sit in the same data folder as the SQLite DB, so they inherit
    the app's existing data location without introducing a config value."""
    return os.path.join(os.path.dirname(db_path), "pomodoro.json")


def buzzer_beep(reason: str = "") -> None:
    """Buzzer hook. The buzzer is not wired yet, so this is intentionally a
    safe no-op that only logs - call sites can treat it as real. Replace the
    body (e.g. a Bridge call to a sketch-side tone()) once the hardware is in,
    and nothing else has to change."""
    logger.info("Buzzer hook (no hardware yet): %s", reason or "beep")


class Pomodoro:
    def __init__(self, storage, settings_path: str, shared_state=None,
                 eye_mood_hook=None, buzzer_hook=buzzer_beep):
        self._storage = storage
        self._settings_path = settings_path
        self._shared_state = shared_state
        self._eye_mood_hook = eye_mood_hook
        self._buzzer_hook = buzzer_hook

        self._lock = threading.RLock()
        self.settings = dict(DEFAULT_SETTINGS)
        self._load_settings()

        self._phase = PHASE_IDLE
        self._running = False
        self._total = 0              # planned seconds for the current phase
        self._deadline = 0.0         # monotonic, valid while running
        self._frozen_remaining = 0   # valid while paused
        self._cycle_count = 0        # completed focus phases this run

        # Per-session bookkeeping, flushed to the DB when the phase ends.
        self._session_start_iso = None
        self._session_focused = 0

    # ---------- settings ----------

    def _load_settings(self):
        if not os.path.exists(self._settings_path):
            return
        try:
            with open(self._settings_path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            for key in DEFAULT_SETTINGS:
                if key in blob:
                    self.settings[key] = blob[key]
            logger.info("Pomodoro settings loaded from %s", self._settings_path)
        except Exception:  # noqa: BLE001 - a corrupt file must not brick the app
            logger.exception("Could not read %s - using defaults.", self._settings_path)

    def _save_settings_locked(self):
        try:
            os.makedirs(os.path.dirname(self._settings_path), exist_ok=True)
            tmp = self._settings_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.settings, fh)
            os.replace(tmp, self._settings_path)
        except Exception:  # noqa: BLE001
            logger.exception("Could not write %s", self._settings_path)

    def update_settings(self, incoming: dict):
        """Returns (settings, error). Only known keys are accepted, each
        clamped to its range. A duration change applies to the *next* phase,
        never retroactively to a phase already counting down."""
        with self._lock:
            for key, (lo, hi) in LIMITS.items():
                if key not in incoming or incoming[key] is None:
                    continue
                try:
                    value = int(incoming[key])
                except (TypeError, ValueError):
                    return None, f"{key} must be a whole number."
                if value < lo or value > hi:
                    return None, f"{key} must be between {lo} and {hi}."
                self.settings[key] = value

            if "auto_start_next" in incoming and incoming["auto_start_next"] is not None:
                self.settings["auto_start_next"] = bool(incoming["auto_start_next"])

            self._save_settings_locked()
            return dict(self.settings), None

    def _phase_seconds(self, phase: str) -> int:
        if phase == PHASE_FOCUS:
            return int(self.settings["focus_minutes"]) * 60
        if phase == PHASE_SHORT:
            return int(self.settings["short_break_minutes"]) * 60
        if phase == PHASE_LONG:
            return int(self.settings["long_break_minutes"]) * 60
        return 0

    # ---------- internal helpers (all assume the lock is held) ----------

    def _remaining_locked(self) -> int:
        if self._phase == PHASE_IDLE:
            return 0
        if not self._running:
            return max(0, int(self._frozen_remaining))
        return max(0, int(round(self._deadline - time.monotonic())))

    def _begin_locked(self, phase: str, run: bool):
        self._phase = phase
        self._total = self._phase_seconds(phase)
        self._session_start_iso = datetime.now(timezone.utc).isoformat()
        self._session_focused = 0
        if run:
            self._running = True
            self._deadline = time.monotonic() + self._total
        else:
            self._running = False
            self._frozen_remaining = self._total

    def _log_session_locked(self, completed: bool):
        """Writes one pomodoro_sessions row for the phase that just ended."""
        if self._session_start_iso is None or self._phase == PHASE_IDLE:
            return
        try:
            self._storage.insert_pomodoro_session({
                "start": self._session_start_iso,
                "end": datetime.now(timezone.utc).isoformat(),
                "phase": self._phase,
                "planned_seconds": self._total,
                "completed": 1 if completed else 0,
                "focused_seconds": self._session_focused,
            })
        except Exception:  # noqa: BLE001 - a DB hiccup must not stop the timer
            logger.exception("Could not store pomodoro session - continuing.")
        self._session_start_iso = None
        self._session_focused = 0

    def _next_phase_locked(self) -> str:
        if self._phase == PHASE_FOCUS:
            per_long = max(1, int(self.settings["sessions_before_long_break"]))
            return PHASE_LONG if (self._cycle_count % per_long == 0) else PHASE_SHORT
        return PHASE_FOCUS

    def _fire_focus_end_hooks_locked(self):
        """Happy eye mood + buzzer at the end of a *completed* focus phase.
        Both are best-effort: a failing hook must never break the timer."""
        if self._eye_mood_hook is not None:
            try:
                self._eye_mood_hook("happy")
            except Exception:  # noqa: BLE001
                logger.exception("Eye-mood hook failed - continuing.")
        if self._buzzer_hook is not None:
            try:
                self._buzzer_hook("focus phase complete")
            except Exception:  # noqa: BLE001
                logger.exception("Buzzer hook failed - continuing.")

    # ---------- commands ----------

    def start(self):
        """Starts a focus phase from idle, or resumes a paused one."""
        with self._lock:
            if self._phase == PHASE_IDLE:
                self._begin_locked(PHASE_FOCUS, run=True)
            elif not self._running:
                self._running = True
                self._deadline = time.monotonic() + self._frozen_remaining
            return self.snapshot()

    def pause(self):
        with self._lock:
            if self._phase != PHASE_IDLE and self._running:
                self._frozen_remaining = self._remaining_locked()
                self._running = False
            return self.snapshot()

    def resume(self):
        return self.start()

    def toggle(self):
        """One control for the TFT's action pad and the web Start/Pause
        button: whichever of start/pause makes sense right now."""
        with self._lock:
            if self._phase != PHASE_IDLE and self._running:
                return self.pause()
            return self.start()

    def skip(self):
        """Ends the current phase early and moves to the next one. A skipped
        focus phase is logged as not completed, does not count toward the
        long-break cycle, and does not fire the end-of-focus hooks."""
        with self._lock:
            if self._phase == PHASE_IDLE:
                return self.snapshot()
            nxt = self._next_phase_locked()
            self._log_session_locked(completed=False)
            self._begin_locked(nxt, run=bool(self.settings["auto_start_next"]))
            return self.snapshot()

    def reset(self):
        """Abandons the current phase (logged as not completed) and returns to
        idle, clearing the long-break cycle counter."""
        with self._lock:
            if self._phase != PHASE_IDLE:
                self._log_session_locked(completed=False)
            self._phase = PHASE_IDLE
            self._running = False
            self._total = 0
            self._frozen_remaining = 0
            self._cycle_count = 0
            return self.snapshot()

    # ---------- the clock ----------

    def tick(self):
        """Call once a second. Credits focused time and auto-advances the
        phase when the countdown reaches zero."""
        with self._lock:
            if self._phase == PHASE_IDLE or not self._running:
                return self.snapshot()

            # Focused-time credit, taken from the live engagement state.
            if self._phase == PHASE_FOCUS and self._shared_state is not None:
                try:
                    sample = self._shared_state.get_sample()
                    if sample is not None and getattr(sample, "state", None) == "engaged":
                        self._session_focused += 1
                except Exception:  # noqa: BLE001
                    pass

            if self._remaining_locked() > 0:
                return self.snapshot()

            # Phase complete.
            finished = self._phase
            if finished == PHASE_FOCUS:
                self._cycle_count += 1
            self._log_session_locked(completed=True)

            if finished == PHASE_FOCUS:
                self._fire_focus_end_hooks_locked()

            # _next_phase_locked() reads self._phase, which is still the
            # phase that just finished - and the cycle counter it consults has
            # already been incremented above.
            nxt = self._next_phase_locked()
            self._begin_locked(nxt, run=bool(self.settings["auto_start_next"]))
            logger.info("Pomodoro: %s complete -> %s (%s)", finished, nxt,
                        "auto-started" if self._running else "waiting for start")
            return self.snapshot()

    # ---------- reporting ----------

    def _state_name_locked(self) -> str:
        if self._phase == PHASE_IDLE:
            return PHASE_IDLE
        return self._phase if self._running else STATE_PAUSED

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state_name_locked(),
                "phase": self._phase,
                "running": self._running,
                "remaining_s": self._remaining_locked(),
                "total_s": self._total,
                "cycle_count": self._cycle_count,
                "completed_today": self._completed_today(),
                "last_quality_pct": self._last_quality_pct(),
                "settings": dict(self.settings),
            }

    def _completed_today(self) -> int:
        try:
            return self._storage.count_pomodoro_completed_today()
        except Exception:  # noqa: BLE001
            logger.exception("Could not count today's pomodoro sessions.")
            return 0

    def _last_quality_pct(self):
        """focused_seconds / planned_seconds of the most recent completed
        focus session, as a percentage. None when there isn't one yet."""
        try:
            row = self._storage.get_last_completed_focus_session()
        except Exception:  # noqa: BLE001
            logger.exception("Could not read the last pomodoro session.")
            return None
        if not row:
            return None
        planned = row.get("planned_seconds") or 0
        if planned <= 0:
            return None
        focused = row.get("focused_seconds") or 0
        return int(round(min(100.0, (focused / planned) * 100.0)))

    def tft_payload(self) -> dict:
        """The four values the sketch's pomo_state handler takes. Phase is
        sent as a small int so the sketch never parses strings:
        0 idle, 1 focus, 2 short_break, 3 long_break."""
        with self._lock:
            phase_codes = {PHASE_IDLE: 0, PHASE_FOCUS: 1, PHASE_SHORT: 2, PHASE_LONG: 3}
            return {
                "phase": phase_codes.get(self._phase, 0),
                "remaining_s": self._remaining_locked(),
                "total_s": self._total,
                "running": 1 if self._running else 0,
            }
