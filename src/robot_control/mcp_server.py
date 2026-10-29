"""HTTP MCP frontend; all mutations are submitted to the shared RC controller."""
from __future__ import annotations

import asyncio
import base64
import copy
import hmac
import io
import json
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
import uvicorn

from .direct import control_settings
from .direct_commands import MOTIONS, parse_commands, structured_step
from .mcp_config import mcp_settings
from .direct_types import finite_number, finite_vector
from .operations import TERMINAL
from .policy_results import PolicyWaitActive

Timeout = Annotated[float, Field(ge=0, le=120)]
PolicyWaitTimeout = Annotated[FiniteFloat, Field(ge=0)]
Quality = Annotated[int, Field(ge=1, le=100)]
Vector6 = Annotated[list[FiniteFloat], Field(min_length=6, max_length=6)]
Arm = Literal["left", "right"]


class TargetOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gripper: FiniteFloat | None = None
    speed: Literal["slow", "medium", "fast"] | None = None

    @field_validator("gripper", mode="before")
    @classmethod
    def validate_gripper(cls, value):
        return None if value is None else finite_number(value, "gripper")


class EefTarget(TargetOptions):
    end_pos: Vector6

    @field_validator("end_pos", mode="before")
    @classmethod
    def validate_position(cls, value):
        return finite_vector(value, 6, "end_pos")


class JointTarget(TargetOptions):
    joint_pos: Vector6

    @field_validator("joint_pos", mode="before")
    @classmethod
    def validate_position(cls, value):
        return finite_vector(value, 6, "joint_pos")


class BearerTokenMiddleware:
    def __init__(self, app, token):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            supplied = Headers(scope=scope).get("authorization", "").encode()
            if not hmac.compare_digest(supplied, self.expected):
                return await JSONResponse({"error": "unauthorized"}, status_code=401,
                                          headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        await self.app(scope, receive, send)


def result(payload, images=(), *, error=False):
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, allow_nan=False)), *images],
                          structured_content=payload, is_error=error)


