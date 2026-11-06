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
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'packages/policy-sdk/src'),
                str(ROOT / 'packages/connector-sdk/src'), str(ROOT / 'tests')]

import numpy as np
from config_helpers import read_config
from project_templates import copy_templates
from robot_control.controller import RobotController
from connector_helpers import MockRobot
from robot_control.mcp_server import MCPService


def model_fixture(body):
    """Deterministic decisions driven by the same live context as a real model."""
    messages = body.get('messages', [])
    context, exchanges = {}, []
    text = messages[-1].get('content', '') if messages else ''
    for index, message in enumerate(messages):
        try:
            data = json.loads(message.get('content', ''))
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get('kind') == 'live_capabilities_and_context':
            context = data
            text = messages[index + 1]['content']
        elif data.get('kind') == 'tool_result_data':
            exchanges.append(data)
    capabilities = context.get('capabilities', {})
    calls = [exchange['tool'] for exchange in exchanges]
    names = [call['name'] for call in calls]
    status = capabilities.get('status', {})
    def decision(name=None, arguments=None, message=None):
        return {'message': message or (text + (' · 已执行' if name is None else ' · ' + name)),
                'tool_call': None if name is None else {'name': name, 'arguments': arguments or {}}}
    if '非法' in text:
        value = decision('unregistered_tool')
    elif '未知策略' in text:
        value = decision('policy_command', {'instance_id': 'not-installed', 'command': 'set-message', 'args': ['测试']})
    elif '重复实例' in text:
        value = decision(message='请明确选择 example-0 还是 example-1。')
    elif '设置消息' in text or '刚才那个策略' in text or '链后设置' in text:
        instances = [row['instance_id'] for row in capabilities.get('policy_commands', {}).get('policies', []) if row.get('loaded')]
        target = 'example-1' if 'example-1' in instances else next(iter(instances), None)
        if 'show' not in names:
            value = decision('show', {'scope': 'policy'})
        elif status.get('control_owner') != 'mcp':
            value = decision('take')
        elif '链后设置' in text and 'policy_chain' not in names:
            value = decision('policy_chain', {'policies': ['example', 'example']})
        elif 'policy_command' not in names:
            value = decision('policy_command', {'instance_id': target, 'command': 'set-message', 'args': ['浏览器验证消息']})
        elif names.count('show') < 2:
            value = decision('show', {'scope': 'policy'})
        else:
            value = decision(message=f'{target} 消息已更新并查询核实 · 已执行')
    elif '任务完成' in text or '任务终止' in text:
        instances = [row['instance_id'] for row in capabilities.get('policy_commands', {}).get('policies', []) if row.get('loaded')]
        if 'return_and_wait' in names:
            value = decision(message='策略任务已完成，已收到 completed 结果。')
        elif status.get('control_owner') != 'mcp':
            value = decision('take')
        else:
            value = decision('return_and_wait')
    elif '录制' in text:
        value = decision() if 'record' in names else decision('record', {'action': 'off' if '停止录制' in text else 'on'})
    elif '毫米' in text:
        if status.get('control_owner') != 'mcp':
            value = decision('take')
        elif 'send_command' not in names:
            value = decision('send_command', {'commands': 'left:up:0.001@slow'})
        elif 'state' not in names:
            value = decision('state')
        else:
            value = decision(message='已发送左臂上移一毫米的目标并读取反馈。')
    elif '复位' in text or '切换自链' in text or '继续' in text:
        operation = 'return' if '继续' in text else 'command'
        if operation in names:
            value = decision()
        elif status.get('control_owner') != 'mcp':
            value = decision('take')
        else:
            value = decision(operation, {} if operation == 'return' else {'line': 'switch example_chain' if '切换自链' in text else 'reset'})
    else:
        value = decision() if 'status' in names else decision('status')
    return text, context, exchanges, value


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
            pixels[:, ::40] = (110, 115, 125)
            pixels[::40, :] = (110, 115, 125)
            pixels[150:371, 160+i*15:351] = (65+i*30, 110, 135+i*30)
            yy, xx = np.ogrid[:480, :640]
            pixels[((xx-470)/70)**2 + ((yy-240)/70)**2 <= 1] = (185, 100+i*25, 65)
            self.backgrounds[name] = pixels

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


