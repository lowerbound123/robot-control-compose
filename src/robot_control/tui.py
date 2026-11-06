"""Textual frontend. Robot and policy work belongs exclusively to the controller."""

from __future__ import annotations

from .secret_config import public_value, reject_secret_edit

from queue import Empty
import asyncio
import time

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.suggester import Suggester
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

from .completion import apply_completion
from .completion_service import CompletionService
from .completion_panel import CompletionPanel
from .history import CommandHistory, recent_unique, valid_history_line


class HistorySuggestionReady(Message):
    def __init__(self, value, suggestion, revision, edit_revision):
        super().__init__()
        self.value, self.suggestion = value, suggestion
        self.revision, self.edit_revision = revision, edit_revision


class HistorySuggester(Suggester):
    def __init__(self, field):
        super().__init__(use_cache=False, case_sensitive=True)
        self.field = field

    async def get_suggestion(self, value):
        if not value:
            return None
        return next((line for line in reversed(self.field.history)
                     if line != value and line.startswith(value)), None)

    async def _get_suggestion(self, requester, value):
        revision, edit_revision = self.field.history_revision, self.field._ghost_epoch
        suggestion = await self.get_suggestion(value)
        if suggestion is not None:
            requester.post_message(HistorySuggestionReady(value, suggestion, revision, edit_revision))


