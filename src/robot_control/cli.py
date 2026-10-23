"""Command line entry point; the headless path has no terminal UI dependency."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from queue import Empty, Queue
import signal
import sys
import threading
import time

from .terminal_commands import TerminalCommandRouter


def make_controller(config_path):
    from .controller import RobotController

    return RobotController(config_path)


def format_event(event: dict) -> str:
    timestamp = event.get("time", "")
    prefix = f"[{timestamp}] " if timestamp else ""
    return f"{prefix}[{event.get('level', 'INFO')}] {event.get('message', '')}"


def drain_events(controller, stream) -> bool:
    """Drain one batch so a noisy policy cannot starve input and shutdown."""
    for _ in range(256):
        try:
            event = controller.events.get_nowait()
        except Empty:
            return False
        print(format_event(event), file=stream, flush=True)
    return not controller.events.empty()


@contextmanager
def shutdown_signals(controller):
    """Route process signals through the same bounded controller shutdown path."""
    previous = {}

    def request_exit(signum, frame):
        controller.submit("exit")

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_exit)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run_headless(controller, *, stdin=None, stdout=None, router=None) -> None:
    """Read complete lines without blocking progress or shutdown log output."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    router = router if router is not None else TerminalCommandRouter(controller)
    lines = Queue()

    def read_lines():
        try:
            for line in stdin:
                lines.put(line.rstrip("\r\n"))
        except (OSError, ValueError) as exc:
            lines.put(exc)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_lines, name="rc-stdin", daemon=True)
    reader.start()
    exit_requested = False
    try:
        while not controller.done.is_set():
            drain_events(controller, stdout)
            try:
                line = lines.get(timeout=0.05)
            except Empty:
                continue
            if line is None or isinstance(line, Exception):
                if isinstance(line, Exception):
                    print(f"[ERROR] stdin: {line}", file=stdout, flush=True)
                if not exit_requested:
                    controller.submit("exit")
                    exit_requested = True
            elif line.strip():
                try:
                    router.submit(line)
                except (ValueError, RuntimeError, PermissionError) as exc:
                    print(f"[ERROR] 命令失败：{exc}", file=stdout, flush=True)
                if line.strip() in ("exit", "quit"):
                    exit_requested = True
    finally:
        router.close()
    # A future may be done while its result callback is still emitting logs.
    while router.pending:
        drain_events(controller, stdout)
        time.sleep(.01)
    while drain_events(controller, stdout):
        pass


def run_tui(controller, *, router=None) -> None:
    from .graphics import probe_graphics
    from .tui_config import tui_settings
    from .tui import RobotControlApp

    settings = tui_settings(getattr(controller, "config", {}).get("tui"))
    graphics = probe_graphics(settings["camera"]["protocol"])
    RobotControlApp(controller, start_controller=False, router=router, graphics=graphics).run()


def make_mcp_service(controller, override=None):
    from .mcp_server import MCPService
    return MCPService(controller, getattr(controller, "config", {}).get("mcp"), override=override)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Robot control terminal")
    parser.add_argument("--config", default="rc.json", help="RC JSON config (default: rc.json)")
    parser.add_argument("--headless", action="store_true", help="read commands from stdin and log to stdout")
    parser.add_argument("--mcp", metavar="HOST:PORT", help="MCP listen override (default: 127.0.0.1:4000)")
    args = parser.parse_args(argv)
    try:
        controller = make_controller(args.config)
    except Exception as exc:
        print(f"robot-control: {exc}", file=sys.stderr)
        return 1

    failed = False
    started = False
    service = None
    router = None
    with shutdown_signals(controller):
        try:
            service = make_mcp_service(controller, args.mcp)
            if hasattr(controller, "config"):
                controller.config["mcp"] = service.settings
            service.bind()
            controller.start()
            started = True
            service.start()
            router = TerminalCommandRouter(controller, service)
            if hasattr(controller, "_emit"):
                controller._emit(f"MCP listening at {service.url}; 默认只读，take 后接管")
            if args.headless:
                run_headless(controller, router=router)
            else:
                run_tui(controller, router=router)
        except KeyboardInterrupt:
            controller.submit("exit")
        except Exception as exc:
            failed = True
            print(f"robot-control: {exc}", file=sys.stderr)
        finally:
            if router is not None:
                router.close()
            try:
                if not controller.done.is_set():
                    if started:
                        controller.submit("exit")
                    else:
                        controller.close_unstarted()
                controller.done.wait()
            finally:
                if service is not None:
                    try:
                        service.close()
                    except Exception as exc:
                        failed = True
                        print(f"robot-control MCP: {exc}", file=sys.stderr)
    return 1 if failed else controller.exit_code
