"""Central configuration store.

All configuration variables should be defined here.
"""

from pathlib import Path

# --- Project paths ---------------------------------------------------------
ROOT_DIR = Path(__file__).parent.parent
MODELS_DIR = ROOT_DIR / "models"
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


# --- Primary SQLite database (attendance + InspireFace FeatureHub tables) ---
DB_PATH = str(DATA_DIR / "attendance.db")

# --- Enrollment Images Directory -------------------------------------------
ENROLLMENT_IMAGES_DIR = DATA_DIR / "enrollment_images"
ENROLLMENT_IMAGES_DIR.mkdir(exist_ok=True)

# --- GUI Application (PySide6) ---------------------------------------------
APP_ENROL_FRAMES = 20
APP_FRAME_WIDTH = 640
APP_FRAME_HEIGHT = 480
APP_TIMER_INTERVAL_MS = 33  # For ~30 FPS

APP_HIBERNATE_INTERVAL_MS = 5000  # Check for wake-up every 5 seconds
APP_BLACK_FRAME_THRESHOLD = 5.0  # Avg pixel value below which frame is considered black
# Axon USB camera is on /dev/video1; index 0 often times out
APP_CAMERA_INDICES = [1, 0, 2]

# --- InspireFace ------------------------------------------------------------
INSPIREFACE_MODEL_NAME = "Pikachu"
# Pikachu modelpack default; matches official sample threshold
SIMILARITY_THRESHOLD = 0.48
# Gundam_RK3588 targets NPU but currently segfaults on Axon — do not use yet
# INSPIREFACE_MODEL_NAME = "Gundam_RK3588"

# FeatureHub persists embeddings in the same SQLite file as attendance metadata
DATABASE_PATH = DB_PATH
