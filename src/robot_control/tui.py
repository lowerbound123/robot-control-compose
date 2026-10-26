"""Textual frontend. Robot and policy work belongs exclusively to the controller."""

from __future__ import annotations

from os.path import commonprefix
from queue import Empty
import re
import time

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static

from .log_tui import StructuredLog
from .terminal_commands import TerminalCommandRouter
from .graphics import GraphicsSupport, prepare_frame
from .preview import LivePreview
from .tui_config import tui_settings
from .tui_panels import CameraPanel, MonitorPanel
from .policy_monitor_tui import PolicyMonitorSidebar
from .policy_manager_tui import PolicyManagerPanel

from rc_policy_sdk.commands import BUILTIN_COMMANDS as COMMANDS


class CommandInput(Input):
    """History keeps complete command lines, including JSON quotes and spaces."""

    BINDINGS = [
        Binding("up", "history_previous", show=False),
        Binding("down", "history_next", show=False),
        Binding("tab", "complete_command", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.commands = COMMANDS
        self.mcp_tools = ()
        self.history: list[str] = []
        self.history_index = 0
        self.draft = ""

    def paste_text(self, text: str) -> None:
        text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        start, end = self.selection
        self.replace(text, min(start, end), max(start, end))

    def _on_paste(self, event: events.Paste) -> None:
        event.stop().prevent_default()
        self.paste_text(event.text)

    def remember(self, command: str) -> None:
        if not self.history or self.history[-1] != command:
            self.history.append(command)
        self.history_index = len(self.history)
        self.draft = ""

    def _replace(self, value: str) -> None:
        self.value = value
        self.cursor_position = len(value)

    def action_history_previous(self) -> None:
        if self.history_index == len(self.history):
            self.draft = self.value
        if self.history_index > 0:
            self.history_index -= 1
            self._replace(self.history[self.history_index])

    def action_history_next(self) -> None:
        if self.history_index < len(self.history):
            self.history_index += 1
            self._replace(self.draft if self.history_index == len(self.history)
                          else self.history[self.history_index])

    def action_complete_command(self) -> None:
        # Only complete command/tool names, never the argument tail.
        prefix = self.value
        head = ""
        candidates = self.commands
        tool = re.fullmatch(r"(mcp\s+)(\S*)", prefix)
        if tool:
            head, prefix = tool.groups()
            candidates = self.mcp_tools
            if not prefix:
                return
        if not prefix or any(char.isspace() for char in prefix):
            return
        matches = [command for command in candidates if command.startswith(prefix)]
        if len(matches) == 1:
            self._replace(head + matches[0] + " ")
        elif matches:
            self._replace(head + commonprefix(matches))


class RobotControlApp(App):
    TITLE = "Robot Control"
    BINDINGS = [
        Binding("ctrl+c", "safe_exit", "Exit", priority=True),
        Binding("ctrl+q", "safe_exit", "Exit", priority=True),
        Binding("ctrl+shift+c", "copy_log_selection", "Copy logs", priority=True),
    ]
    CSS = """
    Screen { layout: vertical; }
    #log-area { height: 1fr; min-height: 2; }
    #output { width: 2fr; min-width: 0; height: 1fr; min-height: 2; border: solid $accent; padding: 0 1; }
    #status { height: auto; min-height: 1; padding: 0 1; background: $panel; }
    #command-area { dock: bottom; height: auto; max-height: 100%; background: $surface; }
    #command { height: 3; }
    """

    def __init__(self, controller, *, start_controller=True, router=None, graphics=None):
        super().__init__()
        self.controller = controller
        self.router = router if router is not None else TerminalCommandRouter(controller)
        self.start_controller = start_controller
        self.exit_requested = False
        self.settings = tui_settings(getattr(controller, "config", {}).get("tui"))
        self.graphics = graphics if graphics is not None else GraphicsSupport(
            self.settings["camera"]["protocol"], error="终端图像能力尚未初始化，请通过 robot-control 启动")
        self.preview = LivePreview(self._camera_source, self._monitor_source, prepare_frame, self.graphics.protocol)
        self.router.panel_handler = self._set_panel
        self.router.policy_manager_handler = self._manage_panel
        self._editor_pending = None
        self._ui_active = False
        self._refresh_timers = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="log-area"):
            yield StructuredLog(id="output", max_records=10000)
            yield PolicyMonitorSidebar()
        yield MonitorPanel(self.settings["monitor"])
        yield CameraPanel(self.graphics, self._write_graphics, self._camera_failed)
        with Vertical(id='command-area'):
            yield PolicyManagerPanel()
            yield Static("Starting…", id="status")
            yield CommandInput(id="command", placeholder="help · ↑/↓ history · Tab complete · Ctrl+C exit")

    def on_mount(self) -> None:
        self._ui_active = True
        if self.start_controller:
            self.controller.start()
        self.query_one(CommandInput).focus()
        self._refresh_timers.append(self.set_interval(0.05, self.refresh_controller))
        self._refresh_timers.append(self.set_interval(1 / 30, self.refresh_preview))
        self.refresh_controller()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        line = event.value
        command_input = self.query_one(CommandInput)
        command_input.value = ""
        if not line.strip():
            return
        command_input.remember(line)
        self.query_one(StructuredLog).write(Text(f"> {line}", style="bold cyan"))
        try:
            self.router.submit(line)
        except (ValueError, RuntimeError, PermissionError) as exc:
            self.query_one(StructuredLog).write(Text(f"命令失败：{exc}", style="red"))
            return
        if line.strip() in ("exit", "quit"):
            self.exit_requested = True
            self._stop_preview()

    def action_copy_log_selection(self) -> None:
        self.query_one(StructuredLog).copy_selection()

    def on_structured_log_paste_to_command(self, event: StructuredLog.PasteToCommand) -> None:
        event.stop()
        command = self.query_one(CommandInput)
        command.paste_text(event.text)
        command.focus()

    def action_safe_exit(self) -> None:
        if not self.exit_requested:
            self.exit_requested = True
            self._stop_preview()
            self.controller.submit("exit")

    async def action_quit(self) -> None:
        # Also cover programmatic use of Textual's inherited quit action.
        self.action_safe_exit()

    def on_unmount(self) -> None:
        self._ui_active = False
        for timer in self._refresh_timers:
            timer.stop()
        self._stop_preview()
        self.router.panel_handler = None
        self.router.policy_manager_handler = None
        self.router.close()

    def _camera_source(self):
        return self.controller.robot.camera_preview_snapshot()

    def _manage_panel(self, action):
        panel = self.query_one(PolicyManagerPanel)
        if action == 'close':
            if panel.token is not None:
                try:
                    self.controller.manage_policy('close', token=panel.token)
                except (ValueError, RuntimeError, PermissionError):
                    pass  # A stale editor can always be dismissed.
            panel.display = False
            self.query_one(CommandInput).focus()
            return
        if self._editor_pending is not None:
            raise RuntimeError('链操作正在进行')
        if action != 'open' and (not panel.display or panel.token is None):
            raise ValueError('请先运行 manage-policy')
        if action != 'open' and not panel.policies:
            raise ValueError('空草稿不能应用或保存，请添加 policy')
        order = list(panel.policies)
        identity = self.controller.manage_policy(action, order if action != 'open' else None, token=panel.token)
        self._editor_pending = (identity, action, order)
        panel.display = True
        panel.disabled = True
        panel.refresh_note({'open': '正在暂停并读取链配置…', 'apply': '正在校验并加载整条链…', 'save': '正在保存草稿…'}[action])

    def on_policy_manager_action(self, event):
        event.stop()
        try:
            self._manage_panel(event.action)
        except (ValueError, RuntimeError, PermissionError) as exc:
            panel = self.query_one(PolicyManagerPanel)
            panel.error = str(exc)
            panel.refresh_note()
            self.query_one(StructuredLog).write(Text(f'链操作失败：{exc}', style='red'))

    def _refresh_editor(self):
        if self._editor_pending is None:
            return
        identity, action, order = self._editor_pending
        receipt = self.controller.get_operation(identity)
        if receipt['status'] not in ('succeeded', 'failed', 'cancelled'):
            return
        self._editor_pending = None
        panel = self.query_one(PolicyManagerPanel)
        panel.disabled = False
        if receipt['status'] != 'succeeded':
            panel.error = receipt.get('error', '链操作失败')
            self.query_one(StructuredLog).write(Text(panel.error, style='red'))
        elif action == 'open':
            panel.load_snapshot(receipt['result'])
        elif action == 'apply':
            panel.applied = order
            panel.error = ''
        else:
            panel.saved = order
            panel.error = ''
        panel.refresh_note()
        if panel.display:
            panel.query_one('#chain-cells').focus()

    def _monitor_source(self):
        return self.controller.monitor_snapshot()

    def _write_graphics(self, text):
        if self._driver is not None:
            self._driver.write(text)

    def _set_panel(self, name, desired):
        if name.startswith("monitor-"):
            monitors = getattr(self.controller, "policy_monitors", None)
            if monitors is None:
                raise ValueError("当前控制器不支持 policy monitor")
            enabled = monitors.set_enabled(name[len("monitor-"):], desired)
            self.query_one(PolicyMonitorSidebar).update_snapshots(monitors.snapshot())
            return enabled
        panel = self.query_one(CameraPanel if name == "camera" else MonitorPanel)
        enabled = not panel.display if desired is None else desired
        if name == "camera" and enabled and self.graphics.error:
            raise ValueError(self.graphics.error)
        if self.preview.closed:
            raise RuntimeError("TUI 正在关闭")
        if panel.display == enabled:
            return
        self.preview.set_enabled(name, enabled)
        panel.display = enabled
        if name == "camera" and not enabled:
            panel.clear_images()
        self.screen.refresh(layout=True)

    def _camera_failed(self, error):
        if self.preview.closed:
            return
        self.preview.set_enabled("camera", False)
        panel = self.query_one(CameraPanel)
        panel.display = False
        try:
            panel.clear_images()
        except Exception:
            pass  # a broken terminal writer must not take down control/exit
        self.query_one(StructuredLog).write(Text(f"相机预览已关闭：{error}", style="red"))
        self.screen.refresh(layout=True)

    def refresh_preview(self):
        if not self._ui_active or not self.is_running or self.preview.closed:
            return
        camera = self.query_one(CameraPanel)
        result = self.preview.poll(camera.sizes() if camera.display else {})
        if result["render_error"]:
            self._camera_failed(result["render_error"])
        elif camera.display:
            camera.update_frames(result["frames"])
            camera.update_status(self.preview, time.monotonic())
        if result["monitor"] is not None:
            self.query_one(MonitorPanel).update_snapshot(result["monitor"])

    def _stop_preview(self):
        monitors = getattr(self.controller, "policy_monitors", None)
        if monitors is not None:
            monitors.close(wait=False)
        self.preview.close()
        for panel in self.query(CameraPanel):
            try:
                panel.clear_images()
            except Exception:
                pass

    def refresh_controller(self) -> None:
        if not self._ui_active or not self.is_running:
            return
        self._refresh_editor()
        if self.controller.done.is_set():
            self._stop_preview()
            self.router.close()
        log = self.query_one(StructuredLog)
        events = []
        for _ in range(256):
            try:
                event = self.controller.events.get_nowait()
            except Empty:
                break
            events.append(event)
        log.write_events(events)
        monitors = getattr(self.controller, "policy_monitors", None)
        monitor_commands = []
        if monitors is not None:
            monitor_commands = ["monitor-" + identity for identity in monitors.available()]
            self.query_one(PolicyMonitorSidebar).update_snapshots(monitors.snapshot())
        self.query_one(CommandInput).commands = (*COMMANDS, *getattr(self.controller, "local_commands", lambda: [])(), *monitor_commands)
        self.query_one(CommandInput).mcp_tools = self.router.tool_names
        status = self.controller.status()
        fields = [f"{key}={value}" for key, value in status.items() if key != "direct"]
        if "direct" in status:
            fields.extend([f"direct={status['direct']['phase']}", f"direct_pending={status['direct']['pending_steps']}"])
        self.query_one("#status", Static).update(Text(" | ".join(fields)))
        if self.controller.done.is_set() and self.controller.events.empty() and not self.router.pending:
            self._ui_active = False
            for timer in self._refresh_timers:
                timer.stop()
            self.exit(return_code=self.controller.exit_code)
