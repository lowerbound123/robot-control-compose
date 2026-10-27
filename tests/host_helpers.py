"""Test-owned host lifecycle; production never launches a local policy."""
from pathlib import Path
import shutil
import tempfile

from robot_control.process import PolicyProcess as RemotePolicyProcess
from test_host_transport import Host

_hosts = {}


class LocalPolicyProcess(RemotePolicyProcess):
    def __init__(self, directory, config, version=1, *, python_command=None, **kwargs):
        identity = config.setdefault('id', 'test-policy')
        host_id = identity
        config.setdefault('schema_version', 3)
        execution = config.setdefault('execution', {})
        for key,value in dict(mode='sync',call_every_actions=1,control_mode='qpos').items():
            execution.setdefault(key,value)
        config.setdefault('params', {})
        if host_id not in _hosts:
            temporary = tempfile.TemporaryDirectory(prefix='rcp-', dir='/tmp')
            _hosts[host_id] = (temporary, None)
        temporary, host = _hosts[host_id]
        destination = Path(temporary.name) / identity
        shutil.copytree(directory, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
        if host is None:
            host = Host(temporary.name, host_id)
            _hosts[host_id] = (temporary, host)
        kwargs.pop('endpoint', None)
        kwargs.pop('exit_authority', None)
        super().__init__(directory, config, version, host_socket=host.socket, **kwargs)


def cleanup_hosts():
    from robot_control.host_client import HostClient
    for client in list(HostClient._clients.values()): client.disconnect()
    for temporary, host in _hosts.values():
        if host: host.close()
        temporary.cleanup()
    _hosts.clear()
