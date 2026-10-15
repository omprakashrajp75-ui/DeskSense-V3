"""
Small thread-safe box for the latest raw frame, JPEG preview, and scored
sample - shared between the video-pump thread, the scoring loop(), and the
web layer, so they never step on each other or read torn state.
"""
import threading
from typing import Optional


class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self._latest_jpeg: Optional[bytes] = None
        self._latest_raw_frame = None  # numpy array (BGR), written by the video pump thread
        self._latest_sample = None
        self._camera_ok = False
        self._physical = {"available": False, "relays": {}, "environment": {}}

    def update_frame(self, jpeg_bytes: bytes):
        with self._lock:
            self._latest_jpeg = jpeg_bytes

    def get_frame(self):
        with self._lock:
            return self._latest_jpeg

    def update_raw_frame(self, frame):
        with self._lock:
            self._latest_raw_frame = frame

    def get_raw_frame(self):
        with self._lock:
            return self._latest_raw_frame

    def update_sample(self, sample):
        with self._lock:
            self._latest_sample = sample

    def get_sample(self):
        with self._lock:
            return self._latest_sample

    def set_camera_ok(self, ok: bool):
        with self._lock:
            self._camera_ok = ok

    def is_camera_ok(self):
        with self._lock:
            return self._camera_ok

    def update_physical(self, snapshot: dict):
        with self._lock:
            self._physical = snapshot

    def get_physical(self):
        with self._lock:
            return self._physical
