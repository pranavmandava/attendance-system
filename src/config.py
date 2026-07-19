"""Central configuration store.

All configuration variables should be defined here.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# --- Project paths ---------------------------------------------------------
ROOT_DIR = Path(__file__).parent.parent
load_dotenv(ROOT_DIR / ".env")
MODELS_DIR = ROOT_DIR / "models"
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
ASSETS_DIR = ROOT_DIR / "assets"
KCC_LOGO_PATH = ASSETS_DIR / "kcc-logo.jpg"


# --- Primary SQLite database (attendance + InspireFace FeatureHub tables) ---
DB_PATH = str(DATA_DIR / "attendance.db")

# --- Enrollment Images Directory -------------------------------------------
ENROLLMENT_IMAGES_DIR = DATA_DIR / "enrollment_images"
ENROLLMENT_IMAGES_DIR.mkdir(exist_ok=True)

# --- Capture-based enrollment ------------------------------------------------
# Enrollment happens at the kiosk camera (same lighting/position as recognition).
ENROLL_SAMPLES_REQUIRED = 5  # quality-gated samples averaged into one embedding
ENROLL_MIN_FACE_WIDTH = 140  # px at 640x480; forces the subject close enough
ENROLL_CENTER_TOLERANCE = 0.25  # face center within +/-25% of frame center
ENROLL_MIN_SHARPNESS = 60.0  # Laplacian variance on the face crop (blur gate)
ENROLL_MIN_SELF_SIMILARITY = 0.55  # each sample must agree with the running mean
ENROLL_MIN_SAMPLE_GAP_SECONDS = 0.4  # spread samples over time for pose variety
ENROLL_TIMEOUT_SECONDS = 15.0  # abort if samples can't be collected in time

# --- GUI Application (PySide6) ---------------------------------------------
APP_ENROL_FRAMES = 20
APP_FRAME_WIDTH = 640
APP_FRAME_HEIGHT = 480
# Digital zoom: center-crop then scale back to APP_FRAME_* (1.0 = off).
# Helps enrollment when the USB cam FOV is too wide for kids standing farther back.
APP_CAMERA_ZOOM = 1.4
APP_TIMER_INTERVAL_MS = 33  # For ~30 FPS

APP_HIBERNATE_INTERVAL_MS = 5000  # Check for wake-up every 5 seconds
APP_BLACK_FRAME_THRESHOLD = 5.0  # Avg pixel value below which frame is considered black
# Axon USB camera is on /dev/video1; index 0 often times out.
# On macOS (dev machine) the built-in camera is index 0 - index 1 can be a
# Continuity/virtual camera that opens but delivers blank frames.
if sys.platform == "darwin":
    APP_CAMERA_INDICES = [0]
else:
    APP_CAMERA_INDICES = [1, 0, 2]

# --- InspireFace ------------------------------------------------------------
INSPIREFACE_MODEL_NAME = "Pikachu"
# Pikachu modelpack default; matches official sample threshold
SIMILARITY_THRESHOLD = 0.48
# Gundam_RK3588 targets NPU but currently segfaults on Axon — do not use yet
# INSPIREFACE_MODEL_NAME = "Gundam_RK3588"

# --- Flask API server runtime ----------------------------------------------
AXON_DEBUG = os.environ.get("AXON_DEBUG", "").lower() in {"1", "true", "yes"}
AXON_HOST = os.environ.get("AXON_HOST", "127.0.0.1")
AXON_PORT = int(os.environ.get("AXON_PORT", "1337"))

# FeatureHub persists embeddings in the same SQLite file as attendance metadata
DATABASE_PATH = DB_PATH

# --- kcc-app device sync (cloud REST + SSE) --------------------------------
KCC_API_URL = os.environ.get("KCC_API_URL", "").rstrip("/")
DEVICE_ID = os.environ.get("DEVICE_ID", "")
DEVICE_TOKEN = os.environ.get("DEVICE_TOKEN", "")
