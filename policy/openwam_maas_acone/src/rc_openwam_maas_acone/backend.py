"""OpenWAM v2 complete-chunk WebSocket client. The server owns image preprocessing."""
import base64
from io import BytesIO
import json
import sys

import numpy as np
from PIL import Image
from websockets.sync.client import connect

PONG = dict(type='pong', protocol_version=2, representation='qpos',
            action_representation='chunk_delta', action_horizon=32,
            gripper_indices=[6, 13], multiview=True)


def encode_png(frame):
    with BytesIO() as png:
        Image.fromarray(frame).save(png, format='PNG', compress_level=1)
        return base64.b64encode(png.getvalue()).decode('ascii')


def decode_reply(raw):
    if not isinstance(raw, str):
        raise ValueError('OpenWAM requires a JSON text frame')
    reply = json.loads(raw)
    if not isinstance(reply, dict):
        raise ValueError('OpenWAM reply must be an object')
    if reply.get('type') == 'error':
        raise RuntimeError(f"OpenWAM error: {reply.get('code')} {str(reply.get('message', ''))[:256]}")
    return reply


class OpenWAMBackend:
    chunk_size = 32

    def __init__(self, params, record_stage):
        self.record_stage = record_stage
        self.url = params['service_url']
        self.open_timeout = params['open_timeout_s']
        self.timeout = params['inference_timeout_s']
        self.socket = None
        self.last_request_bytes = 0
        self.server_latency_ms = '—'

    def load(self):
        self.socket = connect(self.url, open_timeout=self.open_timeout, close_timeout=1,
                              ping_interval=None, compression=None, proxy=None)
        try:
            self.socket.send('{"type":"ping"}')
            reply = decode_reply(self.socket.recv(timeout=self.open_timeout))
            for key, expected in PONG.items():
                if type(reply.get(key)) is not type(expected) or reply[key] != expected:
                    raise ValueError(f'OpenWAM pong contract mismatch: {key}')
        except Exception:
            self.close()
            raise

    def warmup(self):
        # load() already verifies the service; a real observation starts inference.
        pass

    def predict(self, qpos, images, instruction):
        socket = self.socket
        if socket is None:
            raise RuntimeError('OpenWAM connection closed')
        self.record_stage('encode')
        payload = {name + '_camera': encode_png(images[name]) for name in ('head', 'left_wrist', 'right_wrist')}
        message = json.dumps(dict(type='chunk_obs', images=payload, prompt=instruction, state=qpos.tolist()),
                             ensure_ascii=True, allow_nan=False, separators=(',', ':'))
        self.record_stage('send')
        socket.send(message)
        self.record_stage('wait_response')
        raw = socket.recv(timeout=self.timeout)
        self.record_stage('decode')
        reply = decode_reply(raw)
        if reply.get('type') != 'action_chunk' or type(reply.get('horizon')) is not int or reply['horizon'] != 32:
            raise ValueError('OpenWAM expected action_chunk with horizon 32')
        rows = reply.get('actions')
        if (not isinstance(rows, list) or len(rows) != 32
                or any(not isinstance(row, list) or len(row) != 14
                       or any(type(value) not in (int, float) for value in row) for row in rows)):
            raise ValueError('OpenWAM actions must be 32 x 14 numbers')
        with np.errstate(over='raise', invalid='raise'):
            actions = np.asarray(rows, dtype=np.float32)
        if not np.isfinite(actions).all():
            raise ValueError('OpenWAM actions must be finite')
        # JSON is ASCII, so the character count is the payload byte count.
        self.last_request_bytes = len(message)
        latency = reply.get('latency_ms')
        self.server_latency_ms = (latency if type(latency) in (int, float) and 0 <= latency <= sys.float_info.max
                                  else '—')
        return actions

    def reset_context(self):
        # chunk_obs is stateless. The server's reset would affect other clients.
        if self.socket is None:
            self.load()

    def close(self):
        socket, self.socket = self.socket, None
        if socket is not None:
            socket.close()
