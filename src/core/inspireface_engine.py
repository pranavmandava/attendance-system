"""InspireFace engine bootstrap.

Follows the official sample:
https://github.com/HyperInspire/InspireFace/blob/master/python/sample_face_recognition.py
"""

from __future__ import annotations

from pathlib import Path

import inspireface as isf

from src.config import DATABASE_PATH, INSPIREFACE_MODEL_NAME, SIMILARITY_THRESHOLD


def model_dir() -> Path:
    return Path.home() / ".inspireface" / "models" / INSPIREFACE_MODEL_NAME


def ensure_model() -> Path:
    """Return local model path, downloading on first use if missing."""
    path = model_dir()
    if not path.exists():
        isf.pull_latest_model(INSPIREFACE_MODEL_NAME)
    return path


def create_session(
    db_path: str | None = None,
    threshold: float | None = None,
) -> isf.InspireFaceSession:
    """Launch InspireFace and enable persistent FeatureHub."""
    path = ensure_model()
    if not isf.launch(resource_path=str(path)):
        raise RuntimeError(f"InspireFace launch failed for {path}")

    session = isf.InspireFaceSession(
        isf.HF_ENABLE_FACE_RECOGNITION,
        isf.HF_DETECT_MODE_ALWAYS_DETECT,
    )

    hub_config = isf.FeatureHubConfiguration(
        primary_key_mode=isf.HF_PK_AUTO_INCREMENT,
        enable_persistence=True,
        persistence_db_path=db_path or DATABASE_PATH,
        search_threshold=threshold if threshold is not None else SIMILARITY_THRESHOLD,
        search_mode=isf.HF_SEARCH_MODE_EAGER,
    )
    if not isf.feature_hub_enable(hub_config):
        raise RuntimeError("Failed to enable FeatureHub.")

    return session
