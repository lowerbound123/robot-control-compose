"""Real terminal driver fixture; no robot, policy, Docker or network access."""
import json
from pathlib import Path
from queue import Queue
import sys
from threading import Event

from robot_control.log_tui import StructuredLog
from robot_control.tui import RobotControlApp


PAYLOAD = '{"state":"paused","runtime":{"phase":"INITIALIZING","target":"qwen35_single"}}'


class Controller:
    def __init__(self):
        self.events = Queue()
        self.events.put({"message": PAYLOAD})
        self.done = Event()
        self.exit_code = 0

    def start(self):
        pass

    def status(self):
        return {"state": "paused", "policy": None}

    def submit(self, line):
        if line in ("exit", "quit"):
            self.done.set()
        elif line == "status":
            self.events.put({"message": PAYLOAD})


class TerminalApp(RobotControlApp):
    def on_mount(self):
        super().on_mount()
        self.set_interval(.03, self.save_observation)

    def save_observation(self):
        log = self.query_one(StructuredLog)
        if not log.records:
            return
        record = next(iter(log.records.values()))
        region = log.scrollable_content_region
        # The host inspects observable UI state, then clicks its screen position.
        data = dict(x=region.x + 3, y=region.y, copy_x=region.right - 3,
                    width=region.width, ready=record.ready,
                    expanded=record.expanded, clipboard=self.clipboard)
        target = Path(sys.argv[1])
        pending = target.with_suffix(".tmp")
        pending.write_text(json.dumps(data))
        pending.replace(target)


if __name__ == "__main__":
    TerminalApp(Controller()).run()