def create_server(controller):
    server = MCPServer("robot-control", version="0.3.0", instructions=(
        "Read-only until take. take pauses new policy inference and retains its model. "
        "return cancels manual motions, resets policy context from fresh feedback and resumes policy. "
        "return_and_wait resumes policy and awaits runtime.finish; its timeout or disconnection does not cancel the episode. "
        "Use get_policy_result to query it later. Completed and terminated outcomes are both valid policy reports. "
        "All clients share control ownership. Read props for units, speeds, topics and command grammar. "
        "Only confirmed operation success means completion; wait timeout does not cancel motion."
    ))
    read = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)
    config = getattr(controller, "config", {})
    direct = control_settings(config.get("direct_control"))
    camera_names = tuple(config.get("robot", {}).get("camera_topics", {"head": "", "left_wrist": "", "right_wrist": ""}))

    def validate_images(cameras, image_quality):
        if cameras is not None and (not cameras or len(set(cameras)) != len(cameras) or set(cameras) - set(camera_names)):
            raise ValueError(f"cameras must be unique names from {camera_names}")
        if image_quality is not None and not 1 <= image_quality <= 100:
            raise ValueError("invalid image quality")

    def attach_images(payload, include_images, cameras, image_quality):
        if not include_images:
            return payload, []
        try:
            frames, errors = controller.robot.image_snapshot()
        except RuntimeError as exc:
            return {**payload, "images": [], "image_errors": {name: str(exc) for name in (cameras or camera_names)}}, []
        metadata, content = [], []
        selected = camera_names if cameras is None else cameras
        for camera in selected:
            if camera not in frames:
                continue
            frame, timestamp = frames[camera]
            try:
                image = Image.fromarray(frame)
                image.thumbnail((1024, 768))
                stream = io.BytesIO()
                image.save(stream, format="JPEG", quality=image_quality or 85)
                content.append(ImageContent(type="image", mime_type="image/jpeg", data=base64.b64encode(stream.getvalue()).decode()))
                metadata.append({"camera": camera, "timestamp": timestamp, "width": image.width, "height": image.height,
                                 "content_index": len(content)})
            except Exception as exc:
                errors[camera] = str(exc)
        return {**payload, "images": metadata, "image_errors": {key: value for key, value in errors.items() if key in selected}}, content

    async def finish_operation(identity, wait, timeout_s, include_images=False, cameras=None, image_quality=None):
        payload = controller.get_operation(identity)
        deadline = time.monotonic() + timeout_s
        while wait and payload["status"] not in TERMINAL:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Cancellation cannot interrupt to_thread itself. Bound each wait so
            # closing a local call never leaves an executor thread waiting 120s.
            payload = await asyncio.to_thread(controller.wait_operation, identity, min(.1, remaining))
        payload = {**payload, "controller": controller.status()}
        payload, images = await asyncio.to_thread(attach_images, payload, include_images, cameras, image_quality)
        return result(payload, images, error=payload["status"] in ("failed", "cancelled"))

    async def query(line):
        try:
            payload = await asyncio.to_thread(controller.query, line)
            return result(payload if isinstance(payload, dict) else {"result": payload})
        except (ValueError, RuntimeError) as exc:
            return result({"error": str(exc)}, error=True)

    @server.tool(annotations=read)
    async def status() -> CallToolResult:
        """Read controller state, policy, ownership, inference and motion progress."""
        return await query("status")

    @server.tool(annotations=read)
    async def policies() -> CallToolResult:
        """List valid/invalid installed policy manifests without importing models."""
        return await query("policies")

    @server.tool(annotations=read)
    async def show(scope: Literal["rc", "policy", "global"]) -> CallToolResult:
        """Read RC configuration, active policy configuration, or global shared state."""
        return await query("show " + scope)

    @server.tool(annotations=read)
    async def help() -> CallToolResult:
        """List built-in RC commands supported by command(line); policy extensions are local-only."""
        return await query("help")

    @server.tool(annotations=read)
    async def props() -> CallToolResult:
        """Read robot configuration, control units, gripper defaults and directions."""
        return result({"robot": config.get("robot", {}), "frequency_hz": config.get("frequency_hz"),
                       "direct_control": direct, "safety": config.get("safety", {}),
                       "policy_modes": ["qpos", "epos", "mixed"], "control_owner": controller.status()["control_owner"],
                       "units": {"epos": "absolute xyz (meters), XYZ RPY (radians), raw gripper", "qpos": "six radians plus raw gripper per arm"},
                       "motions": MOTIONS, "grammar": "left|right|both:motion[&motion][:value][@slow|medium|fast]",
                       "directions": "translation in base frame: forward +x, left +y, up +z; rotation in local tool frame",
                       "home": {"joint_pos": [0] * 6, "gripper": 0}, "reset": "uses RC reset_qpos"})

    @server.tool(annotations=read)
    async def state(include_images: bool = False, cameras: list[str] | None = None,
                    image_quality: Quality | None = None, wait_for_new: bool = False,
                    since_seq: Annotated[int, Field(ge=0)] | None = None, timeout_s: Timeout = 3) -> CallToolResult:
        """Read feedback and motion progress; optionally wait for fresh feedback or attach named JPEGs."""
        try:
            validate_images(cameras, image_quality)
            observation = await asyncio.to_thread(controller.robot.read_state)
            seq = sum(observation["sequences"].values())
            baseline = seq if since_seq is None else since_seq
            deadline = time.monotonic() + timeout_s
            while wait_for_new and seq <= baseline and time.monotonic() < deadline:
                await asyncio.sleep(min(.02, max(0, deadline - time.monotonic())))
                observation = await asyncio.to_thread(controller.robot.read_state)
                seq = sum(observation["sequences"].values())
            payload = {"state": observation, "seq": seq, "controller": controller.status(), "waited_for_new": wait_for_new and seq > baseline}
            payload, images = await asyncio.to_thread(attach_images, payload, include_images, cameras, image_quality)
            return result(payload, images)
        except (ValueError, RuntimeError) as exc:
            return result({"error": str(exc), "controller": controller.status()}, error=True)

    @server.tool(annotations=read)
    async def events(after_seq: int = 0, limit: int = 100) -> CallToolResult:
        """Read a bounded log page without consuming the TUI log queue."""
        try:
            return result(controller.read_events(after_seq, limit))
        except ValueError as exc:
            return result({"error": str(exc)}, error=True)

    @server.tool(annotations=read)
    async def get_operation(operation_id: str, wait: bool = False, timeout_s: Timeout = 30) -> CallToolResult:
        """Read or await a command receipt; waiting timeout never cancels the operation."""
        try:
            return await finish_operation(operation_id, wait, timeout_s)
        except (ValueError, RuntimeError) as exc:
            return result({"error": str(exc)}, error=True)

    async def submit(line, wait, timeout_s):
        try:
            if not line.strip():
                raise ValueError("command cannot be empty")
            identity = await asyncio.to_thread(controller.submit, line, origin="mcp")
            # Shutdown acknowledges acceptance before the server closes its listener.
            if line.strip() in ("exit", "quit"):
                return result({"operation_id": identity, "status": "accepted"})
            return await finish_operation(identity, wait, timeout_s)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return result({"error": str(exc)}, error=True)

    @server.tool(annotations=write)
    async def command(line: str, wait: bool = False, timeout_s: Timeout = 30) -> CallToolResult:
        """Execute any TUI command. Queries work read-only; other commands require take. RC settings remain cold."""
        return await submit(line, wait, timeout_s)

    @server.tool(annotations=write)
    async def take(wait: bool = True, timeout_s: Timeout = 30) -> CallToolResult:
        """Take control, invalidate policy output and pause new inference while retaining the model."""
        return await submit("take", wait, timeout_s)

    @server.tool(name="return", annotations=write)
    async def return_control(wait: bool = False, timeout_s: Timeout = 30) -> CallToolResult:
        """Cancel manual motion, restore read-only MCP and resume policy after resetting its context."""
        return await submit("return", wait, timeout_s)

    @server.tool(annotations=read)
    async def get_policy_result(operation_id: str | None = None) -> CallToolResult:
        """Read a policy episode without consuming it; omit the ID for the latest episode in this RC run."""
        try:
            payload = controller.get_policy_result(operation_id)
            return result({**payload, 'controller': controller.status()},
                          error=payload['status'] in ('failed', 'cancelled'))
        except (ValueError, RuntimeError) as exc:
            return result({'error': str(exc)}, error=True)

    @server.tool(annotations=write)
    async def return_and_wait(timeout_s: PolicyWaitTimeout | None = None) -> CallToolResult:
        """After take, return control and await explicit policy completion/termination; default: no timeout.

        A timeout (0 returns immediately), cancellation or disconnect only ends this
        call. The episode continues and will hand control back to MCP. Query its
        operation_id with get_policy_result. A succeeded receipt carries the
        business outcome completed or terminated in result.outcome.
        """
        try:
            # Registration is a short lock/queue operation. Do it before the
            # first await so cancellation cannot leave an unregistered episode.
            identity = controller.submit_policy_wait()
            deadline = None if timeout_s is None else time.monotonic() + timeout_s
            payload = controller.get_policy_result(identity)
            while not payload['finished']:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                await asyncio.to_thread(controller.wait_operation, identity,
                                        .1 if remaining is None else min(.1, remaining))
                payload = controller.get_policy_result(identity)
            return result({**payload, 'timed_out': not payload['finished'], 'controller': controller.status()},
                          error=payload['status'] in ('failed', 'cancelled'))
        except PolicyWaitActive as exc:
            return result({'error': str(exc), 'operation_id': exc.operation_id}, error=True)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return result({'error': str(exc)}, error=True)

    async def motion(make_steps, wait, timeout_s, clear_queue, include_images, cameras, image_quality):
        try:
            validate_images(cameras, image_quality)
            steps = make_steps()
            identity = await asyncio.to_thread(controller.submit_direct, steps, clear_queue=clear_queue)
            return await finish_operation(identity, wait, timeout_s, include_images, cameras, image_quality)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return result({"error": str(exc)}, error=True)

    @server.tool(annotations=write)
    async def send_command(commands: str | list[str | list[str]], wait: bool = False, timeout_s: Timeout = 30,
                           clear_queue: bool = False, include_images: bool = False, cameras: list[str] | None = None,
                           image_quality: Quality | None = None) -> CallToolResult:
        """Queue relative commands after take. Example: left:up:0.02@slow | right:hold. Lists are sequential steps.

        Motions: forward/backward, left/right, up/down, twist-clockwise/counterclockwise,
        tilt-down-toward-table/tilt-up-away-from-table, turn-left-across-table/turn-right-across-table,
        open/close, hold, home. Moves use meters/radians; gripper values are raw absolute commands.
        clear_queue removes pending steps only. wait timeout does not cancel motion.
        """
        return await motion(lambda: parse_commands(commands, direct["default_speed"]), wait, timeout_s, clear_queue, include_images, cameras, image_quality)

    @server.tool(annotations=write)
    async def move_eef(targets: dict[Arm, EefTarget], wait: bool = False, timeout_s: Timeout = 30,
                       clear_queue: bool = False, include_images: bool = False, cameras: list[str] | None = None,
                       image_quality: Quality | None = None) -> CallToolResult:
        """Queue absolute xyz/RPY (meters/radians) for selected arms; optional raw gripper and speed. Requires take."""
        return await motion(lambda: [structured_step(4, {arm: value.model_dump(exclude_none=True) for arm, value in targets.items()}, direct["default_speed"])],
                            wait, timeout_s, clear_queue, include_images, cameras, image_quality)

    @server.tool(annotations=write)
    async def move_joints(targets: dict[Arm, JointTarget], wait: bool = False, timeout_s: Timeout = 30,
                          clear_queue: bool = False, include_images: bool = False, cameras: list[str] | None = None,
                          image_quality: Quality | None = None) -> CallToolResult:
        """Queue six absolute joint angles per selected arm, with separate raw gripper and speed. Requires take."""
        return await motion(lambda: [structured_step(5, {arm: value.model_dump(exclude_none=True) for arm, value in targets.items()}, direct["default_speed"])],
                            wait, timeout_s, clear_queue, include_images, cameras, image_quality)

    return server


