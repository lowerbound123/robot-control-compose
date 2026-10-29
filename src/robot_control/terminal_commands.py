"""Local terminal routing without UI, network, or MCP SDK imports."""
from __future__ import annotations

from .secret_config import reject_secret_edit

from concurrent.futures import CancelledError, ThreadPoolExecutor
import json
from pathlib import Path
import threading


SHORTHANDS = {
    "send_command": "commands",
    "command": "line",
    "show": "scope",
    "get_operation": "operation_id",
    "get_policy_result": "operation_id",
}
MCP_HELP = "\n".join([
    "mcp <tool> [JSON对象]；help mcp / mcp：工具与参数帮助；mcp help：RC 内置命令帮助",
    "简写：mcp send_command <动作>；mcp command <RC命令>；mcp show rc|policy|global；mcp get_operation <id>",
    "先 mcp take，确认 succeeded 后发动作，最后 mcp return；不自动接管。",
    "mcp return_and_wait [JSON对象]：交还并等待 policy 明确结果，timeout_s 默认 null（不超时）；超时不取消任务。",
    "mcp get_policy_result [操作ID]：查询等待结果；省略 ID 查询最近一次；result.outcome 区分 completed / terminated。",
    "示例：mcp send_command left:up:0.01@slow | right:hold",
    '示例：mcp send_command {"commands":["left:open","right:close"],"wait":true}',
    "调用在后台执行；wait=true 不阻塞终端，等待超时不取消动作；图像只显示元数据。",
])
PANEL_HELP = "camera [on|off]；monitor [on|off]：TUI 面板开关；不带参数切换，启动时均关闭。"
POLICY_MONITOR_HELP = "monitor-<policy_id> [on|off]：已加载 policy 的监控开关；不带参数切换；默认关闭；仅支持 TUI。"
MANAGE_HELP = 'manage-policy [apply|save|close]：本地 TUI 链编排；进入时暂停，apply 后暂停，save 只保存草稿。'
SNAPSHOT_HELP = "snapshot：后台保存三路原始相机 PNG 和完整 state 到当前工作目录 snapshot/<时间戳>/；支持本地 TUI/headless。"


def _invalid_constant(value):
    raise ValueError(f"JSON 不支持 {value}")


def parse_mcp_command(line: str) -> tuple[str, dict]:
    """Split only the prefix/tool; keep the argument tail intact."""
    parts = line.strip().split(maxsplit=2)
    if len(parts) < 2 or parts[0] != "mcp":
        raise ValueError("用法：mcp <tool> [JSON对象或简写参数]；help mcp 查看帮助")
    name = parts[1]
    if len(parts) == 2:
        return name, {}
    raw = parts[2]
    if raw.startswith("{"):
        arguments = json.loads(raw, parse_constant=_invalid_constant)
    elif name in SHORTHANDS:
        arguments = {SHORTHANDS[name]: raw}
    else:
        raise ValueError(f"{name} 的参数必须是 JSON 对象；help mcp 查看帮助")
    return name, arguments


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

    @property
    def tool_names(self):
        return tuple(tool["name"] for tool in self.service.tools) if self.service else ()

    def help(self):
        rows = [MCP_HELP]
        if self.service is None:
            return "\n".join([*rows, "MCP 服务未启动"])
        for tool in self.service.tools:
            schema = tool["input_schema"]
            required = schema.get("required", [])
            args = []
            for name, spec in schema.get("properties", {}).items():
                if name in required:
                    args.append(f"<{name}>")
                elif "default" in spec:
                    args.append(f"[{name}={json.dumps(spec['default'], ensure_ascii=False)}]")
                else:
                    args.append(f"[{name}]")
            description = (tool.get("description") or "").split("\n", 1)[0]
            rows.append(f"mcp {tool['name']} {' '.join(args)} — {description}")
        return "\n".join(rows)

    def submit(self, line: str):
        reject_secret_edit(line)
        words = line.split()
        if words and words[0] == "snapshot":
            if len(words) != 1:
                raise ValueError("用法：snapshot")
            return self._submit_snapshot()
        if words == ["help", "snapshot"]:
            self.controller._emit(SNAPSHOT_HELP)
            return
        if words and words[0] == 'manage-policy':
            if len(words) > 2 or (len(words) == 2 and words[1] not in ('apply', 'save', 'close')):
                raise ValueError(MANAGE_HELP)
            if self.policy_manager_handler is None:
                raise ValueError('manage-policy 仅支持本地 TUI')
            return self.policy_manager_handler(words[1] if len(words) == 2 else 'open')
        if words == ['help', 'manage-policy']:
            self.controller._emit(MANAGE_HELP)
            return
        if words and (words[0] in ("camera", "monitor") or words[0].startswith("monitor-")):
            if len(words) > 2 or (len(words) == 2 and words[1] not in ("on", "off")):
                raise ValueError(f"用法：{words[0]} [on|off]")
            if self.panel_handler is None:
                raise ValueError(f"{words[0]} 仅支持本地 TUI")
            return self.panel_handler(words[0], None if len(words) == 1 else words[1] == "on")
        if len(words) == 2 and words[0] == "help" and words[1].startswith("monitor-"):
            self.controller._emit(POLICY_MONITOR_HELP)
            return
        if len(words) == 2 and words[0] == "help" and words[1] in ("camera", "monitor"):
            self.controller._emit(PANEL_HELP)
            return
        if words in (["mcp"], ["help", "mcp"]):
            self.controller._emit(self.help())
            return
        if not words or words[0] != "mcp":
            identity = self.controller.submit(line)
            if words == ["help"]:
                self.controller._emit(MCP_HELP + "\n" + PANEL_HELP + "\n" + POLICY_MONITOR_HELP + "\n" + MANAGE_HELP + "\n" + SNAPSHOT_HELP)
            return identity
        name, arguments = parse_mcp_command(line)
        with self._lock:
            if self._closed or self.service is None:
                raise RuntimeError("MCP 服务未启动或正在关闭")
            future = self.service.submit_tool(name, arguments)
            self._pending.add(future)
            if name == 'command' and arguments.get('line', '').strip() in ('exit', 'quit'):
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
            self.controller._emit(f"snapshot 已保存：{path}")
        except Exception as exc:
            self.controller._emit(f"snapshot 保存失败：{exc}", "error")
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
            if payload is not None:
                text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            else:
                text = "\n".join(block.text for block in response.content if block.type == "text")
            images = sum(block.type == "image" for block in response.content)
            if images:
                text += f"\n图像内容未显示（{images} 张）；请查看 images 元数据。"
            self.controller._emit(f"MCP {name}: {text}", "error" if response.is_error else "info", **log_options)
        except CancelledError:
            self.controller._emit(f"MCP {name}: 本地等待已取消；已接受动作的状态请查询回执", "warning", **log_options)
        except Exception as exc:
            self.controller._emit(f"MCP {name} 失败：{exc}", "error", **log_options)
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
