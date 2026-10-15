"""
Thin wrapper around OpenCV's VideoCapture, with a small amount of retry
logic since USB webcams on a fresh SBC boot can take a moment to enumerate.
"""
import os
import glob
import time
import logging

import cv2

logger = logging.getLogger("workspace_ai.camera")

# /dev/v4l/by-id symlinks are tied to the physical USB device, unlike
# /dev/video* indices which shuffle on every replug and power cycle.
# "-video-index0" is the capture node; "-video-index1" on the same device
# is metadata-only and must be excluded.
BY_ID_GLOB = "/dev/v4l/by-id/*-video-index0"


class Camera:
    def __init__(self, index: int, width: int, height: int):
        self.index = index
        self.width = width
        self.height = height
        self._cap = None

    @staticmethod
    def _try_index(source, settle: float = 0.3, read_attempts: int = 3):
        """Return an opened, frame-verified VideoCapture for source, or None.

        `source` is anything cv2.VideoCapture() accepts - a numeric index or
        a device path string. Some V4L2 devices (e.g. the Qualcomm Venus
        encoder nodes) accept cv2.VideoCapture() but never produce a usable
        frame, so isOpened() alone can't be trusted. We also give
        freshly-opened devices a short settle before the first read, and
        retry a couple of times, since some V4L2 devices aren't ready for
        their first read() immediately after opening.
        """
        cap = cv2.VideoCapture(source)
        opened = cap.isOpened()
        if not opened:
            logger.info("Camera probe %s: opened=False", source)
            cap.release()
            return None

        time.sleep(settle)

        ok, frame = False, None
        for attempt in range(1, read_attempts + 1):
            ok, frame = cap.read()
            if ok and frame is not None and frame.size > 0:
                break
            time.sleep(settle)

        shape = frame.shape if (frame is not None and hasattr(frame, "shape")) else None
        logger.info(
            "Camera probe %s: opened=%s frame_ok=%s shape=%s",
            source, opened, bool(ok and frame is not None and frame.size > 0), shape,
        )

        if not ok or frame is None or frame.size == 0:
            cap.release()
            return None
        return cap

    @staticmethod
    def _probe_by_id():
        """Try stable /dev/v4l/by-id/*-video-index0 symlinks in turn.

        Returns (path, cap) for the first one that opens and yields a valid
        frame, or (None, None) if none work.
        """
        for path in sorted(glob.glob(BY_ID_GLOB)):
            cap = Camera._try_index(path)
            if cap is not None:
                return path, cap
        return None, None

    def _acquire(self):
        """Find a working camera source, trying an explicit override first.

        /dev/video* indices are unstable across boots/replugs since the
        Qualcomm Venus encoder and the USB webcam can swap positions, so we
        prefer the stable /dev/v4l/by-id symlink for the webcam's capture
        node, falling back to probing numeric indices only if no by-id
        symlink works.
        """
        override = os.environ.get("WAI_CAMERA_INDEX")
        if override is not None:
            cap = self._try_index(int(override))
            if cap is not None:
                return int(override), cap

        path, cap = self._probe_by_id()
        if cap is not None:
            return path, cap

        for i in range(6):
            cap = self._try_index(i)
            if cap is not None:
                return i, cap

        return None, None

    def open(self, retries: int = 5, delay: float = 1.0):
        for attempt in range(1, retries + 1):
            index, cap = self._acquire()
            if cap is not None:
                self._cap = cap
                self.index = index
                self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                # Keep the internal buffer small so we always read a fresh
                # frame instead of a stale queued one.
                self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                logger.info("Camera source selected: %s (attempt %s)", self.index, attempt)
                return
            logger.warning("Camera open failed (attempt %s/%s), retrying in %.1fs", attempt, retries, delay)
            time.sleep(delay)
        raise RuntimeError(
            f"Could not open webcam at {self.index} after {retries} attempts. "
            "Check the USB hub connection and run `v4l2-ctl --list-devices` on the board."
        )

    def read(self):
        """Returns (success, frame_bgr) or (False, None)."""
        if self._cap is None:
            raise RuntimeError("Camera not opened - call open() first")
        ok, frame = self._cap.read()
        if not ok:
            return False, None
        return True, frame

    def release(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None