def create_app(controller, settings, *, server=None):
    server = server if server is not None else create_server(controller)
    mcp_app = server.streamable_http_app(stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=settings["allowed_hosts"], allowed_origins=settings["allowed_origins"]))

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    async def health(request):
        return JSONResponse(controller.status())

    app = Starlette(routes=[Route("/health", health), Mount("/", app=mcp_app)], lifespan=lifespan)
    token = os.environ.get(settings["token_env"])
    if token:
        app.add_middleware(BearerTokenMiddleware, token=token)
    return app


class MCPService:
    """Own HTTP and local tool calls; controller lifecycle belongs to the CLI."""
    def __init__(self, controller, config=None, *, override=None, allow_ephemeral=False):
        self.controller = controller
        self.settings = mcp_settings(config, override, allow_ephemeral=allow_ephemeral)
        self.port = self.settings["port"]
        self.listener = self.server = self.thread = None
        self.closing = threading.Event()
        self.failure = None
        self.mcp_server = None
        self.tools = ()
        self._loop = None
        self._call_lock = threading.Lock()
        self._futures = set()
        self._local_tasks = set()

    @property
    def url(self):
        host = self.settings["host"]
        return f"http://{'[' + host + ']' if ':' in host else host}:{self.port}/mcp"

    def bind(self):
        if self.listener is not None:
            return
        family, kind, protocol, _, address = socket.getaddrinfo(self.settings["host"], self.port, type=socket.SOCK_STREAM)[0]
        listener = socket.socket(family, kind, protocol)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(address)
            listener.listen(128)
            self.port = listener.getsockname()[1]
            self.listener = listener
        except BaseException:
            listener.close()
            raise

    def start(self):
        if self.thread is not None or self.closing.is_set():
            raise RuntimeError("MCP service has already been started or closed")
        self.bind()
        self.mcp_server = create_server(self.controller)
        app = create_app(self.controller, self.settings, server=self.mcp_server)
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, ws="none", timeout_graceful_shutdown=5))

        async def serve():
            self.tools = tuple({"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
                               for tool in await self.mcp_server.list_tools())
            with self._call_lock:
                self._loop = asyncio.get_running_loop()
            try:
                await self.server.serve(sockets=[self.listener])
            finally:
                with self._call_lock:
                    self._loop = None
                    pending = tuple(self._futures)
                for future in pending:
                    future.cancel()
                tasks = tuple(self._local_tasks)
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

        def run():
            try:
                asyncio.run(serve())
            except BaseException as exc:
                self.failure = exc
            finally:
                if not self.closing.is_set() and not self.controller.done.is_set():
                    self.controller.exit_code = 1
                    self.controller._emit(f"MCP HTTP 服务退出：{self.failure or 'unexpected stop'}", "error")
                    self.controller.submit("exit")

        self.thread = threading.Thread(target=run, name="rc-mcp", daemon=False)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.close()
                raise RuntimeError(f"MCP startup failed: {self.failure or 'startup timeout'}")
            time.sleep(.01)

    def submit_tool(self, name, arguments):
        """Schedule the same registered tool used by HTTP without blocking the caller."""
        with self._call_lock:
            if (self.closing.is_set() or self.controller.done.is_set()
                    or self._loop is None or not self.server.started):
                raise RuntimeError("MCP service is not running")
            future = asyncio.run_coroutine_threadsafe(
                self._call_local_tool(name, copy.deepcopy(arguments)), self._loop)
            self._futures.add(future)
        future.add_done_callback(self._forget_call)
        return future

    async def _call_local_tool(self, name, arguments):
        if self.closing.is_set() or self.controller.done.is_set():
            raise RuntimeError("MCP service is closing")
        task = asyncio.current_task()
        self._local_tasks.add(task)
        try:
            return await self.mcp_server.call_tool(name, arguments)
        except ToolError as exc:
            return result({"error": str(exc)}, error=True)
        finally:
            self._local_tasks.discard(task)

    def _forget_call(self, future):
        with self._call_lock:
            self._futures.discard(future)

    def close(self):
        with self._call_lock:
            self.closing.set()
            pending = tuple(self._futures)
        for future in pending:
            future.cancel()
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(7)
            if self.thread.is_alive():
                raise RuntimeError("MCP HTTP thread did not stop")
        if self.listener is not None:
            self.listener.close()
            self.listener = None
