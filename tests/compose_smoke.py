"""Exercise production Compose TUI attach, headless, persistence and stop.

Build production images first, then run with any Python >=3.10 on the Docker host.
Uses a unique project, temporary bind mounts and a free localhost port.
"""
import json
import os
import pty
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def wait(callback, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = callback()
            if result: return result
        except (OSError, ValueError):
            pass
        time.sleep(.1)
    raise AssertionError('Compose smoke condition timed out')


def main():
    ros_sim = '--ros-sim' in sys.argv[1:]
    config_file = 'rc.ros-test.json' if ros_sim else 'rc.mock.json'
    project = 'rc-smoke-' + uuid.uuid4().hex[:8]
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
    # Ghostty over SSH may retain TERM but lose COLORTERM. Image IDs must still
    # survive Textual's color encoding inside the actual Compose environment.
    environment = {**os.environ, 'RC_MCP_PORT':str(port), 'TERM':'xterm-ghostty', 'COLORTERM':'',
                   'ROBOT_CONTROL_MCP_TOKEN':'', 'ROS_DOMAIN_ID':'94'}
    with tempfile.TemporaryDirectory(prefix='rc-compose-smoke-') as temporary:
        root = Path(temporary)
        shutil.copytree(ROOT/'configs', root/'configs')
        shutil.copytree(ROOT/'policy', root/'policy')
        for name in ('outputs', 'snapshot'): (root/name).mkdir()
        volumes = [f'{root}/configs:/workspace/configs', f'{root}/policy:/workspace/policy',
                   f'{root}/outputs:/workspace/outputs', f'{root}/snapshot:/data/snapshot']
        override = root/'smoke.json'
        override.write_text(json.dumps({'services':{
            'robot-control':{'volumes':volumes},
            'policy-hold':{'volumes':[f'{root}/policy:/workspace/policy:ro']}}}))
        compose = ['docker','compose','-p',project,'-f',str(ROOT/'compose.yaml')]
        if ros_sim: compose += ['--profile','ros-sim','-f',str(ROOT/'compose.ros-sim.yaml')]
        compose += ['-f',str(override)]
        def run(*args):
            return subprocess.run([*compose,*args], cwd=ROOT, env=environment,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True, timeout=120).stdout
        def health():
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2) as response:
                return json.load(response)
        def clean_runtime():
            output = run('exec','-T','policy-hold','python','-c',
                'from pathlib import Path; assert not list(Path("/dev/shm").glob("rc*")); '
                'assert not any(b"rc_policy_sdk.host_runtime" in p.read_bytes() '
                'for p in Path("/proc").glob("[0-9]*/cmdline") if p.parent.name != str(__import__("os").getpid()))')
            return output
        attached = headless = None
        terminal = None
        try:
            run('up','-d','--no-build','robot-control')
            assert wait(lambda:health().get('state')=='paused')
            assert health()['consumed']==0
            try:
                color_check = run('exec', '-T', 'robot-control', 'python3', '-c', '''
from types import SimpleNamespace
from textual.strip import Strip
from robot_control.graphics import GraphicsSupport, kitty_placeholder_lines
from robot_control.tui import RobotControlApp
app = RobotControlApp(SimpleNamespace(config={}), start_controller=False,
                      graphics=GraphicsSupport('kitty'))
row = kitty_placeholder_lines(0x123456, (1, 1))[0]
rendered = Strip([row], 1).render(app.console)
assert '\\x1b[38;2;18;52;86m' in rendered, (app.console.color_system, repr(rendered))
print('Compose Kitty placeholder RGB image ID preserved: PASS')
''')
                print(color_check.strip(), flush=True)
            except subprocess.CalledProcessError as exc:
                print(exc.stdout, file=sys.stderr, flush=True)
                raise
            terminal, slave = pty.openpty()
            attached = subprocess.Popen([*compose,'attach','--sig-proxy=false','robot-control'],
                cwd=ROOT,env=environment,stdin=slave,stdout=slave,stderr=slave)
            os.close(slave)
            def drain_terminal():
                try:
                    while os.read(terminal, 65536): pass
                except OSError: pass
            threading.Thread(target=drain_terminal, daemon=True).start()
            time.sleep(.5)
            assert attached.poll() is None, 'compose attach failed'
            os.write(terminal, b'resume\r')
            wait(lambda:health()['consumed']>=2)
            os.write(terminal, b'pause\r')
            wait(lambda:health()['state']=='paused')
            os.write(terminal, b'snapshot\r')
            wait(lambda:list((root/'snapshot').glob('*/state.json')))
            run('stop','robot-control')
            container = run('ps','-a','-q','robot-control').strip()
            inspect = subprocess.check_output(['docker','inspect',container],text=True)
            assert json.loads(inspect)[0]['State']['ExitCode']==0
            attached.wait(10)
            clean_runtime()
            print('TUI attach/resume/pause/snapshot and Compose SIGTERM cleanup: PASS', flush=True)

            lines, transcript = queue.Queue(), []
            headless = subprocess.Popen([*compose,'run','--rm','-T','--no-deps','robot-control',
                'python3','-m','robot_control','--headless','--config',f'/workspace/configs/{config_file}',
                '--mcp','0.0.0.0:4000'],cwd=ROOT,env=environment,stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
            def read():
                for line in headless.stdout:
                    transcript.append(line); lines.put(line)
            threading.Thread(target=read,daemon=True).start()
            def seen(text): return any(text in line for line in transcript)
            wait(lambda:seen('等待 resume'))
            headless.stdin.write('resume\n'); headless.stdin.flush()
            time.sleep(.5)
            headless.stdin.write('pause\nsnapshot\nsave-config rc\nsave-config policy\n'); headless.stdin.flush()
            wait(lambda:seen('snapshot 已保存'))
            wait(lambda:sum('有效配置已保存' in line for line in transcript)==2)
            headless.stdin.write('exit\n'); headless.stdin.flush()
            assert headless.wait(20)==0, ''.join(transcript)
            snapshots = list((root/'snapshot').glob('*/state.json'))
            assert len(snapshots)==2
            for state_file in snapshots:
                assert len(list(state_file.parent.glob('*.png')))==3
                assert len(json.loads(state_file.read_text())['images'])==3
            assert json.loads((root/'configs'/config_file).read_text())['schema_version']==2
            clean_runtime()
            print('Headless commands/atomic config saves/persistent snapshots and clean exit: PASS', flush=True)
        finally:
            run('down','-v','--remove-orphans')
            for process in (attached,headless):
                if process is not None and process.poll() is None:
                    process.terminate(); process.wait(10)
            if terminal is not None: os.close(terminal)


if __name__ == '__main__': main()
