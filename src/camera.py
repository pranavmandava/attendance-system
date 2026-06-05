"""Camera helpers for the Axon USB webcam."""

from __future__ import annotations

import cv2

from src.config import APP_CAMERA_INDICES, APP_FRAME_HEIGHT, APP_FRAME_WIDTH


def open_camera(indices: list[int] | None = None) -> cv2.VideoCapture:
    """Open the first working camera, preferring Axon indices (1 before 0)."""
    for idx in indices or APP_CAMERA_INDICES:
        cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            cap.release()
            continue
        ret, _ = cap.read()
        if not ret:
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, APP_FRAME_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, APP_FRAME_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, 30)
        return cap

    raise RuntimeError(
        f"No working camera found (tried indices {indices or APP_CAMERA_INDICES})"
    )
