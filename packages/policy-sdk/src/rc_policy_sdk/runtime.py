"""Policy metadata, deferred control tools and RC-owned external image leases."""
from contextlib import contextmanager
import inspect
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .wire import receive_message
from .outbox import PrioritySender
from .control import merge_control_requests


class WorkerChannel:
    def __init__(self, connection):
        self.connection = connection
        self.commands = queue.Queue()
        self.observations = ThreadPoolExecutor(max_workers=1, thread_name_prefix='policy-observe')
        self.on_observe = None
        self.on_monitor = None
        self.on_monitor_gate = None
        self.sender = PrioritySender(connection)
        self.on_command = None
        self.on_command_gate = None
        self.on_command_resume = None
        self.runtime_tools = frozenset()
        self._send_lock = threading.Lock()
        self._condition = threading.Condition()
        self._acks = {}
        self._serial = 0
        self._closed = False
        self.reader = threading.Thread(target=self._read, name='policy-channel', daemon=True)
        self.reader.start()

    def send(self, message):
        self.sender.send(message)

    def send_monitor(self, message):
        return self.sender.send_monitor(message)

    def _read(self):
        try:
            while True:
                message = receive_message(self.connection)
                if message.get('type') == 'resource_ack':
                    with self._condition:
                        self._acks[message['rpc_id']] = message
                        self._condition.notify_all()
                elif message.get('type') == 'policy_command':
                    if self.on_command:
                        self.on_command(message)
                elif message.get('type') == 'command_gate':
                    if self.on_command_gate:
                        self.on_command_gate()
                elif message.get('type') == 'command_resume':
                    if self.on_command_resume:
                        self.on_command_resume()
                elif message.get('type') == 'monitor':
                    if self.on_monitor:
                        self.on_monitor(message)
                elif message.get('type') == 'observe':
                    if self.on_observe:
                        self.observations.submit(self.on_observe, message)
                else:
                    if message.get("type") in ("update_config", "reset_context", "shutdown") and self.on_command_gate:
                        self.on_command_gate()
                    if message.get("type") in ("update_config", "reset_context", "shutdown") and self.on_monitor_gate:
                        self.on_monitor_gate()
                    self.commands.put(message)
        except (EOFError, OSError):
            with self._condition:
                self._closed = True
                self._condition.notify_all()
            self.commands.put({'type': 'shutdown'})

    def rpc(self, method, **payload):
        with self._condition:
            self._serial += 1
            serial = self._serial
        self.send({'type': 'resource', 'rpc_id': serial, 'method': method, **payload})
        deadline = time.monotonic() + 15
        with self._condition:
            while serial not in self._acks:
                if self._closed or time.monotonic() >= deadline:
                    raise RuntimeError('RC external lease acknowledgement unavailable')
                self._condition.wait(.05)
            ack = self._acks.pop(serial)
        if ack.get('error'):
            raise RuntimeError(ack['error'])
        return ack.get('result')

    def barrier(self):
        self.observations.submit(lambda: None).result()


class PolicyRuntime:
    def __init__(self, channel, message=None):
        self.channel = channel
        message = message or {}
        metadata = message.get('runtime', {})
        self.observation_id = metadata.get('observation_id', str(message.get('request_id', 'initialization')))
        self.consumed_actions = metadata.get('consumed_actions', 0)
        self.control_epoch = metadata.get('control_epoch', message.get('generation', 0))
        self.config_version = message.get('config_version', 1)
        self._inference_thread = None
        self._control_requests = []

    @contextmanager
    def _inference_scope(self):
        # Only the worker opens this scope, around the policy callback itself.
        self._inference_thread = threading.current_thread()
        try:
            yield
        finally:
            self._inference_thread = None

    def pause(self, reason: str = "") -> None:
        """Request a pause after this entire inference succeeds.

        This records a request locally; it does not wait for or interrupt RC.
        Keep returning valid actions so all subsequent chain stages can finish.
        Only the active policy callback thread may call this method.
        """
        if self._inference_thread is not threading.current_thread():
            raise RuntimeError("pause() is only available in the active policy() callback thread")
        if "pause" not in self.channel.runtime_tools:
            raise RuntimeError("host does not support runtime.pause(); upgrade robot-control")
        if not isinstance(reason, str):
            raise TypeError("pause reason must be a string")
        self._control_requests = merge_control_requests(
            self._control_requests, [{"type": "pause", "reason": reason}])

    def finish(self, message: str = "", *, outcome: str = "completed") -> None:
        """Report a task result after the entire inference/chain succeeds.

        Still return valid actions. RC discards this round's actions and pauses;
        an outstanding MCP policy wait also receives control and this result.
        Ordinary pause requests take precedence over finish requests.
        """
        if self._inference_thread is not threading.current_thread():
            raise RuntimeError("finish() is only available in the active policy() callback thread")
        if "finish" not in self.channel.runtime_tools:
            raise RuntimeError("host does not support runtime.finish(); upgrade robot-control")
        if not isinstance(message, str):
            raise TypeError("finish message must be a string")
        if outcome not in ("completed", "terminated"):
            raise ValueError("finish outcome must be completed or terminated")
        self._control_requests = merge_control_requests(self._control_requests, [
            {"type": "finish", "outcome": outcome, "message": message}])

    def register_remote(self, remote_id, endpoint, owner_id, model_id, instance_id=None):
        return self.channel.rpc('register', remote_id=remote_id, endpoint=endpoint,
                                owner_id=owner_id, model_id=model_id, instance_id=instance_id)

    def update_remote(self, remote_id, generation):
        return self.channel.rpc('generation', remote_id=remote_id, generation=generation)

    def retain_remote(self, remote_id, request_id, descriptors):
        return self.channel.rpc('retain', remote_id=remote_id, request_id=request_id, images=descriptors)

    def release_images(self, remote_id, request_id):
        return self.channel.rpc('released', remote_id=remote_id, request_id=request_id)


def invoke(function, args, runtime):
    if 'runtime' in inspect.signature(function).parameters:
        return function(*args, runtime=runtime)
    return function(*args)
