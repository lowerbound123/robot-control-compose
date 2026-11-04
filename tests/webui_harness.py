"""Isolated mock RC + real example worker + local compatible-API test server.

No deployment/private configuration is read. The visual counter exists only in
this test robot, so browser measurements count unique displayed source frames.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'packages/policy-sdk/src'), str(ROOT / 'tests')]

import numpy as np
from PIL import Image, ImageDraw
from config_helpers import read_config
from project_templates import copy_templates
from robot_control.controller import RobotController
from robot_control.robots.mock import MockRobot
from robot_control.mcp_server import MCPService


class PreviewTestRobot(MockRobot):
    def __init__(self, config=None):
        super().__init__(config)
        self.counter = None
        self.samples = {}
        self.backgrounds = {}
        # Deliberately synthetic scene with representative detail, never a
        # generated camera photograph presented as a live robot observation.
        for i, name in enumerate(('head', 'left_wrist', 'right_wrist')):
            rng = np.random.default_rng(120 + i)
            pixels = np.clip(rng.normal(167, 18, (480, 640, 3)), 0, 255).astype(np.uint8)
            picture = Image.fromarray(pixels)
            draw = ImageDraw.Draw(picture)
            for x in range(0, 640, 40): draw.line((x, 0, x, 480), fill=(110, 115, 125), width=1)
            for y in range(0, 480, 40): draw.line((0, y, 640, y), fill=(110, 115, 125), width=1)
            draw.rectangle((160 + i * 15, 150, 350, 370), fill=(65 + i * 30, 110, 135 + i * 30))
            draw.ellipse((400, 170, 540, 310), fill=(185, 100 + i * 25, 65))
            draw.text((22, 26), 'MOCK CAMERA / ' + name.upper(), fill='white')
            self.backgrounds[name] = np.array(picture)

    def _camera_snapshot_locked(self):
        if self._camera_epoch is None:
            return {}
        tick = max(0, int((time.monotonic() - self._camera_epoch) * 30))
        if tick != self.counter:
            self.counter = tick
            self.samples = {}
            for camera, background in self.backgrounds.items():
                frame = background.copy()
                for bit in range(24):
                    frame[:12, bit * 6:(bit + 1) * 6] = 240 if (tick + 1) & (1 << bit) else 16
                frame.flags.writeable = False
                self.samples[camera] = {'image': frame, 'sequence': tick + 1,
                    'received': self._camera_epoch + tick / 30,
                    'timestamp': self._camera_wall_epoch + tick / 30}
        return self.samples


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18765)
    parser.add_argument('--api-port', type=int, default=18766)
    parser.add_argument('--empty-policy', action='store_true')
    parser.add_argument('--config', type=Path, help='Explicit isolated mock deployment config; enables real dynamic containers')
    args = parser.parse_args()
    requests = []
    with tempfile.TemporaryDirectory(prefix='rc-webui-harness-') as folder:
        root = Path(folder)
        if args.config:
            path = args.config.resolve()
            config = read_config(path)
            if config['robot'].get('backend') != 'mock':
                raise ValueError('WebUI harness accepts only an explicit mock deployment')
        else:
            copy_templates(ROOT / 'policy/example', root / 'policy/example')
            copy_templates(ROOT / 'policy/example_chain', root / 'policy/example_chain')
            config = read_config(ROOT / 'configs/rc.mock.yaml.example')
            config.update(policy_directory=str(root / 'policy'), outputs_directory=str(root / 'outputs'),
                startup_policy=None if args.empty_policy else 'example',
                global_state={'path': str(root / 'global.env')},
                timeouts=dict(initialize_s=10, inference_s=5, release_s=3, shutdown_s=15))
            config['robot']['image_size'] = [480, 640]
            path = root / 'rc.yaml'
            path.write_text(json.dumps(config))
        robot = PreviewTestRobot(config['robot'])
        if args.config:
            controller = RobotController(path, robot=robot)
        else:
            from host_helpers import LocalPolicyProcess
            controller = RobotController(path, robot=robot, process_factory=LocalPolicyProcess)
        service = MCPService(controller, {'host': args.host, 'port': args.port})

        def command(line):
            result = controller.wait_operation(controller.submit(line), 15)
            if result['status'] != 'succeeded': raise RuntimeError(str(result))
            return result

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def reply(self, value, status=200, cors=True):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                if cors: self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                try: self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass
            def do_OPTIONS(self):
                self.send_response(403 if self.path == '/no-cors' else 204)
                if self.path != '/no-cors':
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Access-Control-Allow-Headers', 'content-type, authorization')
                    self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.end_headers()
            def do_GET(self):
                if self.path == '/health': self.reply({'ready': True})
                elif self.path == '/test/requests': self.reply(requests)
                elif self.path == '/test/clock': self.reply({'camera_wall_epoch': robot._camera_wall_epoch, 'now': time.time()})
                else: self.reply({'error': 'not found'}, 404)
            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length) or b'{}')
                if self.path == '/test/pause':
                    self.reply(command('pause'))
                    return
                if self.path == '/test/reset':
                    for line in ('record off', 'pause', 'switch example', 'return', 'pause'):
                        command(line)
                    requests.clear()
                    self.reply(controller.status())
                    return
                text = body.get('messages', [{}])[-1].get('content', '')
                requests.append({'text': text, 'model': body.get('model')})
                if '慢' in text: time.sleep(.5)
                if '超时' in text: time.sleep(2)
                if 'HTTP错误' in text:
                    self.reply({'error': 'fixture'}, 500)
                    return
                if '非法' in text: steps = [{'action': 'reset'}, {'action': 'quit'}]
                elif '未知策略' in text: steps = [{'action': 'switch', 'policy': 'not-installed'}]
                elif '停止录制' in text: steps = [{'action': 'record', 'mode': 'off'}]
                elif '录制' in text: steps = [{'action': 'record', 'mode': 'on'}]
                elif '复位' in text: steps = [{'action': 'reset'}]
                elif '切换自链' in text: steps = [{'action': 'switch', 'policy': 'example_chain'}]
                elif '继续' in text: steps = [{'action': 'resume'}]
                else: steps = [{'action': 'status'}]
                content = 'bad json' if '坏JSON' in text else json.dumps({'message': text, 'steps': steps}, ensure_ascii=False)
                self.reply({'choices': [{'message': {'content': content}}]}, cors=self.path != '/no-cors')

        api = ThreadingHTTPServer((args.host, args.api_port), Handler)
        api.daemon_threads = True
        api_thread = threading.Thread(target=api.serve_forever, daemon=True)
        def stop(*_): controller.submit('exit')
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            controller.start()
            service.start()
            api_thread.start()
            print(json.dumps({'mcp_port': args.port, 'api_port': args.api_port, 'pid': __import__('os').getpid()}), flush=True)
            while not controller.done.wait(.1): pass
        finally:
            if not controller.done.is_set(): stop()
            controller.done.wait(20)
            service.close()
            api.shutdown()
            api.server_close()
            if not args.config:
                from host_helpers import cleanup_hosts
                cleanup_hosts()
        return controller.exit_code


if __name__ == '__main__':
    raise SystemExit(main())
