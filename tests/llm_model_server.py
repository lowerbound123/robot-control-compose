"""Isolated Compose test service. Never included in production overlays."""
import signal
import threading

from llm_http_helpers import model_server

stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())
signal.signal(signal.SIGINT, lambda *_: stop.set())
with model_server('0.0.0.0', 8080):
    stop.wait()
