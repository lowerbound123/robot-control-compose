"""A bound path is not proof that the test-owned host is listening yet."""
import socket

from test_host_transport import Host
import test_host_transport


def test_test_host_waits_until_socket_accepts_connections(tmp_path, monkeypatch):
    folder = tmp_path / "policy"
    folder.mkdir()
    (folder / "main.py").write_text("")
    real_popen = test_host_transport.subprocess.Popen
    delayed_listen = '''
import runpy, socket, time
original = socket.socket.listen
def delayed(self, *args, **kwargs):
    time.sleep(.3)
    return original(self, *args, **kwargs)
socket.socket.listen = delayed
runpy.run_module("rc_policy_sdk.host", run_name="__main__")
'''

    def launch(args, **kwargs):
        assert args[1:3] == ["-m", "rc_policy_sdk.host"]
        return real_popen([args[0], "-c", delayed_listen, *args[3:]], **kwargs)

    monkeypatch.setattr(test_host_transport.subprocess, "Popen", launch)
    host = Host(tmp_path)
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(1)
            connection.connect(host.socket)
    finally:
        host.close()
