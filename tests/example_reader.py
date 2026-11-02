"""Loopback reference peer for example's optional external POSIX reader; no model."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from uuid import uuid4

from rc_policy_sdk.images import map_images


def serve(address, ready, *, wrong_ack=False):
    # One request at a time: unload runs after any active mmap is closed and
    # revokes this owner before any queued request from it can start reading.
    instance = uuid4().hex
    revoked = set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, payload, code=200):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/v1/status":
                return self.reply({"instance_id": instance, "generation": 1})
            if self.path.startswith("/v1/operations/") and self.path.split("/")[-1] in revoked:
                return self.reply({"status": "succeeded", "result": {
                    "instance_id": instance, "state": "unloaded", "worker_pid": None}})
            self.reply({}, 404)  # An unknown request is never release proof.

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            owner = data["owner_id"]
            if self.path == "/v1/models/unload" and data.get("expected_instance_id") == instance:
                revoked.add(owner)
                return self.reply({"operation_id": owner})
            if self.path != "/v1/example/read" or data.get("instance_id") != instance or owner in revoked:
                return self.reply({}, 409)
            with map_images(data["images"]) as images:
                samples = {name: int(image.flat[0]) for name, image in images.items()}
                assert all(not image.flags.writeable for image in images.values())
            self.reply({"instance_id": instance, "owner_id": owner, "images_released": True,
                        "request_id": "different-request" if wrong_ack else data["request_id"],
                        "samples": samples})

    with HTTPServer(address, Handler) as server:
        ready(server.server_port)
        server.serve_forever()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    serve((args.bind, args.port), lambda port: print(port, flush=True))
