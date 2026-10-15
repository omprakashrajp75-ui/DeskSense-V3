"""
Gaze/facing-camera proxy using OpenCV's bundled DNN face detector - a small,
standard convolutional model (res10 SSD), NOT MediaPipe. It uses only
ordinary conv-net ops through OpenCV's DNN module, so it doesn't hit the
missing ARM instruction extensions that crash MediaPipe on this board's CPU.

Honest framing: this is not true gaze/eye tracking. It detects whether a
*frontal* face is visible and roughly centered. A person turned away or in
strong profile typically won't be picked up by a frontal detector - which we
lean on deliberately as the "looking away" signal. It's coarse, but it's a
real, CPU-safe approximation, not a guess dressed up as precision.

Model files (small, ~10MB total) need to be present at the paths in
config.py - see python/models/README.md for the fetch command.
"""
import os
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger("workspace_ai.face_gaze")


@dataclass
class GazeResult:
    face_present: bool
    looking_at_screen: bool = False
    confidence: float = 0.0


class OpenCVFaceGazeEstimator:
    def __init__(self, prototxt_path: str, model_path: str, conf_threshold: float, center_tolerance: float):
        self.conf_threshold = conf_threshold
        self.center_tolerance = center_tolerance
        self.enabled = False
        self._mode = None  # "dnn" or "haar"
        self._net = None
        self._cascade = None

        if not (os.path.isfile(prototxt_path) and os.path.isfile(model_path)):
            logger.warning(
                "Face detector model files missing (%s / %s) - gaze scoring disabled, "
                "engagement will fall back to presence+phone only. See python/models/README.md.",
                prototxt_path, model_path,
            )
            return

        if hasattr(cv2, "dnn"):
            try:
                self._net = cv2.dnn.readNetFromCaffe(prototxt_path, model_path)
                self._mode = "dnn"
                self.enabled = True
                logger.info("Face/gaze detector: using DNN (res10 SSD).")
                return
            except Exception as exc:  # noqa: BLE001 - degrade gracefully, never crash the app
                logger.warning("Failed to load DNN face detector (%s) - gaze scoring disabled.", exc)
                return

        # This board's OpenCV build has no cv2.dnn module - fall back to the
        # (less accurate, but dependency-free) Haar cascade face detector
        # bundled with OpenCV itself.
        logger.warning("cv2.dnn not available on this OpenCV build - falling back to Haar cascade face detector.")
        try:
            cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
            cascade = cv2.CascadeClassifier(cascade_path)
            if cascade.empty():
                raise RuntimeError(f"Could not load cascade file at {cascade_path}")
            self._cascade = cascade
            self._mode = "haar"
            self.enabled = True
            logger.info("Face/gaze detector: using Haar cascade fallback.")
        except Exception as exc:  # noqa: BLE001 - degrade gracefully, never crash the app
            logger.warning("Failed to load Haar cascade fallback (%s) - gaze scoring disabled.", exc)

    def process(self, frame_bgr) -> GazeResult:
        if not self.enabled:
            return GazeResult(face_present=False)
        if self._mode == "haar":
            return self._process_haar(frame_bgr)
        return self._process_dnn(frame_bgr)

    def _process_dnn(self, frame_bgr) -> GazeResult:
        try:
            h, w = frame_bgr.shape[:2]
            blob = cv2.dnn.blobFromImage(
                cv2.resize(frame_bgr, (300, 300)), 1.0, (300, 300),
                (104.0, 177.0, 123.0),
            )
            self._net.setInput(blob)
            detections = self._net.forward()

            best_conf = 0.0
            best_box: Optional[Tuple[float, float, float, float]] = None
            for i in range(detections.shape[2]):
                conf = float(detections[0, 0, i, 2])
                if conf > best_conf:
                    box = detections[0, 0, i, 3:7] * np.array([w, h, w, h])
                    best_conf = conf
                    best_box = box

            if best_box is None or best_conf < self.conf_threshold:
                return GazeResult(face_present=False)

            x1, y1, x2, y2 = best_box
            face_center_x = (x1 + x2) / 2.0
            frame_center_x = w / 2.0
            offset_ratio = abs(face_center_x - frame_center_x) / w

            looking_at_screen = offset_ratio <= self.center_tolerance
            return GazeResult(face_present=True, looking_at_screen=looking_at_screen, confidence=round(best_conf, 2))

        except Exception as exc:  # noqa: BLE001
            logger.warning("Face/gaze detection failed on a frame (%s) - skipping this sample.", exc)
            return GazeResult(face_present=False)

    def _process_haar(self, frame_bgr) -> GazeResult:
        try:
            h, w = frame_bgr.shape[:2]
            gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
            faces = self._cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60),
            )

            if len(faces) == 0:
                return GazeResult(face_present=False)

            # Haar gives no confidence score - take the largest face as the
            # one that matters and treat "found at least one" as face-visible.
            # detectMultiScale returns numpy int32 rows - cast to plain
            # Python types here so nothing numpy-typed (e.g. numpy.bool_,
            # which FastAPI's JSON encoder chokes on) leaks into GazeResult.
            x, y, fw, fh = (int(v) for v in max(faces, key=lambda f: f[2] * f[3]))
            face_center_x = x + fw / 2.0
            frame_center_x = w / 2.0
            offset_ratio = abs(face_center_x - frame_center_x) / w

            looking_at_screen = bool(offset_ratio <= self.center_tolerance)
            return GazeResult(face_present=True, looking_at_screen=looking_at_screen, confidence=1.0)

        except Exception as exc:  # noqa: BLE001
            logger.warning("Face/gaze detection failed on a frame (%s) - skipping this sample.", exc)
            return GazeResult(face_present=False)
