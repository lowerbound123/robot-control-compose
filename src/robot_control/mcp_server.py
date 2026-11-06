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
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, Strict, StrictBool, StrictInt, StrictStr, field_validator
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
import uvicorn

from .direct import control_settings
from .direct_commands import MOTIONS, parse_commands, structured_step
from .mcp_config import mcp_settings
from .direct_types import finite_number, finite_vector
from .operations import TERMINAL
from .policy_results import PolicyWaitActive
from .web_preview import WebPreview, multipart_frames

Timeout = Annotated[float, Field(ge=0, le=120)]
PolicyWaitTimeout = Annotated[FiniteFloat, Field(ge=0)]
Quality = Annotated[int, Field(ge=1, le=100)]
Vector6 = Annotated[list[FiniteFloat], Field(min_length=6, max_length=6)]
Arm = Literal["left", "right"]
PolicyArgument = StrictStr | StrictBool | StrictInt | Annotated[FiniteFloat, Strict()]


class ControlGuard(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: StrictStr
    control_epoch: StrictInt
    generation: StrictInt


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
        "Read-only until take, except recording on/off/toggle never requires takeover or changes motion/ownership. "
        "The human-only tui_command text-batch escape hatch uses local TUI semantics without take. "
        "Agents should not use tui_command or cancel_tui_command; use the structured tools and take instead. "
        "take pauses new policy inference and retains its model. "
        "return cancels manual motions, resets policy context from fresh feedback and resumes policy. "
        "return_and_wait resumes policy and awaits runtime.finish; its timeout or disconnection does not cancel the episode. "
        "Use get_policy_result to query it later. Completed and terminated outcomes are both valid policy reports. "
        "All clients share control ownership. Read props for units, speeds, topics and command grammar. "
        "Use policy_commands/help to discover manifest commands without loading models. "
        "policy_chain accepts an ordered source list: apply rebuilds while paused; save only writes default_chain. "
        "policy_command/retry_reload require exact instances and take; lifecycle success keeps MCP ownership. "
        "backend_profiles lists named connections; switch_backend/retry_backend require take and remain paused. "
        "Backend changes apply execution.on_backend_switch for each instance; failed instances are listed in retry_reload_instances. "
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
        epoch = getattr(controller.robot, 'generation', None)
        expected_epoch = payload.get('state', {}).get('backend_generation',
            payload.get('controller', {}).get('backend', {}).get('generation'))
        def stale():
            return {**payload, 'images': [], 'image_errors': {
                name: 'backend changed during image capture; request a fresh snapshot'
                for name in (cameras or camera_names)}}, []
        if expected_epoch is not None and epoch != expected_epoch:
            return stale()
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
                                 **({'backend_generation': epoch} if epoch is not None else {}),
                                 "content_index": len(content)})
            except Exception as exc:
                errors[camera] = str(exc)
        if epoch is not None and epoch != getattr(controller.robot, 'generation', epoch):
            return stale()
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
    async def help(policy_id: str | None = None, command: str | None = None) -> CallToolResult:
        """Read RC help, or a policy/instance's declared capabilities and one optional command."""
        if policy_id is not None or command is not None:
            try:
                return result(await asyncio.to_thread(controller.policy_help, policy_id, command))
            except (ValueError, RuntimeError) as exc:
                return result({'error': str(exc)}, error=True)
        return await query("help")

    @server.tool(annotations=read)
    async def policy_commands(policy_id: str | None = None) -> CallToolResult:
        """Discover typed commands/effects without importing models; a repeated source lists all instances."""
        try:
            return result(await asyncio.to_thread(controller.policy_capabilities, policy_id))
        except (ValueError, RuntimeError) as exc:
            return result({'error': str(exc)}, error=True)

    @server.tool(annotations=read)
    async def props() -> CallToolResult:
        """Read robot configuration, control units, gripper defaults and directions."""
        connection = (controller.backend_properties() if hasattr(controller, 'backend_properties')
                      else {'robot': config.get('robot', {})})
        return result({**connection, "frequency_hz": config.get("frequency_hz"),
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
            epoch = observation.get('backend_generation')
            if epoch is not None and epoch != getattr(controller.robot, 'generation', epoch):
                raise RuntimeError('backend changed while reading state/images; request a fresh snapshot')
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

    def rejected(tool, exc):
        payload = {'error': str(exc)}
        controller._emit_command(f'MCP {tool} rejected: ' + json.dumps(payload, ensure_ascii=False), 'error')
        return result(payload, error=True)

    async def submit(line, wait, timeout_s, expected_control=None):
        try:
            if not line.strip():
                raise ValueError("command cannot be empty")
            identity = await asyncio.to_thread(controller.submit, line, origin="mcp",
                expected_control=expected_control.model_dump() if expected_control is not None else None)
            # Shutdown acknowledges acceptance before the server closes its listener.
            if line.strip() in ("exit", "quit"):
                return result({"operation_id": identity, "status": "accepted"})
            return await finish_operation(identity, wait, timeout_s)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return rejected(line if line in ('take', 'return') else 'command', exc)

    @server.tool(annotations=write)
    async def command(line: str, wait: bool = False, timeout_s: Timeout = 30,
                      expected_control: ControlGuard | None = None) -> CallToolResult:
        """Execute RC/policy commands. Queries and recording need no take; other writes require take. UI commands stay local."""
        return await submit(line, wait, timeout_s, expected_control)

    @server.tool(annotations=write)
    async def tui_command(text: StrictStr, wait: bool = False, timeout_s: Timeout = 30,
                          expected_control: ControlGuard | None = None) -> CallToolResult:
        """Human WebUI text input only. Agents should not use this tool; use structured tools and take instead.

        Execute newline-separated RC/policy commands with local TUI semantics, without take.
        Text batches are FIFO; real local TUI input cancels their unstarted commands.
        No recursive mcp calls, local panels or snapshots. A failed line stops its batch.
        Timeout/disconnection only stops waiting. Query get_operation or explicitly cancel_tui_command.
        """
        try:
            identity = await asyncio.to_thread(controller.submit_tui_batch, text,
                expected_control=expected_control.model_dump() if expected_control is not None else None)
            # An exit anywhere at the end of a batch may close the HTTP listener.
            if text.strip().splitlines()[-1].strip() in ('exit', 'quit'):
                return result({'operation_id': identity, 'status': 'accepted'})
            return await finish_operation(identity, wait, timeout_s)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return rejected('tui_command', exc)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                           idempotent_hint=True, open_world_hint=False))
    async def cancel_tui_command(operation_id: StrictStr) -> CallToolResult:
        """Human text-batch cancellation. Agents should not use this tool; prefer structured controls.

        Idempotently stop only this batch's unstarted lines. An already-started command finishes
        under its existing lifecycle; poll get_operation until the parent is terminal.
        """
        try:
            payload = await asyncio.to_thread(controller.cancel_tui_batch, operation_id)
            return result({**payload, 'controller': controller.status()})
        except (ValueError, RuntimeError, PermissionError) as exc:
            return rejected('cancel_tui_command', exc)

    async def manage(tool, method, *args, wait=False, timeout_s=30, **kwargs):
        try:
            identity = await asyncio.to_thread(method, *args, **kwargs)
            return await finish_operation(identity, wait, timeout_s)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return rejected(tool, exc)

    @server.tool(annotations=write)
    async def switch_backend(profile: StrictStr, wait: bool = False, timeout_s: Timeout = 30,
                             expected_control: ControlGuard | None = None) -> CallToolResult:
        """After take, reconnect a named profile and adapt each policy; keep ownership and remain paused. Never automatically retry."""
        import shlex
        return await manage('switch_backend', controller.submit, shlex.join(['backend', 'switch', profile]),
            origin='mcp', mcp_tool='switch_backend', wait=wait, timeout_s=timeout_s,
            expected_control=expected_control.model_dump() if expected_control is not None else None)

    @server.tool(annotations=write)
    async def retry_backend(wait: bool = False, timeout_s: Timeout = 30,
                            expected_control: ControlGuard | None = None) -> CallToolResult:
        """After take and an explicit retry request, finish pending cleanup and retry the failed backend connection."""
        return await manage('retry_backend', controller.submit, 'backend retry',
            origin='mcp', mcp_tool='retry_backend', wait=wait, timeout_s=timeout_s,
            expected_control=expected_control.model_dump() if expected_control is not None else None)

    @server.tool(annotations=read)
    async def backend_profiles() -> CallToolResult:
        """List named robot connection profiles without connecting, importing policies or taking control."""
        return await query('backend list')

    @server.tool(annotations=write)
    async def policy_chain(policies: list[StrictStr], action: Literal['apply', 'save'] = 'apply',
                           wait: bool = False, timeout_s: Timeout = 30,
                           expected_control: ControlGuard | None = None) -> CallToolResult:
        """After take, apply an ordered source list (duplicates allowed) or save it as default_chain.

        Apply rebuilds the group, stays paused under MCP, and does not save or reset.
        Save only writes the supplied list; it does not change the active group or member parameters.
        """
        return await manage('policy_chain', controller.submit_policy_chain, policies, action,
                            wait=wait, timeout_s=timeout_s,
                            expected_control=expected_control.model_dump() if expected_control is not None else None)

    @server.tool(annotations=write)
    async def record(action: Literal['status', 'on', 'off', 'toggle'] = 'status',
                     wait: bool = False, timeout_s: Timeout = 30,
                     expected_control: ControlGuard | None = None) -> CallToolResult:
        """Query/control recording without take, independently of policy execution and control ownership.

        off succeeds only after accepted data is flushed. Waiting timeout never cancels the operation.
        """
        if action == 'status':
            return await query('record status')
        return await manage('record', controller.submit, 'record' if action == 'toggle' else 'record ' + action,
                            origin='mcp', mcp_tool='record', wait=wait, timeout_s=timeout_s,
                            expected_control=expected_control.model_dump() if expected_control is not None else None)

    @server.tool(annotations=write)
    async def policy_command(instance_id: str, command: str, args: list[PolicyArgument] = [],
                             wait: bool = False, timeout_s: Timeout = 30,
                             expected_control: ControlGuard | None = None) -> CallToolResult:
        """After take, execute one declared command on an exact loaded instance using strictly typed JSON arguments.

        Query policy_commands/help for argument types and effects. Lifecycle effects preserve MCP ownership and pause policy.
        timeout_s bounds this wait, not the manifest's handler deadline. No automatic retry.
        """
        return await manage('policy_command', controller.submit_policy_command, instance_id, command, list(args),
                            wait=wait, timeout_s=timeout_s,
                            expected_control=expected_control.model_dump() if expected_control is not None else None)

    @server.tool(annotations=write)
    async def retry_reload(instance_id: str, wait: bool = False, timeout_s: Timeout = 30,
                           expected_control: ControlGuard | None = None) -> CallToolResult:
        """After take, cold-retry one exact failed instance from status.retry_reload_instances (or legacy retry_reload_instance); preserve healthy instances and MCP ownership."""
        import shlex
        return await manage('retry_reload', controller.submit, shlex.join(['retry-reload', instance_id]),
                            origin='mcp', mcp_tool='retry_reload', wait=wait, timeout_s=timeout_s,
                            expected_control=expected_control.model_dump() if expected_control is not None else None)

    @server.tool(annotations=write)
    async def take(wait: bool = True, timeout_s: Timeout = 30,
                   expected_control: ControlGuard | None = None) -> CallToolResult:
        """Take control, invalidate policy output and pause new inference while retaining the model."""
        return await submit("take", wait, timeout_s, expected_control)

    @server.tool(name="return", annotations=write)
    async def return_control(wait: bool = False, timeout_s: Timeout = 30,
                             expected_control: ControlGuard | None = None) -> CallToolResult:
        """Cancel manual motion, restore read-only MCP and resume policy after resetting its context."""
        return await submit("return", wait, timeout_s, expected_control)

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
    async def return_and_wait(timeout_s: PolicyWaitTimeout | None = None,
                              expected_control: ControlGuard | None = None) -> CallToolResult:
        """After take, return control and await explicit policy completion/termination; default: no timeout.

        A timeout (0 returns immediately), cancellation or disconnect only ends this
        call. The episode continues and will hand control back to MCP. Query its
        operation_id with get_policy_result. A succeeded receipt carries the
        business outcome completed or terminated in result.outcome.
        """
        try:
            # Registration is a short lock/queue operation. Do it before the
            # first await so cancellation cannot leave an unregistered episode.
            identity = controller.submit_policy_wait(expected_control=expected_control.model_dump() if expected_control is not None else None)
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
            rejected('return_and_wait', exc)
            return result({'error': str(exc), 'operation_id': exc.operation_id}, error=True)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return rejected('return_and_wait', exc)

    async def motion(tool, make_steps, wait, timeout_s, clear_queue, include_images, cameras, image_quality, expected_control):
        try:
            validate_images(cameras, image_quality)
            steps = make_steps()
            identity = await asyncio.to_thread(controller.submit_direct, steps, clear_queue=clear_queue, tool_name=tool,
                expected_control=expected_control.model_dump() if expected_control is not None else None)
            return await finish_operation(identity, wait, timeout_s, include_images, cameras, image_quality)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return rejected(tool, exc)

    @server.tool(annotations=write)
    async def send_command(commands: str | list[str | list[str]], wait: bool = False, timeout_s: Timeout = 30,
                           clear_queue: bool = False, include_images: bool = False, cameras: list[str] | None = None,
                           image_quality: Quality | None = None, expected_control: ControlGuard | None = None) -> CallToolResult:
        """Queue relative commands after take. Example: left:up:0.02@slow | right:hold. Lists are sequential steps.

        Motions: forward/backward, left/right, up/down, twist-clockwise/counterclockwise,
        tilt-down-toward-table/tilt-up-away-from-table, turn-left-across-table/turn-right-across-table,
        open/close, hold, home. Moves use meters/radians; gripper values are raw absolute commands.
        clear_queue removes pending steps only. wait timeout does not cancel motion.
        """
        return await motion('send_command', lambda: parse_commands(commands, direct["default_speed"]), wait, timeout_s, clear_queue, include_images, cameras, image_quality, expected_control)

    @server.tool(annotations=write)
    async def move_eef(targets: dict[Arm, EefTarget], wait: bool = False, timeout_s: Timeout = 30,
                       clear_queue: bool = False, include_images: bool = False, cameras: list[str] | None = None,
                       image_quality: Quality | None = None, expected_control: ControlGuard | None = None) -> CallToolResult:
        """Queue absolute xyz/RPY (meters/radians) for selected arms; optional raw gripper and speed. Requires take."""
        return await motion('move_eef', lambda: [structured_step(4, {arm: value.model_dump(exclude_none=True) for arm, value in targets.items()}, direct["default_speed"])],
                            wait, timeout_s, clear_queue, include_images, cameras, image_quality, expected_control)

    @server.tool(annotations=write)
    async def move_joints(targets: dict[Arm, JointTarget], wait: bool = False, timeout_s: Timeout = 30,
                          clear_queue: bool = False, include_images: bool = False, cameras: list[str] | None = None,
                          image_quality: Quality | None = None, expected_control: ControlGuard | None = None) -> CallToolResult:
        """Queue six absolute joint angles per selected arm, with separate raw gripper and speed. Requires take."""
        return await motion('move_joints', lambda: [structured_step(5, {arm: value.model_dump(exclude_none=True) for arm, value in targets.items()}, direct["default_speed"])],
                            wait, timeout_s, clear_queue, include_images, cameras, image_quality, expected_control)

    return server