class CommandInput(Input):
    """Native input editing with explicit completion confirmation and durable history."""

    BINDINGS = [
        Binding('up', 'history_previous', show=False),
        Binding('down', 'history_next', show=False),
        Binding('tab', 'complete_command', show=False),
        Binding('shift+tab', 'complete_previous', show=False),
        Binding('escape', 'dismiss_assistance', show=False),
    ]

    def __init__(self, **kwargs):
        self.history = []
        self.history_revision = 0
        self.history_index = 0
        self.draft = ''
        self.menu_open = False
        self.selected_completion = 0
        self.completion_result = None
        self.completion_service = None
        self.history_store = None
        self._history_store_revision = -1
        self._ghost_epoch = 0
        self._ghost_hidden_value = None
        super().__init__(suggester=HistorySuggester(self), **kwargs)

    def attach_assistance(self, service, history=None):
        self.completion_service, self.history_store = service, history
        self._request_completion()
        self.poll_assistance()

    def _panel(self):
        if self.is_mounted:
            return self.app.query_one(CompletionPanel)
        return None

    def _show_assistance(self):
        panel = self._panel()
        if panel is not None:
            panel.present(self.completion_result, self.selected_completion, self.menu_open)

    def _request_completion(self):
        if self.completion_service is None:
            return
        self._accept_result(self.completion_service.request(self.value, self.cursor_position))

    def _accept_result(self, result):
        if result is None or result.text != self.value or result.cursor != self.cursor_position:
            return
        current = self.completion_result
        if current is not None and current.text == result.text and current.cursor == result.cursor and result.revision < current.revision:
            return
        previous = None
        if current is not None and current.items and self.selected_completion < len(current.items):
            previous = current.items[self.selected_completion]
        self.completion_result = result
        self.selected_completion = next((index for index, item in enumerate(result.items) if item == previous), 0)
        self._show_assistance()

    def poll_assistance(self):
        if self.completion_service is not None:
            self._accept_result(self.completion_service.poll())
        if self.history_store is not None:
            lines, revision = self.history_store.snapshot()
            if revision != self._history_store_revision:
                browsing = self.history_index < len(self.history)
                old = self.history[self.history_index] if browsing else None
                self.history = list(lines)
                self.history_index = self.history.index(old) if old in self.history else len(self.history)
                self._history_store_revision = revision
                self.history_revision += 1
                self._refresh_ghost()

    def _watch_value(self, value):
        self._ghost_epoch += 1
        self._ghost_hidden_value = None
        super()._watch_value(value)
        self.selected_completion = 0
        self._request_completion()

    def _watch_selection(self, selection):
        super()._watch_selection(selection)
        self._refresh_ghost()
        self._request_completion()

    def _ghost_allowed(self):
        return (bool(self.value) and not self.menu_open and self.selection.is_empty
                and self.cursor_at_end and self._ghost_hidden_value != self.value)

    def _refresh_ghost(self):
        self._ghost_epoch += 1
        self._suggestion = ''
        if self.is_mounted and self._ghost_allowed():
            self.run_worker(self.suggester._get_suggestion(self, self.value))

    async def _on_suggestion_ready(self, event):
        # Only tagged, current-history replies may paint the native ghost.
        event.stop().prevent_default()

    def on_history_suggestion_ready(self, event):
        event.stop()
        if (self._ghost_allowed() and event.value == self.value
                and event.revision == self.history_revision and event.edit_revision == self._ghost_epoch
                and event.suggestion != self.value and event.suggestion.startswith(self.value)):
            self._suggestion = event.suggestion

    def paste_text(self, text):
        text = text.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
        start, end = self.selection
        self.replace(text, min(start, end), max(start, end))

    def _on_paste(self, event):
        event.stop().prevent_default()
        self.paste_text(event.text)

    def remember(self, command):
        valid_history_line(command)
        self.history = recent_unique([*self.history, command])
        self.history_revision += 1
        self.history_index = len(self.history)
        self.draft = ''
        if self.history_store is not None:
            self.history_store.record(command)
        self._refresh_ghost()

    def _replace(self, value):
        self.value = value
        self.cursor_position = len(value)

    def _move_completion(self, direction):
        result, panel = self.completion_result, self._panel()
        if result is not None and panel is not None:
            self.selected_completion = panel.move(self.selected_completion, len(result.items), direction)
            self._show_assistance()

    def action_history_previous(self):
        if self.menu_open:
            self._move_completion('up')
            return
        if self.history_index == len(self.history):
            self.draft = self.value
        if self.history_index > 0:
            self.history_index -= 1
            self._replace(self.history[self.history_index])

    def action_history_next(self):
        if self.menu_open:
            self._move_completion('down')
            return
        if self.history_index < len(self.history):
            self.history_index += 1
            self._replace(self.draft if self.history_index == len(self.history) else self.history[self.history_index])

    def action_complete_command(self):
        if self.menu_open:
            self._move_completion('next')
        else:
            self.menu_open = True
            self._refresh_ghost()
            self._request_completion()
            self._show_assistance()

    def action_complete_previous(self):
        if not self.menu_open:
            self.action_complete_command()
        else:
            self._move_completion('previous')

    def close_menu(self):
        self.menu_open = False
        self._show_assistance()
        self._refresh_ghost()

    def action_dismiss_assistance(self):
        self.close_menu()
        self._ghost_hidden_value = self.value
        self._ghost_epoch += 1
        self._suggestion = ''

    def _confirm_completion(self):
        self.poll_assistance()
        result = self.completion_result
        self.close_menu()
        if result is None or result.text != self.value or result.cursor != self.cursor_position or not result.items:
            return False
        value, cursor = apply_completion(self.value, self.cursor_position, result, result.items[self.selected_completion])
        self.value, self.cursor_position = value, cursor
        return True

    async def action_submit(self):
        if self.menu_open:
            self._confirm_completion()
            return
        await super().action_submit()

    async def _on_key(self, event):
        if self.menu_open and event.character == ' ':
            event.stop().prevent_default()
            if not self._confirm_completion():
                self.insert_text_at_cursor(' ')
            return
        # Native non-printable bindings normally bubble back to the App queue.
        # Resolve them here, in the same queue as printable keys and Paste, so a
        # rapid Ctrl+U/type/Tab sequence cannot delete the newly typed text.
        if not event.is_printable and await self.app._check_bindings(event.key):
            event.stop().prevent_default()
            return
        await super()._on_key(event)

    def action_cursor_left(self, select=False):
        if self.menu_open and not select:
            self._move_completion('left')
            return
        if select:
            self.close_menu()
        super().action_cursor_left(select)

    def action_cursor_right(self, select=False):
        self.poll_assistance()
        if self.menu_open and not select:
            self._move_completion('right')
            return
        if select:
            self.close_menu()
        if not self._ghost_allowed():
            self._suggestion = ''
        super().action_cursor_right(select)

    def action_home(self, select=False):
        self.close_menu()
        super().action_home(select)

    def action_end(self, select=False):
        self.close_menu()
        super().action_end(select)

    async def _on_mouse_down(self, event):
        self.close_menu()
        await super()._on_mouse_down(event)


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
        self.preview = LivePreview(self._camera_source, self._monitor_source, prepare_frame, self.graphics.protocol,
                                   source_generation=lambda: getattr(getattr(controller, 'robot', None), 'generation', 0))
        self.router.panel_handler = self._set_panel
        self.router.policy_manager_handler = self._manage_panel
        self._editor_pending = None
        self._ui_active = False
        self._refresh_timers = []
        self.completion_service = CompletionService(controller, tools=tuple(self.router.service.tools) if self.router.service is not None else ())
        config_path = getattr(controller, "config_path", None)
        outputs = getattr(controller, "outputs", None)
        self.command_history = CommandHistory(config_path, outputs) if config_path is not None and outputs is not None else None

    def compose(self) -> ComposeResult:
        with Horizontal(id="log-area"):
            yield StructuredLog(id="output", max_records=10000)
            yield PolicyMonitorSidebar()
        yield MonitorPanel(self.settings["monitor"])
        yield CameraPanel(self.graphics, self._write_graphics, self._camera_failed,
                          height_limit=self._camera_height_limit)
        with Vertical(id='command-area'):
            yield PolicyManagerPanel()
            yield Static("Starting…", id="status")
            yield CompletionPanel()
            yield CommandInput(id="command", placeholder="help · ↑/↓ history · Tab candidates · → history · Ctrl+C exit")

    def on_mount(self) -> None:
        self._ui_active = True
        if self.start_controller:
            self.controller.start()
        self.query_one(CommandInput).attach_assistance(self.completion_service, self.command_history)
        self.query_one(CommandInput).focus()
        self._refresh_timers.append(self.set_interval(0.05, self.refresh_controller))
        self._refresh_timers.append(self.set_interval(1 / 30, self.refresh_preview))
        self.refresh_controller()

    def _restore_terminal_input(self) -> None:
        """A late Docker attach never sees the driver's startup escape codes."""
        if not self._ui_active or self.exit_requested:
            return
        driver = self._driver
        # Textual 8.2's terminal driver owns the modes and honors mouse=False.
        # Headless/web drivers have no such hooks and need no terminal writes.
        modes = ["_enable_mouse_support"]
        if getattr(driver, "_mouse_pixels", False):
            # 1006h selects cell coordinates in terminals such as Ghostty.
            # Keep the negotiated 1016 pixel encoding aligned with the parser.
            modes.append("_enable_mouse_pixels")
        modes.append("_enable_bracketed_paste")
        for name in modes:
            enable = getattr(driver, name, None)
            if enable is not None:
                enable()
        flush = getattr(driver, "flush", None)
        if flush is not None:
            flush()

    def on_resize(self, event: events.Resize) -> None:
        self._restore_terminal_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        # A same-size reattach may not generate a resize; even an empty Enter
        # restores input without submitting any controller/lifecycle operation.
        self._restore_terminal_input()
        line = event.value
        command_input = self.query_one(CommandInput)
        command_input.value = ""
        if not line.strip():
            return
        try:
            command_input.remember(line)
        except ValueError:
            # Invalid/secret text is never echoed or persisted. The router still
            # receives every real nonempty submission so it revokes queued batches.
            pass
        else:
            self.query_one(StructuredLog).write(Text(f"> {line}", style="bold cyan"))
        try:
            self.router.submit(line)
        except (ValueError, RuntimeError, PermissionError) as exc:
            self.query_one(StructuredLog).write(Text(f"命令失败：{exc}", style="red"))
            return
        if line.strip() == "exit":
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

    async def on_unmount(self) -> None:
        self._ui_active = False
        for timer in self._refresh_timers:
            timer.stop()
        self._stop_preview()
        self.completion_service.close()
        if self.command_history is not None:
            self.command_history.close()
        self.router.panel_handler = None
        self.router.policy_manager_handler = None
        self.router.close()
        if self.command_history is not None:
            deadline = getattr(self.controller, '_shutdown_deadline', None)
            budget = .5 if deadline is None else min(.5, max(0., deadline - time.monotonic()))
            if budget:
                await asyncio.to_thread(self.command_history.wait_idle, budget)

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
            raise ValueError('请先运行 policy manage')
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

    def _camera_height_limit(self):
        monitor = self.query_one(MonitorPanel)
        fixed = self.query_one('#command-area').outer_size.height
        if monitor.display:
            fixed += monitor.outer_size.height
        return max(4, self.screen.content_size.height - fixed - 2)

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
        self.controller._emit(f"相机预览已关闭：{error}", 'error')
        self.screen.refresh(layout=True)

    def refresh_preview(self):
        if not self._ui_active or not self.is_running or self.preview.closed:
            return
        camera = self.query_one(CameraPanel)
        camera.constrain_height()
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
        self.query_one(CommandInput).poll_assistance()
        if self.command_history is not None:
            warning = self.command_history.take_warning()
            if warning:
                self.query_one(StructuredLog).write(Text(warning, style="yellow"))
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
        if monitors is not None:
            self.query_one(PolicyMonitorSidebar).update_snapshots(monitors.snapshot())
        status = self.controller.status()
        fields = [f"{key}={value}" for key, value in status.items() if key not in ("direct", "runtime", "last_action_request")]
        last_request = status.get('last_action_request')
        if last_request is not None:
            fields.append(f"请求 {last_request['request_id']}：裁剪 {last_request['trim']}，缓存 {last_request['cached']}")
        runtime = status.get('runtime')
        if runtime:
            fields.extend([f"runtime={runtime['phase']}", f"target={runtime['target']}"])
            if runtime.get('error'):
                fields.append(f"runtime_error={runtime['error']}")
        if "direct" in status:
            fields.extend([f"direct={status['direct']['phase']}", f"direct_pending={status['direct']['pending_steps']}"])
        self.query_one("#status", Static).update(Text(" | ".join(fields)))
        if self.controller.done.is_set() and self.controller.events.empty() and not self.router.pending:
            self._ui_active = False
            for timer in self._refresh_timers:
                timer.stop()
            self.exit(return_code=self.controller.exit_code)
