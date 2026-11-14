"""Local terminal routing without UI, network, or MCP SDK imports."""
from __future__ import annotations

from .secret_config import reject_secret_edit
from rc_policy_sdk.command_syntax import parse_command
from .help_document import help_text
from .mcp_help import mcp_help_document

from concurrent.futures import CancelledError, ThreadPoolExecutor
import json
from pathlib import Path
import threading


MCP_HELP = "\n".join([
    "mcp <tool> [JSON对象]；mcp help [工具名] 查看工具与 JSON 字段帮助。",
    "先 mcp take，确认 succeeded 后发动作，最后 mcp return；不自动接管。",
    '示例：mcp send_command {"commands":["left:open","right:close"],"wait":true}',
    "调用在后台执行；wait=true 不阻塞终端，等待超时不取消动作；图像只显示元数据。",
])
PANEL_HELP = "camera [show|hide|toggle]：省略参数切换相机面板；monitor [实例]：切换 TUI 监控面板，启动时关闭。"
POLICY_MONITOR_HELP = "monitor <实例>：切换已加载 policy 的监控面板；仅支持 TUI。"
MANAGE_HELP = 'policy manage [apply|save|close]：本地 TUI 链编排；进入时暂停，apply 后暂停，save 只保存草稿。'
SNAPSHOT_HELP = "snapshot：后台保存三路原始相机 PNG 和完整 state 到当前工作目录 snapshot/<时间戳>/；支持本地 TUI/headless。"


class TerminalCommandRouter:
    """Share local MCP routing, help, and result logging between TUI/headless."""

    def __init__(self, controller, service=None):
        self.controller = controller
        self.service = service
        self._lock = threading.Lock()
        self._pending = set()
        self._shutdown_calls = set()
        self._closed = False
        self._snapshot_executor = None
        self._snapshot_job = None
        self.panel_handler = None
        self.policy_manager_handler = None

    @property
    def pending(self):
        with self._lock:
            return bool(self._pending)

    def help(self, tool_name=None):
        return help_text(self.help_document(tool_name))

    def help_document(self, tool_name=None):
        if self.service is None:
            value = mcp_help_document(())
            value['sections'].append({'title': '服务状态', 'body': 'MCP 服务未启动。'})
            return value
        return mcp_help_document(self.service.tools, tool_name)

    def submit(self, line: str):
        if line.strip() and hasattr(self.controller, 'interrupt_tui_batches'):
            self.controller.interrupt_tui_batches()
        reject_secret_edit(line)
        parsed = parse_command(line)
        if parsed.kind == 'snapshot':
            return self._submit_snapshot()
        if parsed.kind == 'policy_manage':
            if self.policy_manager_handler is None:
                raise ValueError('policy manage 仅支持本地 TUI')
            return self.policy_manager_handler(parsed.args[0] if parsed.args else 'open')
        if parsed.kind in ('camera', 'monitor'):
            if self.panel_handler is None:
                raise ValueError(f'{parsed.kind} 仅支持本地 TUI')
            if parsed.kind == 'camera':
                return self.panel_handler('camera', {'show': True, 'hide': False, 'toggle': None}[parsed.args[0]])
            name = 'monitor-' + parsed.target if parsed.target else 'monitor'
            return self.panel_handler(name, None)
        if parsed.kind == 'help' and parsed.args and parsed.args[0] in ('camera', 'monitor', 'snapshot'):
            from .help_content import builtin_help
            value = builtin_help(parsed.args)
            self.controller._emit_command(help_text(value), help_document=value)
            return
        if parsed.kind == 'mcp_help':
            value = self.help_document(parsed.target)
            self.controller._emit_command(help_text(value), help_document=value)
            return
        if parsed.kind != 'mcp_tool':
            return self.controller.submit(line)
        name, arguments = parsed.target, parsed.args[0]
        with self._lock:
            if self._closed or self.service is None:
                raise RuntimeError("MCP 服务未启动或正在关闭")
            future = self.service.submit_tool(name, arguments)
            self._pending.add(future)
            shutdown_line = arguments.get('line')
            if name == 'command' and isinstance(shutdown_line, str) and shutdown_line.strip() == 'exit':
                self._shutdown_calls.add(future)
        # Register outside the lock: an already-completed future calls back inline.
        future.add_done_callback(lambda completed: self._report(name, completed))
        return future

    def _submit_snapshot(self):
        from .snapshots import save_snapshot

        with self._lock:
            if self._closed:
                raise RuntimeError("snapshot 不可用：终端正在关闭")
            if self._snapshot_job is not None:
                raise RuntimeError("snapshot 正在保存，请等待完成")
            # Capture before accepting a subsequent exit command. Only cached
            # reads occur here; encoding and disk access run without control locks.
            payload, frames = self.controller.capture_snapshot()
            if self._snapshot_executor is None:
                self._snapshot_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rc-snapshot")
            future = self._snapshot_executor.submit(save_snapshot, payload, frames, Path.cwd() / "snapshot")
            self._snapshot_job = future
            self._pending.add(future)
        future.add_done_callback(self._report_snapshot)
        return future

    def _report_snapshot(self, future):
        try:
            path = future.result()
            self.controller._emit_command(f"snapshot 已保存：{path}")
        except Exception as exc:
            self.controller._emit_command(f"snapshot 保存失败：{exc}", "error")
        finally:
            with self._lock:
                self._pending.discard(future)
                self._snapshot_job = None

    def _report(self, name, future):
        # Query responses must not become their own input on the next events
        # call. They remain visible in the terminal and the on-disk log.
        log_options = {"record_history": False} if name == "events" else {}
        try:
            response = future.result()
            payload = response.structured_content
            help_document = payload.get('help_document') if name == 'help' and isinstance(payload, dict) else None
            if help_document is None and name == 'command' and isinstance(payload, dict) and isinstance(payload.get('result'), dict):
                try:
                    if parse_command(payload.get('command', '')).kind in ('help', 'policy_help'):
                        help_document = payload['result'].get('help_document')
                except ValueError:
                    pass
            if help_document is not None and not response.is_error:
                self.controller._emit_command(help_text(help_document), help_document=help_document, **log_options)
                return
            if payload is not None:
                text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            else:
                text = "\n".join(block.text for block in response.content if block.type == "text")
            images = sum(block.type == "image" for block in response.content)
            if images:
                text += f"\n图像内容未显示（{images} 张）；请查看 images 元数据。"
            self.controller._emit_command(f"MCP {name}: {text}", "error" if response.is_error else "info", **log_options)
        except CancelledError:
            self.controller._emit_command(f"MCP {name}: 本地等待已取消；已接受动作的状态请查询回执", "warning", **log_options)
        except Exception as exc:
            self.controller._emit_command(f"MCP {name} 失败：{exc}", "error", **log_options)
        finally:
            with self._lock:
                self._pending.discard(future)
                self._shutdown_calls.discard(future)

    def close(self):
        with self._lock:
            if self._closed: return
            self._closed = True
            # Accepted snapshots own their captured data and finish even if the
            # robot has exited. TUI/headless drain pending results before closing.
            pending = tuple(future for future in self._pending if future is not self._snapshot_job)
            shutdown = self._shutdown_calls.copy()
            executor, self._snapshot_executor = self._snapshot_executor, None
        for future in pending:
            if future in shutdown:
                # Controller exit can beat the MCP event loop's acceptance
                # receipt. Give that receipt a bounded grace without blocking UI.
                timer = threading.Timer(.5, future.cancel)
                timer.daemon = True
                timer.start()
            else:
                future.cancel()
        if executor is not None:
            executor.shutdown(wait=False)
