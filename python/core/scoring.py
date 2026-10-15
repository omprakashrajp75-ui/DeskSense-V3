"""
Turns raw per-second detections (bbox-proxy posture + gaze + phone) into:
  - a per-second sample (kept in a short in-memory rolling window)
  - a running accumulator that gets flushed into one hourly_summary row

Mood has been dropped entirely (see project README) - MediaPipe, which
powered it, can't run on this board's CPU, and no hardware-compatible
replacement is available in App Lab's Brick catalog.

STABILITY (v3): raw per-second detections are noisy - live testing showed
engagement capped at exactly 60 (a confirmed-away and a face-not-detected
case were being penalized identically) and visibly jumpy scores frame to
frame. This version:
  1. Separates "face detected but off-center" (confirmed looking away, full
     penalty) from "no face detected at all" (ambiguous - could be a missed
     detection, not necessarily looking away - lighter penalty).
  2. Debounces the phone/gaze booleans so a single noisy frame doesn't flip
     the reported state - requires config.DEBOUNCE_FRAMES consecutive
     consistent readings before a state officially changes.
  3. Exponentially smooths the numeric scores so they move steadily instead
     of jumping around.

State classification per second, used for the "time in state" breakdown:
  - "engaged"     person present, looking at screen (or uncertain), no phone
  - "distracted"  person present, confirmed looking away and/or phone in view
  - "away"        no person detected
"""
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config

logger = logging.getLogger("workspace_ai.scoring")

LIVE_WINDOW_SECONDS = 120  # how much recent history the dashboard sparkline shows


@dataclass
class Sample:
    ts: float
    person_present: bool
    posture_score: float
    posture_label: str
    posture_issues: list
    engagement_score: float
    state: str  # engaged | distracted | away
    looking_at_screen: bool  # debounced/confirmed
    face_present: bool       # debounced/confirmed
    phone_detected: bool     # debounced/confirmed
    person_confidence: float = 0.0
    posture_debug: dict = field(default_factory=dict)
    face_confidence: float = 0.0


class _Debouncer:
    """Requires N consecutive consistent readings before the confirmed state
    flips, in either direction - filters single-frame flicker without adding
    much lag (N=2 at 1 sample/sec is a 2-second worst-case delay)."""

    def __init__(self, frames_required: int, initial: bool = False):
        self.frames_required = max(1, frames_required)
        self._confirmed = initial
        self._streak = 0
        self._streak_value = initial

    def update(self, raw_value: bool) -> bool:
        if raw_value == self._streak_value:
            self._streak += 1
        else:
            self._streak_value = raw_value
            self._streak = 1

        if self._streak >= self.frames_required:
            self._confirmed = self._streak_value
        return self._confirmed


class _HoldDebouncer:
    """Asymmetric debouncer: fast to trigger, slow to release.

    Symmetric debouncing was wrong for phone detection - live testing showed
    the phone staying clearly in frame while detection dropped after 2-3
    seconds, because the COCO detector's confidence on a static, partly
    occluded object fluctuates below threshold for a frame or two. A
    symmetric debouncer treats that as "phone gone".

    This version confirms ON after `on_frames` positive readings, then HOLDS
    that state for `hold_seconds` after the last positive reading before
    releasing. So brief detection gaps don't clear the state, but genuinely
    putting the phone away still clears it within a few seconds."""

    def __init__(self, on_frames: int, hold_seconds: float):
        self.on_frames = max(1, on_frames)
        self.hold_seconds = hold_seconds
        self._confirmed = False
        self._positive_streak = 0
        self._last_positive_at = None

    def update(self, raw_value: bool) -> bool:
        now = time.monotonic()

        if raw_value:
            self._positive_streak += 1
            self._last_positive_at = now
            if self._positive_streak >= self.on_frames:
                self._confirmed = True
        else:
            self._positive_streak = 0
            if self._confirmed and self._last_positive_at is not None:
                if (now - self._last_positive_at) >= self.hold_seconds:
                    self._confirmed = False

        return self._confirmed


