"""Local HTTP model substitute used by SDK, worker and container tests."""
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


def response_for(provider, decision=None):
    decision = decision or {'left': 'up', 'right': 'noop', 'done': False}
    if provider == 'openai_decisions':
        choice = 'done' if decision['done'] else decision['left'] + '|' + decision['right']
        return {'id': 'decision-test', 'object': 'decision', 'created_at': 1, 'model': 'test-vision',
                'answers': [{'type': 'choice', 'name': 'next_action', 'choice': choice,
                             'confidence': .9, 'probabilities': [{'value': choice, 'probability': .9}]}],
                'usage': {'input_tokens': 10, 'output_tokens': 1, 'total_tokens': 11}}
    content = json.dumps(decision)
    if provider == 'openai':
        return {'id': 'resp-test', 'object': 'response', 'created_at': 1, 'model': 'test-vision',
                'status': 'completed', 'error': None, 'incomplete_details': None,
                'output': [{'type': 'message', 'id': 'msg-test', 'status': 'completed', 'role': 'assistant',
                            'content': [{'type': 'output_text', 'text': content, 'annotations': []}]}],
                'usage': {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20}}
    return {'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 1, 'model': 'test-vision',
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': content, 'refusal': None}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}


@contextmanager
def model_server(host='127.0.0.1', port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self, value):
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            self.reply(self.server.requests if self.path == '/requests' else {'healthy': True})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/control':
                self.server.requests.clear()
                self.server.responses.clear()
                self.server.responses.extend(body.get('responses', []))
                self.server.received.clear()
                self.reply({'ok': True})
                return
            self.server.requests.append({'path': self.path, 'body': body})
            item = self.server.responses.popleft() if self.server.responses else {}
            self.server.received.set()
            if item.get('wait'):
                item['wait'].wait(20)
            if item.get('delay'):
                time.sleep(item['delay'])
            if item.get('disconnect'):
                self.close_connection = True
                return
            provider = ('openai_decisions' if self.path.endswith('/decisions') else
                        'openai' if self.path.endswith('/responses') else 'deepseek')
            payload = item.get('body', response_for(provider))
            data = json.dumps(payload).encode()
            try:
                self.send_response(item.get('status', 200))
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer((host, port), Handler)
    server.requests, server.responses, server.received = [], deque(), threading.Event()
    server.url = f'http://127.0.0.1:{server.server_port}/v1'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
