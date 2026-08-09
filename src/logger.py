import logging
from logging.handlers import RotatingFileHandler

from src.config import DATA_DIR

_LOG_DIR = DATA_DIR / "logs"
_LOGGING_READY = False


def setup_logging():
    """
    Sets up logging configuration with rotating file handlers for different components.

    Creates two separate log files under data/logs/:
    - core_ui.log: For core application and UI components
    - api.log: For Flask API server components

    Both use rotating file handlers to manage log file sizes.
    """
    global _LOGGING_READY
    if _LOGGING_READY:
        return

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Create formatters
    detailed_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    simple_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Clear any existing handlers
    root_logger.handlers.clear()

    # Console handler for all logs
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(simple_formatter)
    root_logger.addHandler(console_handler)

    # Core/UI logger - for main application, face recognition, UI components
    core_ui_logger = logging.getLogger("core_ui")
    core_ui_logger.setLevel(logging.DEBUG)
    core_ui_logger.handlers.clear()

    # Rotating file handler for core/UI logs (max 10MB, keep 5 backup files)
    core_ui_handler = RotatingFileHandler(
        _LOG_DIR / "core_ui.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        mode="a",
    )
    core_ui_handler.setLevel(logging.DEBUG)
    core_ui_handler.setFormatter(detailed_formatter)
    core_ui_logger.addHandler(core_ui_handler)

    # API logger - for Flask server, API endpoints, server-related logs
    api_logger = logging.getLogger("api")
    api_logger.setLevel(logging.DEBUG)
    api_logger.handlers.clear()

    # Rotating file handler for API logs (max 10MB, keep 5 backup files)
    api_handler = RotatingFileHandler(
        _LOG_DIR / "api.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        mode="a",
    )
    api_handler.setLevel(logging.DEBUG)
    api_handler.setFormatter(detailed_formatter)
    api_logger.addHandler(api_handler)

    # Prevent propagation to root logger to avoid duplicate logs
    core_ui_logger.propagate = False
    api_logger.propagate = False
    _LOGGING_READY = True


def get_core_ui_logger(name=None):
    """
    Get a logger for core/UI components.

    Args:
        name: Optional logger name (will be prefixed with 'core_ui.')

    Returns:
        Logger instance for core/UI logging
    """
    if name:
        return logging.getLogger(f"core_ui.{name}")
    return logging.getLogger("core_ui")


def get_api_logger(name=None):
    """
    Get a logger for API components.

    Args:
        name: Optional logger name (will be prefixed with 'api.')

    Returns:
        Logger instance for API logging
    """
    if name:
        return logging.getLogger(f"api.{name}")
    return logging.getLogger("api")