class _Smoother:
    """Exponential moving average - keeps displayed/stored scores from
    jumping frame to frame while still tracking real changes within a
    couple of seconds."""

    def __init__(self, alpha: float):
        self.alpha = alpha
        self._value = None

    def update(self, raw_value: float) -> float:
        if self._value is None:
            self._value = raw_value
        else:
            self._value = self.alpha * raw_value + (1 - self.alpha) * self._value
        return self._value

    def reset(self):
        self._value = None


@dataclass
class _Accumulator:
    period_start: datetime
    posture_sum: float = 0.0
    engagement_sum: float = 0.0
    engaged_seconds: int = 0
    distracted_seconds: int = 0
    away_seconds: int = 0
    looking_away_seconds: int = 0
    phone_seconds: int = 0
    posture_good_seconds: int = 0
    posture_fair_seconds: int = 0
    posture_poor_seconds: int = 0
    sample_count: int = 0
    active_sample_count: int = 0  # excludes "away" samples from score averages
    posture_scored_count: int = 0  # excludes "calibrating" samples from posture average


class ScoringEngine:
    def __init__(self, rollup_interval_seconds: int, on_rollup):
        """
        on_rollup: callback(dict) invoked whenever a rollup period completes -
                   wired up to Storage.insert_hourly_summary in main.py.
        """
        self.rollup_interval_seconds = rollup_interval_seconds
        self.on_rollup = on_rollup
        self.live_window = deque(maxlen=LIVE_WINDOW_SECONDS)
        self._acc = _Accumulator(period_start=datetime.now(timezone.utc))
        self._period_start_monotonic = time.monotonic()

        self._phone_debouncer = _HoldDebouncer(config.PHONE_ON_FRAMES, config.PHONE_HOLD_SECONDS)
        self._face_debouncer = _Debouncer(config.DEBOUNCE_FRAMES)
        self._looking_debouncer = _Debouncer(config.DEBOUNCE_FRAMES)
        self._engagement_smoother = _Smoother(config.SCORE_SMOOTHING_ALPHA)
        self._posture_smoother = _Smoother(config.POSTURE_SMOOTHING_ALPHA)

    def add_sample(self, posture_result, gaze_result, phone_detected_raw: bool, person_confidence: float = 0.0) -> Sample:
        now = time.time()
        person_present = posture_result.person_present

        phone_detected = self._phone_debouncer.update(phone_detected_raw)

        if not person_present:
            state = "away"
            engagement_score = 0.0
            looking_at_screen = False
            face_present = False
            self._engagement_smoother.reset()
            self._posture_smoother.reset()
        else:
            face_present = self._face_debouncer.update(gaze_result.face_present)
            raw_looking = gaze_result.looking_at_screen if gaze_result.face_present else False
            looking_at_screen = self._looking_debouncer.update(raw_looking)

            raw_engagement = 100.0
            if face_present and not looking_at_screen:
                # Confirmed: a face WAS found, but it's off-center - a real signal.
                raw_engagement -= config.ENGAGEMENT_PENALTY_LOOKING_AWAY_CONFIRMED
            elif not face_present:
                # Ambiguous: could be looking away, could just be a missed
                # detection (angle/lighting) - lighter penalty so this
                # doesn't silently cap engagement the way it did before.
                raw_engagement -= config.ENGAGEMENT_PENALTY_FACE_UNCERTAIN
            if phone_detected:
                raw_engagement -= config.ENGAGEMENT_PENALTY_PHONE
            raw_engagement = max(0.0, raw_engagement)

            engagement_score = round(self._engagement_smoother.update(raw_engagement), 1)

            # "Distracted" state still reserved for confirmed signals (phone,
            # or confirmed looking away) - a merely-uncertain face doesn't by
            # itself count as distracted, just less engaged.
            state = "distracted" if (phone_detected or (face_present and not looking_at_screen)) else "engaged"

        posture_score = posture_result.score
        if posture_result.label not in ("no_person", "calibrating"):
            posture_score = round(self._posture_smoother.update(posture_result.score), 1)

        sample = Sample(
            ts=now,
            person_present=person_present,
            posture_score=posture_score,
            posture_label=posture_result.label,
            posture_issues=posture_result.issues,
            engagement_score=engagement_score,
            state=state,
            looking_at_screen=looking_at_screen,
            face_present=face_present,
            phone_detected=phone_detected,
            person_confidence=person_confidence,
            posture_debug=getattr(posture_result, "debug", {}) or {},
            face_confidence=getattr(gaze_result, "confidence", 0.0),
        )

        self.live_window.append(sample)
        self._accumulate(sample)
        self._maybe_flush()
        return sample

    def _accumulate(self, sample: Sample):
        acc = self._acc
        acc.sample_count += 1

        if sample.state == "engaged":
            acc.engaged_seconds += 1
        elif sample.state == "distracted":
            acc.distracted_seconds += 1
        else:
            acc.away_seconds += 1

        if sample.person_present:
            acc.active_sample_count += 1
            acc.engagement_sum += sample.engagement_score

            if not sample.looking_at_screen:
                acc.looking_away_seconds += 1
            if sample.phone_detected:
                acc.phone_seconds += 1

            # "calibrating" posture samples carry no meaningful score yet -
            # exclude them from the average rather than dragging it toward 0.
            if sample.posture_label not in ("no_person", "calibrating"):
                acc.posture_scored_count += 1
                acc.posture_sum += sample.posture_score

                if sample.posture_label == "good":
                    acc.posture_good_seconds += 1
                elif sample.posture_label == "fair":
                    acc.posture_fair_seconds += 1
                elif sample.posture_label == "poor":
                    acc.posture_poor_seconds += 1

    def _maybe_flush(self):
        elapsed = time.monotonic() - self._period_start_monotonic
        if elapsed < self.rollup_interval_seconds:
            return
        self._flush()

    @staticmethod
    def _row_from_acc(acc: "_Accumulator", period_end: datetime) -> dict:
        return {
            "period_start": acc.period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "avg_posture_score": round(acc.posture_sum / acc.posture_scored_count, 1) if acc.posture_scored_count else None,
            "avg_engagement_score": round(acc.engagement_sum / acc.active_sample_count, 1) if acc.active_sample_count else None,
            "engaged_seconds": acc.engaged_seconds,
            "distracted_seconds": acc.distracted_seconds,
            "away_seconds": acc.away_seconds,
            "looking_away_seconds": acc.looking_away_seconds,
            "phone_seconds": acc.phone_seconds,
            "posture_good_seconds": acc.posture_good_seconds,
            "posture_fair_seconds": acc.posture_fair_seconds,
            "posture_poor_seconds": acc.posture_poor_seconds,
            "sample_count": acc.sample_count,
        }

    def current_totals(self) -> dict:
        """Snapshot of the in-progress period's running counters, shaped like
        a flushed hourly_summary row - lets /api/today reflect this period's
        progress without waiting for it to roll up."""
        return self._row_from_acc(self._acc, datetime.now(timezone.utc))

    def _flush(self):
        acc = self._acc
        period_end = datetime.now(timezone.utc)

        if acc.active_sample_count == 0:
            logger.info("Rollup period had no active (person-present) samples - skipping DB write.")
        else:
            self.on_rollup(self._row_from_acc(acc, period_end))

        self._acc = _Accumulator(period_start=period_end)
        self._period_start_monotonic = time.monotonic()

    def force_flush(self):
        """Flush whatever is accumulated so far - call on clean shutdown."""
        if self._acc.active_sample_count > 0:
            self._flush()
