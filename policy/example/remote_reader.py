"""可选的外部共享图像读者。演示对端协议见 tests/example_reader.py。"""
import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4


class RemoteReader:
    def __init__(self, endpoint, runtime):
        url = urlsplit(endpoint)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment):
            raise ValueError("reader_url 必须是无凭据的 HTTP(S) 服务地址")
        self.endpoint = endpoint.rstrip("/")
        self.owner = uuid4().hex  # 每次初始化使用新身份，包括同来源的两个实例。
        status = self._request("/v1/status")
        self.instance = status["instance_id"]
        generation = status["generation"]
        if not isinstance(self.instance, str) or not self.instance or type(generation) is not int:
            raise ValueError("外部 reader 未提供有效的实例和代次")
        runtime.register_remote("reader", self.endpoint, self.owner, "example-reader", self.instance)
        runtime.update_remote("reader", generation)

    def _request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(self.endpoint + path, data=data, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=2) as response:
            return json.load(response)

    def read(self, runtime, descriptors):
        request_id = uuid4().hex
        runtime.retain_remote("reader", request_id, descriptors)
        identity = {"instance_id": self.instance, "owner_id": self.owner, "request_id": request_id}
        ack = self._request("/v1/example/read", {**identity, "images": descriptors})
        if ack.get("images_released") is not True or any(ack.get(k) != v for k, v in identity.items()):
            raise RuntimeError("外部 reader 未确认释放本次请求")
        # 刻意不放在 finally：超时、断连、其他实例或旧请求的回执都不是释放证明。
        runtime.release_images("reader", request_id)
