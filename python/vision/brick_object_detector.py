"""
Single wrapper around App Lab's Object Detection Brick, used for two things
from ONE detect() call per frame (avoids paying for a second network/inference
round-trip to the Brick):
  1. The best "person" bounding box, fed to BboxPostureEstimator.
  2. Whether a "cell phone" was detected, fed into engagement scoring.

Confirmed Brick API:
    from arduino.app_bricks.object_detection import ObjectDetection
    od = ObjectDetection(confidence=0.3)
    out = od.detect(image_bytes)
    # out = {"detection": [{"class_name": "...", "confidence": 0-100,
    #                        "bounding_box_xyxy": [x1,y1,x2,y2]}, ...]}

Note: the Brick's constructor takes one confidence threshold, but individual
calls can override it - we use the lower of our two configured thresholds at
construction time to make sure neither class gets filtered out before we see
it, then apply per-class thresholds ourselves on the raw results below.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple, List

import cv2

logger = logging.getLogger("workspace_ai.brick_detector")

PERSON_CLASS_NAME = "person"
PHONE_CLASS_NAME = "cell phone"


@dataclass
class BrickDetectionResult:
    enabled: bool
    person_bbox_xyxy: Optional[Tuple[float, float, float, float]] = None
    person_confidence: float = 0.0
    phone_detected: bool = False
    phone_confidence: float = 0.0
    raw_detections: List[dict] = field(default_factory=list)


class BrickObjectDetector:
    def __init__(self, person_conf_threshold: float, phone_conf_threshold: float):
        self.person_conf_threshold = person_conf_threshold
        self.phone_conf_threshold = phone_conf_threshold
        self.enabled = False
        self._detector = None

        # Construct with the lower of the two thresholds so the Brick itself
        # doesn't pre-filter out results we still want to evaluate ourselves.
        construct_conf = min(person_conf_threshold, phone_conf_threshold) * 0.7

        try:
            from arduino.app_bricks.object_detection import ObjectDetection
            self._detector = ObjectDetection(confidence=construct_conf)
            self.enabled = True
            logger.info("Object Detection Brick loaded (construct_conf=%.2f).", construct_conf)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully, never crash the app
            logger.warning("Object Detection Brick unavailable (%s) - detection disabled.", exc)
            self.enabled = False

    def detect(self, frame_bgr) -> BrickDetectionResult:
        if not self.enabled:
            return BrickDetectionResult(enabled=False)

        try:
            ok, jpeg = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok:
                return BrickDetectionResult(enabled=True)

            out = self._detector.detect(jpeg.tobytes())
            if not out or "detection" not in out:
                return BrickDetectionResult(enabled=True)

            detections = out["detection"]

            best_person = None
            best_person_conf = 0.0
            phone_detected = False
            best_phone_conf = 0.0

            for d in detections:
                name = d.get("class_name")
                # Brick returns confidence as a 0-100 percentage, per its docs.
                conf_pct = float(d.get("confidence", 0.0))
                conf = conf_pct / 100.0 if conf_pct > 1.0 else conf_pct

                if name == PERSON_CLASS_NAME and conf >= self.person_conf_threshold:
                    if conf > best_person_conf:
                        best_person_conf = conf
                        bbox = d.get("bounding_box_xyxy")
                        if bbox and len(bbox) == 4:
                            best_person = tuple(float(v) for v in bbox)

                elif name == PHONE_CLASS_NAME and conf >= self.phone_conf_threshold:
                    phone_detected = True
                    best_phone_conf = max(best_phone_conf, conf)

            return BrickDetectionResult(
                enabled=True,
                person_bbox_xyxy=best_person,
                person_confidence=round(best_person_conf, 2),
                phone_detected=phone_detected,
                phone_confidence=round(best_phone_conf, 2),
                raw_detections=detections,
            )

        except Exception as exc:  # noqa: BLE001
            logger.warning("Object Detection Brick call failed (%s) - skipping this frame.", exc)
            return BrickDetectionResult(enabled=True)
