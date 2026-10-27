#!/usr/bin/env python3
"""Base deployment entry and owner-aware shutdown of dynamic policy projects."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from config_files import read_json


def compose_command(root, arguments):
    root=Path(root).resolve()
    context=read_json(root/'.setup/runtime.json')
    if context.get('version')!=3:
        raise ValueError('run setup for schema 3 first')
    if any(arg.startswith(('--env-file','--project-directory','--project-name')) or arg=='-p' for arg in arguments):
        raise ValueError('deployment paths and project name are managed by setup')
    arguments=list(arguments)
    for index,argument in enumerate(arguments):
        if index and arguments[index-1] in ('-f','--file') and not Path(argument).is_absolute():
            arguments[index]=str(root/argument)
    return ['docker','compose','-p',context['base_project'],'--project-directory',str(root),
            '--env-file',str(root/'.env'),'--env-file',str(root/'.setup/base.env'),
            '-f',str(root/'compose.yaml'),*arguments]


def down(root, arguments):
    # The one host deadline includes RC stop, fallback, external owners and base down.
    deadline=time.monotonic()+125
    sys.path[:0]=[str(root/'src'),str(root/'packages/policy-sdk/src')]
    from robot_control.runtime_manager import ComposeRuntimeManager, docker_command, remaining
    from robot_control.external import ExternalResources
    context=read_json(root/'.setup/runtime.json')
    context['client_root']=str(root)
    manager=ComposeRuntimeManager(context,root/'outputs/runtime')
    rc_ids=manager._ids({'com.docker.compose.project':context['base_project'],
                         'com.docker.compose.service':'robot-control'},deadline)
    base_ids=manager._ids({'com.docker.compose.project':context['base_project']},deadline)
    # Preserve overlays/options preceding down for the stop operation too.
    prefix=arguments[:arguments.index('down')]
    docker_command(compose_command(root,[*prefix,'stop','--timeout','120','robot-control'])[1:],
                   timeout=remaining(deadline),cwd=root)
    stopped=manager._inspect(rc_ids,deadline)
    if any(item['State'].get('Running') for item in stopped):
        raise RuntimeError('RC stop unconfirmed; retain anchor')
    rc_failed=any(item['State'].get('ExitCode',1)!=0 for item in stopped)
    try:
        manager.acquire(deadline)
        manager.cleanup_owned(deadline)
        remote_forced=ExternalResources.recover(root/'outputs/recovery',deadline,context['deployment_id'],
                                                defer_namespace_cleanup=True)
        anchors={data['anchor_id'] for _,data in manager.pool_records()}
        for path in (root/'outputs/recovery').glob('*.json'):
            identity=json.loads(path.read_text()).get('anchor_id')
            if identity:anchors.add(identity)
        anchors.update(item['Id'] for item in manager._inspect(base_ids,deadline)
                       if item['Config'].get('Labels',{}).get('com.docker.compose.service')=='ipc-anchor')
        docker_command(compose_command(root,arguments)[1:],timeout=remaining(deadline),cwd=root)
        manager.check_daemon(deadline)
        if anchors & set(manager._ids({},deadline)):
            raise RuntimeError('anchor removal unconfirmed; retain external recovery journals')
        ExternalResources.finish_namespace_cleanup(root/'outputs/recovery',context['deployment_id'],anchors)
        manager.finish_pool_namespace_cleanup(anchors)
        return 1 if rc_failed or manager.cleanup_forced or remote_forced else 0
    finally:
        manager.release()


def main():
    root=Path(__file__).resolve().parents[1]
    try:
        arguments=sys.argv[1:]
        if 'down' in arguments and not any(arg in ('--dry-run','--help') for arg in arguments):
            raise SystemExit(down(root,arguments))
        command=compose_command(root,arguments)
        os.chdir(root)
        os.execvp(command[0],command)
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as exc:
        print(f'compose error: {exc}',file=sys.stderr)
        raise SystemExit(1)


if __name__=='__main__':
    main()
