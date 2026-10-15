"""
Posture proxy derived from a person bounding box, as a fallback for boards
where MediaPipe Pose can't run (see README - this board's CPU lacks ARM
extensions MediaPipe's prebuilt wheels require).

Honest framing: this is NOT joint-angle posture detection. A single bounding
box carries far less information than pose landmarks - we can't measure
spine curvature or neck angle directly. What we CAN detect is *drift from
your own recent baseline*: has your head dropped lower in frame, has the box
shrunk (leaning back/away from camera), has it shifted sideways. That's a
comparative signal, not an absolute one, and it resets whenever you sit back
down after being away (a fresh baseline is captured each time).

Output shape (PostureResult) intentionally matches the old MediaPipe-based
pose_estimator.py exactly, so nothing downstream - scoring.py, main.py's
draw_overlay, the dashboard - needs to change. This is a drop-in swap.
"""
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Tuple

import config

logger = logging.getLogger("workspace_ai.bbox_posture")

CALIBRATION_SECONDS = 8.0  # how long to watch before trusting a baseline
RECALIBRATE_AFTER_ABSENCE = True  # fresh baseline each time you sit back down


@dataclass
class PostureResult:
    person_present: bool
    score: float = 0.0
    label: str = "no_person"  # no_person | calibrating | good | fair | poor
    issues: List[str] = field(default_factory=list)
    landmarks_px: Optional[list] = None  # unused here, kept for shape compatibility


class BboxPostureEstimator:
    def __init__(self):
        self._baseline_top_ratio: Optional[float] = None
        self._baseline_height_ratio: Optional[float] = None
        self._baseline_center_x_ratio: Optional[float] = None
        self._calibration_started_at: Optional[float] = None
        self._calibration_samples: List[Tuple[float, float, float]] = []  # (top, height, center_x) ratios

    def reset_calibration(self):
        """Call this when the person goes 'away' so the next sit-down gets a fresh baseline."""
        self._baseline_top_ratio = None
        self._baseline_height_ratio = None
        self._baseline_center_x_ratio = None
        self._calibration_started_at = None
        self._calibration_samples = []

    def process(self, person_bbox_xyxy, frame_width: int, frame_height: int) -> PostureResult:
        """
        person_bbox_xyxy: (x1, y1, x2, y2) in pixel coordinates for the
        highest-confidence "person" detection this frame, or None if no
        person was detected.
        """
        if person_bbox_xyxy is None:
            # Person left - next time they return, recalibrate against their
            # new seating position rather than comparing to a stale baseline.
            if RECALIBRATE_AFTER_ABSENCE:
                self.reset_calibration()
            return PostureResult(person_present=False, score=0.0, label="no_person")

        x1, y1, x2, y2 = person_bbox_xyxy
        box_height = max(y2 - y1, 1.0)
        box_width = max(x2 - x1, 1.0)
        top_ratio = y1 / frame_height
        height_ratio_of_frame = box_height / frame_height
        center_x_ratio = ((x1 + x2) / 2.0) / frame_width

        now = time.monotonic()

        # --- Calibration phase ---
        if self._calibration_started_at is None:
            self._calibration_started_at = now

        if self._baseline_height_ratio is None:
            self._calibration_samples.append((top_ratio, height_ratio_of_frame, center_x_ratio))
            elapsed = now - self._calibration_started_at
            if elapsed < CALIBRATION_SECONDS:
                return PostureResult(person_present=True, score=0.0, label="calibrating")

            # Calibration window complete - lock in the median as baseline
            # (median, not mean, so one bad frame doesn't skew it).
            tops = sorted(s[0] for s in self._calibration_samples)
            heights = sorted(s[1] for s in self._calibration_samples)
            centers = sorted(s[2] for s in self._calibration_samples)
            mid = len(tops) // 2
            self._baseline_top_ratio = tops[mid]
            self._baseline_height_ratio = heights[mid]
            self._baseline_center_x_ratio = centers[mid]
            self._calibration_samples = []
            logger.info(
                "Posture baseline calibrated: top=%.3f height=%.3f center_x=%.3f",
                self._baseline_top_ratio, self._baseline_height_ratio, self._baseline_center_x_ratio,
            )

        # --- Scoring phase (baseline is set) ---
        issues = []
        penalty = 0.0

        # Head dropped lower in frame than baseline -> likely slouching down
        top_drift = top_ratio - self._baseline_top_ratio
        if top_drift > 0.06:
            severity = min((top_drift - 0.06) / 0.20, 1.0)
            penalty += severity * 45
            issues.append("head_drop")

        # Box shrank vs baseline -> leaning back / moved away from camera
        # (ambiguous with "sitting further back" - flagged as a softer signal)
        height_ratio_vs_baseline = height_ratio_of_frame / self._baseline_height_ratio
        if height_ratio_vs_baseline < 0.85:
            severity = min((0.85 - height_ratio_vs_baseline) / 0.25, 1.0)
            penalty += severity * 25
            issues.append("distance_change")

        # Shifted sideways vs baseline -> leaning to one side
        center_drift = abs(center_x_ratio - self._baseline_center_x_ratio)
        if center_drift > 0.12:
            severity = min((center_drift - 0.12) / 0.20, 1.0)
            penalty += severity * 30
            issues.append("leaning_sideways")

        score = max(0.0, 100.0 - penalty)
        if score >= config.POSTURE_ALERT_THRESHOLD:
            label = "good"
        elif score >= 60:
            label = "fair"
        else:
            label = "poor"

        return PostureResult(
            person_present=True,
            score=round(score, 1),
            label=label,
            issues=issues,
        )


POSTURE_SUGGESTIONS = {
    "head_drop": "You've drifted lower than your calibrated baseline - sit up and re-align with your screen.",
    "distance_change": "Your distance from the camera has changed noticeably - hard to judge posture precisely from here; try staying in a consistent spot.",
    "leaning_sideways": "You're leaning to one side compared to your baseline - try to stay centered.",
}
