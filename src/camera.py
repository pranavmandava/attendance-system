"""Camera helpers for the Axon USB webcam."""

from __future__ import annotations

import cv2

from src.config import (
    APP_CAMERA_INDICES,
    APP_CAMERA_ZOOM,
    APP_FRAME_HEIGHT,
    APP_FRAME_WIDTH,
)


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


def prepare_frame(frame):
    """Normalize resolution and apply optional digital zoom for UI + detection."""
    if frame.shape[0] != APP_FRAME_HEIGHT or frame.shape[1] != APP_FRAME_WIDTH:
        frame = cv2.resize(
            frame,
            (APP_FRAME_WIDTH, APP_FRAME_HEIGHT),
            interpolation=cv2.INTER_LINEAR,
        )

    zoom = float(APP_CAMERA_ZOOM)
    if zoom <= 1.0:
        return frame

    h, w = frame.shape[:2]
    crop_w = max(1, int(round(w / zoom)))
    crop_h = max(1, int(round(h / zoom)))
    x1 = (w - crop_w) // 2
    y1 = (h - crop_h) // 2
    cropped = frame[y1 : y1 + crop_h, x1 : x1 + crop_w]
    return cv2.resize(
        cropped,
        (APP_FRAME_WIDTH, APP_FRAME_HEIGHT),
        interpolation=cv2.INTER_LINEAR,
    )