def prepare_preview_connector(directory):
    """Install a marked synthetic source only into an isolated test deployment."""
    import inspect
    import shutil
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / 'connector/mock/main.py', directory / 'mock_driver.py')
    (directory / 'main.py').write_text(
        'import sys, time\nfrom pathlib import Path\nimport numpy as np\n'
        'sys.path.insert(0, str(Path(__file__).parent))\nfrom mock_driver import MockRobot\n'
        + inspect.getsource(PreviewTestRobot) + '\ndef create_connector(config): return PreviewTestRobot(config)\n')


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
            if config['connectors'][config['robot']['connector']]['source'] != 'mock':
                raise ValueError('WebUI harness accepts only an explicit mock deployment')
        else:
            copy_templates(ROOT / 'policy/example', root / 'policy/example')
            copy_templates(ROOT / 'policy/example_chain', root / 'policy/example_chain')
            config = read_config(ROOT / 'configs/rc.mock.yaml.example')
            config.update(policy_directory=str(root / 'policy'), outputs_directory=str(root / 'outputs'),
                startup_policy=None if args.empty_policy else 'example',
                global_state={'path': str(root / 'global.env')},
                timeouts=dict(initialize_s=10, inference_s=5, release_s=3, shutdown_s=15))
            directory = root / 'connector/mock'
            prepare_preview_connector(directory)
            (directory / 'device.json').write_text(json.dumps({'image_size': [480, 640]}))
            config.update(connector_directory=str(root / 'connector'),
                          connectors={'body': {'source': 'mock', 'config': 'device.json'}}, robot={'connector': 'body'})
            path = root / 'rc.yaml'
            path.write_text(json.dumps(config))
        if args.config:
            controller = RobotController(path)
        else:
            from host_helpers import LocalPolicyProcess
            controller = RobotController(path, process_factory=LocalPolicyProcess)
        robot = controller.robot
        service = MCPService(controller, {'host': args.host, 'port': args.port})
        batch_gate, batch_held = threading.Event(), threading.Event()
        batch_gate.set()
        stream_gate = threading.Event()
        stream_gate.set()

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
                model_path = self.path in ('/no-cors', '/v1/chat/completions')
                self.send_response(403 if model_path else 204)
                if not model_path:
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Access-Control-Allow-Headers', 'content-type, authorization')
                    self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.end_headers()
            def do_GET(self):
                if self.path == '/health': self.reply({'ready': True})
                elif self.path == '/test/requests': self.reply(requests)
                elif self.path == '/test/clock':
                    frame = robot.recording_snapshot()['frames']['head']
                    self.reply({'camera_wall_epoch': frame['timestamp']-(frame['sequence']-1)/30, 'now': time.time()})
                elif self.path == '/test/batch-state':
                    self.reply({'held': batch_held.is_set(), 'config': controller.query('show policy')})
                else: self.reply({'error': 'not found'}, 404)
            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length) or b'{}')
                if self.path == '/test/stream-hold':
                    stream_gate.clear()
                    self.reply({'ready': True})
                    return
                if self.path == '/test/stream-release':
                    stream_gate.set()
                    self.reply({'ready': True})
                    return
                if self.path == '/test/pause':
                    self.reply(command('pause'))
                    return
                if self.path == '/test/policy-result':
                    # A local operator triggers the real example runtime result
                    # after the return/reset barrier, without changing ownership.
                    import shlex
                    outcome = body.get('outcome', 'completed')
                    if outcome not in ('completed', 'terminated'):
                        self.reply({'error': 'invalid outcome'}, 400)
                        return
                    self.reply(command(shlex.join(['policy', 'example', 'next-result', outcome, '浏览器任务结果'])))
                    return
                if self.path == '/test/batch-hold':
                    batch_gate.clear()
                    batch_held.clear()
                    worker = controller._worker
                    update = worker.update_config
                    def delayed(*args, **kwargs):
                        batch_held.set()
                        try:
                            if not batch_gate.wait(10):
                                raise RuntimeError('test batch gate timed out')
                            return update(*args, **kwargs)
                        finally:
                            worker.update_config = update
                    worker.update_config = delayed
                    self.reply({'ready': True})
                    return
                if self.path == '/test/batch-release':
                    batch_gate.set()
                    self.reply({'ready': True})
                    return
                if self.path == '/test/reset':
                    batch_gate.set()
                    stream_gate.set()
                    for line in ('record off', 'pause', 'switch example', 'return', 'pause'):
                        command(line)
                    requests.clear()
                    self.reply(controller.status())
                    return
                text, context, exchanges, decision = model_fixture(body)
                auth_valid = self.headers.get('Authorization') == 'Bearer local-test-only'
                requests.append({'text': text, 'model': body.get('model'), 'auth_valid': auth_valid,
                                 'stream': body.get('stream'),
                                 'effort': body.get('reasoning_effort'), 'context': context,
                                 'exchanges': exchanges, 'decision': decision,
                                 'origin': self.headers.get('Origin'), 'cookie': self.headers.get('Cookie'),
                                 'host': self.headers.get('Host'), 'path': self.path})
                if not auth_valid or '认证错误' in text:
                    self.reply({'error': 'local-test-only private-upstream'}, 401, cors=False)
                    return
                if '其他错误' in text:
                    self.reply({'error': 'local-test-only private-upstream'}, 418, cors=False)
                    return
                if '重定向' in text:
                    self.send_response(302)
                    self.send_header('Location', 'https://private-upstream.invalid/local-test-only')
                    self.end_headers()
                    return
                if '慢' in text: time.sleep(.5)
                if '超时' in text: time.sleep(2)
                if 'HTTP错误' in text:
                    self.reply({'error': 'local-test-only private-upstream'}, 500, cors=False)
                    return
                content = 'bad json' if '坏JSON' in text else json.dumps(decision, ensure_ascii=False)
                if body.get('stream') is True:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    def chunk(delta, finish=None):
                        value = {'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                        self.wfile.write(('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode())
                        self.wfile.flush()
                    try:
                        thinking = '正在核对当前状态与可用操作。\n完整确认指令后，再提交执行。'
                        if '长输出' in text:
                            thinking = '\n'.join(f'测试思考片段 {i + 1}：核对当前请求的执行条件。' for i in range(22))
                        chunk({'reasoning_content': thinking})
                        for offset in range(0, len(content), 6):
                            chunk({'content': content[offset:offset + 6]})
                            time.sleep(.005)
                        if '流式等待' in text:
                            stream_gate.wait(15)
                        if '流式中断' not in text:
                            chunk({}, 'stop')
                            self.wfile.write(b'data: [DONE]\n\n')
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                self.reply({'choices': [{'message': {'content': content}}]}, cors=False)

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
