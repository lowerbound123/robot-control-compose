"""One monitor request per worker, independent of action and observation slots."""
import time

from rc_policy_sdk.monitor import MONITOR_IDENTITY, normalize_snapshot


class MonitorClient:
    def _init_monitor(self):
        self.supports_monitor = False
        self._monitor_pending = None
        self._monitor_response = None
        self._monitor_serial = 0

    def submit_monitor(self, epoch):
        with self._condition:
            if (not self.supports_monitor or self._state != "running" or self._failure
                    or self._pending_operation or self._monitor_pending is not None):
                return False
            self._monitor_serial += 1
            message = dict(type="monitor", monitor_id=self._monitor_serial,
                           monitor_epoch=epoch, config_version=self.config_version)
            # Ownership is registered under the same lock as nonblocking enqueue.
            self._monitor_pending = {**message, "started_at": time.monotonic()}
            try:
                accepted = self._enqueue_monitor(message)
            except Exception:
                self._monitor_pending = None
                raise
            if not accepted:
                self._monitor_pending = None
            return accepted

    def _receive_monitor(self, message):
        pending = self._monitor_pending
        if pending is None or any(message.get(key) != pending[key] for key in MONITOR_IDENTITY):
            return
        response = dict(message)
        if response["type"] == "monitor_result":
            try:
                response["result"] = normalize_snapshot(response.get("result"))
            except (TypeError, ValueError) as exc:
                response = {**response, "type": "monitor_error", "error": str(exc)}
                response.pop("result", None)
        self._monitor_response = response
        self._monitor_pending = None

    def poll_monitor(self):
        with self._condition:
            response, self._monitor_response = self._monitor_response, None
            return response

    @property
    def monitor_started_at(self):
        with self._condition:
            return self._monitor_pending["started_at"] if self._monitor_pending else None

    def monitor_members(self):
        return {self.config["id"]: self}
