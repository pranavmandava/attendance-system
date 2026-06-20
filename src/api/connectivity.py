"""Connectivity test endpoints for LAN / static-IP PWA checks."""

from datetime import datetime, timezone

from flask import Blueprint

from src.utils import ist_timestamp

connectivity_bp = Blueprint("connectivity", __name__, url_prefix="/test/clock")


@connectivity_bp.route("/ist", methods=["GET"])
def clock_ist():
    return {
        "endpoint": "ist",
        "source": "flask",
        "timestamp": ist_timestamp(),
        "timezone": "Asia/Kolkata",
    }, 200


@connectivity_bp.route("/utc", methods=["GET"])
def clock_utc():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "endpoint": "utc",
        "source": "flask",
        "timestamp": now.isoformat(),
        "timezone": "UTC",
    }, 200
