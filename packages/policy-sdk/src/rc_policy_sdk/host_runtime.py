"""One process containing all policy sessions; no per-policy subprocesses."""
import argparse
import socket
import threading

from .lifecycle import become_subreaper, cleanup_worker_children
from .wire import receive_message, send_message
from .worker import run_session


def run(fd):
    become_subreaper()
    upstream = socket.socket(fileno=fd)
    send_lock = threading.Lock()
    sessions = {}

    def emit(session_id, message):
        with send_lock:
            send_message(upstream, {'session_id': session_id, 'event': message})

    def forward(session_id, connection):
        try:
            while True:
                event = receive_message(connection)
                emit(session_id, event)
                if event['type'] == 'closed': return
        except (OSError, EOFError, ValueError) as exc:
            try: emit(session_id, {'type': 'fatal', 'error': str(exc)})
            except OSError: pass

    try:
        while True:
            message = receive_message(upstream)
            if message['type'] == 'exit': break
            identity = message['session_id']
            if message['type'] == 'load':
                if identity in sessions:
                    raise ValueError('duplicate session')
                dispatch, worker = socket.socketpair()
                sessions[identity] = dispatch
                thread = threading.Thread(target=run_session,
                    args=(worker, message['directory'], 'rc_policy_' + identity), daemon=True)
                thread.start()
                threading.Thread(target=forward, args=(identity, dispatch), daemon=True).start()
                send_message(dispatch, message['event'])
            else:
                send_message(sessions[identity], message['event'])
    except (EOFError, OSError):
        pass
    finally:
        for connection in sessions.values():
            try: connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            connection.close()
        upstream.close()
        cleanup_worker_children()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fd', type=int, required=True)
    run(parser.parse_args().fd)


if __name__ == '__main__': main()