def create_app(controller, settings, *, server=None, preview=None):
    server = server if server is not None else create_server(controller)
    preview = preview if preview is not None else WebPreview(
        lambda: controller.robot.camera_preview_snapshot(), stopping=controller.done.is_set,
        source_generation=lambda: getattr(getattr(controller, 'robot', None), 'generation', 0))
    mcp_app = server.streamable_http_app(stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=settings["allowed_hosts"], allowed_origins=settings["allowed_origins"]))

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with server.session_manager.run():
                yield
        finally:
            deadline = getattr(controller, '_shutdown_deadline', None)
            if not preview.close(min(time.monotonic() + 1, deadline) if deadline is not None else time.monotonic() + 1):
                controller.exit_code = 1

    async def health(request):
        return JSONResponse(controller.status())

    async def preview_status(request):
        return JSONResponse(preview.status(), headers={'Cache-Control': 'no-store'})

    async def preview_stream(request):
        try:
            reader = preview.subscribe(request.path_params['camera'])
        except ValueError:
            return JSONResponse({'error': 'unknown camera'}, status_code=404)
        except RuntimeError:
            return JSONResponse({'error': 'preview is stopping'}, status_code=503)
        return StreamingResponse(multipart_frames(reader), media_type='multipart/x-mixed-replace; boundary=frame',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    app = Starlette(routes=[Route("/health", health), Route('/preview/status', preview_status),
        Route('/preview/{camera}.mjpg', preview_stream), Mount("/", app=mcp_app)], lifespan=lifespan)
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
        self.preview = WebPreview(lambda: controller.robot.camera_preview_snapshot(),
                                  stopping=lambda: self.closing.is_set() or controller.done.is_set(),
                                  source_generation=lambda: getattr(getattr(controller, 'robot', None), 'generation', 0))
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
        app = create_app(self.controller, self.settings, server=self.mcp_server, preview=self.preview)
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
        deadline = getattr(self.controller, '_shutdown_deadline', None)
        preview_ok = self.preview.close(min(time.monotonic() + 1, deadline) if deadline is not None else time.monotonic() + 1)
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(7)
            if self.thread.is_alive():
                raise RuntimeError("MCP HTTP thread did not stop")
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if not preview_ok:
            self.controller.exit_code = 1
            raise RuntimeError('Web preview encoder did not stop before cleanup deadline')
