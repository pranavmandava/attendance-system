"""Gunicorn WSGI config for the Axon Flask API.

Single worker is required: the IPC Unix socket, sync sweeper, and command
stream are process-global and must not be duplicated across workers.
"""

from src.config import AXON_DEBUG, AXON_HOST, AXON_PORT

bind = f"{AXON_HOST}:{AXON_PORT}"
workers = 1
threads = 4
worker_class = "gthread"
timeout = 120
accesslog = "-"
errorlog = "-"
capture_output = True
# Auto-reload on source changes when AXON_DEBUG is set (dev convenience).
reload = AXON_DEBUG


def post_worker_init(worker):
    from src.api.server import start_runtime_services

    start_runtime_services()


def worker_exit(server, worker):
    from src.api.server import stop_runtime_services

    stop_runtime_services()
