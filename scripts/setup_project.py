#!/usr/bin/env python3
"""Prepare schema 3 configuration and images; never launch a controller or policy."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.parse import urlparse

from config_files import configuration_updates, merge_rc_env, read_json, write_updates
from config_validation import validate_documents, load

ROOT = Path(__file__).resolve().parents[1]


def run_command(command, root, *, capture=False, env=None):
    return subprocess.run(command, cwd=root, check=True, text=True, env=env,
                          stdout=subprocess.PIPE if capture else None)


def select_policies(root, selected):
    catalog = load('packages/policy-sdk/src/rc_policy_sdk/catalog.py')
    records = {r['id']:r for r in catalog.discover(Path(root)/'policy')}
    result = []
    for identity in selected:
        if identity not in records or not records[identity]['valid']:
            raise ValueError(f'{identity}: invalid or unconfigured policy')
        config = catalog.read_descriptor(Path(root)/'policy'/identity)['config']
        if config.get('kind') == 'chain':
            for member in config['policies']:
                if member not in result:
                    result.append(member)
        elif identity not in result:
            result.append(identity)
    return result


def build_fingerprint(build):
    value = dict(build)
    context = Path(value['context']).resolve()
    value['context'] = str(context)
    if 'dockerfile_inline' not in value:
        file = context / value.pop('dockerfile', 'Dockerfile')
        value['dockerfile_contents'] = hashlib.sha256(file.read_bytes()).hexdigest()
        ignore = Path(str(file) + '.dockerignore')
        if not ignore.is_file():
            ignore = context / '.dockerignore'
        value['effective_dockerignore'] = hashlib.sha256(ignore.read_bytes() if ignore.is_file() else b'').hexdigest()
    contexts = value.get('additional_contexts', {})
    if isinstance(contexts, list):
        contexts = dict(item.split('=',1) for item in contexts)
    effective = {}
    for name, location in contexts.items():
        folder = Path(location)
        if folder.is_dir():
            effective[name] = [(p.relative_to(folder).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
                               for p in sorted(folder.rglob('*')) if p.is_file()]
        else:
            effective[name] = location
    value['additional_contexts'] = effective
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def docker_endpoint(root):
    endpoint = os.environ.get('DOCKER_HOST')
    if not endpoint:
        data = json.loads(run_command(['docker','context','inspect'],root,capture=True).stdout)
        endpoint = data[0]['Endpoints']['docker']['Host']
    parsed = urlparse(endpoint)
    if parsed.scheme != 'unix':
        raise ValueError('only a local Unix Docker endpoint is supported')
    path = Path(parsed.path).resolve()
    if not path.is_socket():
        raise ValueError('Docker endpoint is not a Unix socket')
    return str(path), path.stat().st_gid


def prepare_context(root, *, config_only):
    path = root/'.setup/runtime.json'
    old = read_json(path) if path.is_file() else {}
    if old.get('host_root') != str(root):
        old = {}
    socket, gid = docker_endpoint(root)
    identity = old.get('deployment_id') or uuid.uuid4().hex
    context = dict(version=3, deployment_id=identity, base_project=old.get('base_project', 'rc-'+identity[:12]),
        host_root=str(root), client_root='/workspace', rpc_volume=old.get('rpc_volume','rc-'+identity[:12]+'-rpc'),
        docker_socket=socket, docker_gid=gid, uid=os.getuid(), gid=os.getgid())
    if not config_only:
        context['daemon_id'] = run_command(['docker','info','--format','{{.ID}}'],root,capture=True).stdout.strip()
    elif old.get('daemon_id'):
        context['daemon_id'] = old['daemon_id']
    return context


def policy_compose(root, identity):
    folder = root/'policy'/identity
    command = ['docker','compose','--project-directory',str(folder), '--env-file',
               str(folder/'deployment.env') if (folder/'deployment.env').is_file() else '/dev/null']
    command += ['-f',str(folder/'compose.yaml')]
    return command


def prepare_images(root, policies, context):
    from compose_project import compose_command
    run_command(compose_command(root,['build','ipc-anchor','robot-control']),root)
    seen = {}
    env = {k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CERT_PATH','DOCKER_TLS_VERIFY')}
    env.update(RC_HOST_PROJECT_ROOT=str(root), RC_UID=str(context['uid']), RC_GID=str(context['gid']))
    for identity in policies:
        command = policy_compose(root,identity)
        model = json.loads(run_command(command+['config','--format','json'],root,capture=True,env=env).stdout)
        if set(model.get('services',{})) != {'policy'}:
            raise ValueError(f'{identity}: expected exactly one service named policy')
        service = model['services']['policy']
        image = service.get('image')
        if not image:
            raise ValueError(f'{identity}: image must be explicit')
        build = service.get('build')
        if build:
            fingerprint = build_fingerprint(build)
            if image in seen and seen[image] != fingerprint:
                raise ValueError('same image tag has different effective build inputs')
            if image not in seen:
                run_command(command+['build','policy'],root,env=env)
            seen[image] = fingerprint
        else:
            try:
                run_command(['docker','image','inspect',image],root,capture=True)
            except subprocess.CalledProcessError:
                run_command(command+['pull','policy'],root,env=env)


def setup(root, policies=None, *, config_only=False):
    root = Path(root).resolve()
    if os.getuid() == 0:
        raise ValueError('run setup as the normal host user, not root')
    changes, documents, notices = configuration_updates(root)
    validate_documents(documents)
    for template in (root/'policy').glob('*/deployment.env.example'):
        if not template.with_suffix('').exists():
            changes.append((template.with_suffix(''),template.read_bytes()))
    context = prepare_context(root, config_only=config_only)
    env_path = root/'.env'
    existing = env_path.read_text() if env_path.exists() else ''
    changes.append((env_path,merge_rc_env((root/'.env.example').read_text(),existing,os.getuid(),os.getgid()).encode()))
    changes.append((root/'.setup/runtime.json',(json.dumps(context,indent=2)+'\n').encode()))
    managed = dict(RC_UID=context['uid'], RC_GID=context['gid'], RC_DEPLOYMENT_ID=context['deployment_id'],
                  RC_BASE_PROJECT=context['base_project'], RC_RPC_VOLUME=context['rpc_volume'],
                  RC_HOST_PROJECT_ROOT=str(root), RC_DOCKER_SOCKET=context['docker_socket'], RC_DOCKER_GID=context['docker_gid'])
    # Single quotes make $ literal for Compose; do not shell-source this file.
    def quote(value):
        return "'"+str(value).replace("'", "\\'")+"'"
    changes.append((root/'.setup/base.env',''.join(f'{k}={quote(v)}\n' for k,v in managed.items()).encode()))
    write_updates(root,changes)
    for name in ('outputs/runtime','snapshot'):
        (root/name).mkdir(parents=True,exist_ok=True)
    catalog=load('packages/policy-sdk/src/rc_policy_sdk/catalog.py')
    requested = policies if policies is not None else [r['id'] for r in catalog.discover(root/'policy') if r['valid']]
    selected=select_policies(root,requested)
    if not config_only:
        prepare_images(root,selected,context)
    for notice in notices:
        print(notice)
    print('配置已准备；基础启动仅 RC 和 ipc-anchor。' if config_only else '镜像与配置已准备；尚未启动容器。')
    return selected


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policies',help='comma-separated discovered policy IDs; default: all configured policies')
    parser.add_argument('--config-only',action='store_true')
    args=parser.parse_args()
    try:
        setup(ROOT, None if args.policies is None else [p for p in args.policies.split(',') if p],config_only=args.config_only)
    except (OSError,ValueError,subprocess.CalledProcessError) as exc:
        print(f'setup failed: {exc}',file=sys.stderr)
        raise SystemExit(1)


if __name__=='__main__':
    main()
