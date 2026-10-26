"""Display-only policy snapshots beside the log; no worker calls here."""
import time

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static


def literal(value):
    """Keep Unicode/newlines, display terminal control bytes as literal escapes."""
    return "".join(char if char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159)
                   else f"\\x{ord(char):02x}" for char in str(value))


class PolicyMonitorPanel(Static):
    DEFAULT_CSS = """
    PolicyMonitorPanel { height: auto; width: 1fr; min-width: 0;
                         border: solid $accent; padding: 0 1; }
    """

    def __init__(self, identity):
        super().__init__("", markup=False)
        self.policy_identity = identity
        self.border_title = f"monitor · {identity}"
        self._last_text = None

    def update_snapshot(self, snapshot):
        values = snapshot["values"]
        rows = []
        if snapshot["error"]:
            rows.append(literal(snapshot["error"]))
        elif snapshot["status"] == "waiting":
            rows.append("等待数据")
        if values is not None:
            rows.extend(f"{literal(key)}: {literal(value)}" for key, value in values.items())
            if not values:
                rows.append("暂无信息")
        if snapshot["updated_at"] is not None:
            age = max(0., time.monotonic() - snapshot["updated_at"])
            rows.append(f"更新于 {age:.1f} 秒前")
        content = "\n".join(rows)
        if content != self._last_text:
            self._last_text = content
            self.update(Text(content, overflow="fold", no_wrap=False))


class PolicyMonitorSidebar(VerticalScroll):
    DEFAULT_CSS = """
    PolicyMonitorSidebar { display: none; width: 1fr; min-width: 0; height: 1fr;
                           overflow-x: hidden; overflow-y: auto; }
    """

    def __init__(self):
        super().__init__(id="policy-monitors")
        self.panels = {}

    def update_snapshots(self, snapshots):
        for identity in list(self.panels):
            if identity not in snapshots:
                self.panels.pop(identity).remove()
        for identity, snapshot in snapshots.items():
            if identity not in self.panels:
                panel = PolicyMonitorPanel(identity)
                self.panels[identity] = panel
                self.mount(panel)
            self.panels[identity].update_snapshot(snapshot)
        self.display = bool(snapshots)
