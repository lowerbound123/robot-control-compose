#!/usr/bin/env python3
"""Small deployment commands backed by the existing owner-aware Compose entry."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import compose_project
from config_files import read_json


def compose_files(root, names):
    """The base is implicit; additional files retain the caller's order."""
    arguments = []
    for name in names:
        path = (root / name).resolve()
        if not path.is_file():
            raise ValueError(f'Compose file not found: {name}')
        if path != root / 'compose.yaml':
            arguments.extend(['-f', str(path)])
    return arguments


def services(root, path):
    # Inspect only this file, without requiring other policies' deployment secrets.
    # Compose itself handles its YAML tags, anchors and service definitions.
    command = ['docker', 'compose', '-p', 'rc-build-discovery', '--project-directory',
               str(path.parent), '--env-file', '/dev/null', '-f', str(path),
               'config', '--format', 'json', '--no-interpolate',
               '--no-env-resolution', '--no-consistency']
    result = subprocess.run(command, cwd=root, check=True, text=True, stdout=subprocess.PIPE)
    return json.loads(result.stdout).get('services', {})


def build_environment(root):
    context = read_json(root / '.setup/runtime.json')
    if context.get('version') != 3:
        raise ValueError('run setup for schema 3 first')
    env = {key: value for key, value in os.environ.items() if key in (
        'PATH', 'HOME', 'LANG', 'DOCKER_HOST', 'DOCKER_CONTEXT',
        'DOCKER_CERT_PATH', 'DOCKER_TLS_VERIFY')}
    env.update(RC_HOST_PROJECT_ROOT=str(root), RC_UID=str(context['uid']),
               RC_GID=str(context['gid']))
    return env


def build_command(root, target):
    if target.startswith('connector:'):
        source = target.removeprefix('connector:')
        if not re.fullmatch(r'[a-z][a-z0-9_-]*', source):
            raise ValueError('connector target must be connector:<source>')
        path = root / 'connector' / source / 'compose.yaml'
        if not path.is_file():
            raise ValueError(f'{target}: connector Compose file not found')
        model = services(root, path)
        if set(model) != {'connector'} or model['connector'].get('build') is None:
            raise ValueError(f'{target}: expected one buildable service named connector')
        return (['docker', 'compose', '--project-directory', str(path.parent), '--env-file', '/dev/null',
                 '-f', str(path), 'build', 'connector'], build_environment(root))
    target = 'robot-control' if target == 'rc' else target
    base = root / 'compose.yaml'
    if services(root, base).get(target, {}).get('build') is not None:
        return compose_project.compose_command(root, ['build', target]), os.environ.copy()

    # All policy Compose files call their service "policy"; the source directory
    # is the unambiguous public build name, just as it is for setup --policies.
    if re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,47}', target):
        policy_file = root / 'policy' / target / 'compose.yaml'
        if policy_file.is_file():
            model = services(root, policy_file)
            if set(model) != {'policy'} or model['policy'].get('build') is None:
                raise ValueError(f'{target}: expected one buildable service named policy')
            from setup_project import policy_compose
            return policy_compose(root, target) + ['build', 'policy'], build_environment(root)

    matches = []
    for path in sorted({*root.glob('compose*.yaml'), *root.glob('compose*.yml')} - {base}):
        if services(root, path).get(target, {}).get('build') is not None:
            matches.append(path)
    if not matches:
        raise ValueError(f'{target}: no build definition found in Compose files; '
                         'use a service name (rc, ipc-anchor, webui), policy source name or connector:<source>')
    if len(matches) > 1:
        raise ValueError(f'{target}: multiple build definitions: ' + ', '.join(p.name for p in matches)
                         + '; select the files explicitly with scripts/compose.sh')
    return compose_project.compose_command(root, ['-f', str(matches[0]), 'build', target]), os.environ.copy()


def main(arguments=None, *, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    up = commands.add_parser('up', help='按顺序合并 Compose 文件并后台启动')
    up.add_argument('files', nargs='*', help='相对仓库根目录或绝对路径；基础 compose.yaml 自动加入')
    build = commands.add_parser('build', help='自动查找并构建指定服务、策略来源或 connector:<来源>')
    build.add_argument('target')
    commands.add_parser('down', help='安全清理当前部署的全部容器及动态策略')
    commands.add_parser('attach', help='连接 RC 终端')
    args = parser.parse_args(arguments)
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    try:
        if args.action == 'down':
            return compose_project.down(root, ['down', '--remove-orphans'])
        if args.action == 'build':
            command, env = build_command(root, args.target)
        else:
            tail = (compose_files(root, args.files) + ['up', '-d'] if args.action == 'up'
                    else ['attach', 'robot-control'])
            command, env = compose_project.compose_command(root, tail), os.environ.copy()
        os.chdir(root)
        os.execvpe(command[0], command, env)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'{args.action} error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
