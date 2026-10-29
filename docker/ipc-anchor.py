"""Initialize only the RPC volume, then permanently drop root privileges."""
import json
import os
from pathlib import Path
import signal
import time

def healthcheck():
    ready = json.loads(Path('/run/robot-control/anchor-ready.json').read_text())
    uid = int(Path('/proc/1/status').read_text().split('Uid:')[1].split()[0])
    if uid != ready['uid'] or uid != int(os.environ['RC_UID']) or uid <= 0:
        raise SystemExit('anchor UID does not match deployment')


def run():
    uid, gid = int(os.environ['RC_UID']), int(os.environ['RC_GID'])
    if uid <= 0 or gid < 0:
        raise SystemExit('RC_UID must be non-root and RC_GID nonnegative')
    root = Path('/run/robot-control')
    os.chown(root, uid, gid)
    os.chmod(root, 0o700)
    # Never traverse bind mounts or symlinks; this is the dedicated named volume.
    for item in root.iterdir():
        if item.is_file() or item.is_socket():
            os.chown(item, uid, gid, follow_symlinks=False)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    ready = root / 'anchor-ready.json'
    ready.write_text(json.dumps(dict(uid=os.getuid(), gid=os.getgid(), pid=os.getpid())))
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        time.sleep(.1)
    ready.unlink(missing_ok=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--healthcheck', action='store_true')
    args = parser.parse_args()
    healthcheck() if args.healthcheck else run()
