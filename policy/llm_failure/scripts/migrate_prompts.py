#!/usr/bin/env python3
"""将 llm_failure 的旧任务和提示词一次性迁入各供应商 YAML。"""
import argparse
import os
from pathlib import Path
import sys


POLICY_ROOT = Path(__file__).resolve().parents[1]
ROOT = POLICY_ROOT.parents[1]
sys.path[:0] = [str(POLICY_ROOT / 'src'), str(ROOT / 'src'), str(ROOT / 'scripts')]

import config_files
from rc_llm_failure.decisions import DECISION_FIELDS
from rc_llm_failure.prompts import validate_prompt_fields
from robot_control.yaml_config import load_yaml_config, render_yaml_config


FACTORY = {'deepseek': 'provider.yaml', **{kind: f'providers/{kind}.yaml' for kind in DECISION_FIELDS}}
CONTAINER_FOLDER = Path('/workspace/policy/llm_failure')
ARTIFACT_DIRS = {'.git', '.setup', '.runtime', '.venv', 'venv', '__pycache__', '.pytest_cache',
                 '.ruff_cache', 'cache', '.cache', 'vendor', 'node_modules', 'build', 'dist',
                 'outputs', 'snapshot', 'models', 'weights', 'checkpoints'}
CONCURRENT_CHANGE = '文件在准备迁移期间发生变化。请等待 setup 和配置编辑完成后重试。'
MANUAL = ('如需手动迁移提示词，请按各文件的 type 从对应 .yaml.example 补齐提示词，复制旧 task/prompt，'
          '核对全部供应商配置后再删除 policy.yaml 中的 params.task 和 params.prompt。')


class MigrationError(ValueError):
    pass


def _local(root, path):
    if (not path.is_relative_to(root) or '..' in path.relative_to(root).parts
            or any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(root))):
        raise MigrationError('迁移目录以外的路径和符号链接需要手动处理。')
    return path


def _read(path, snapshots=None):
    if not path.is_file():
        raise MigrationError(f'{path.name} 不是已有的普通 YAML 文件。')
    try:
        if snapshots is not None:
            snapshots[path] = path.read_bytes()
        return load_yaml_config(path)
    except ImportError:
        raise
    except Exception:
        # Explicit YAML tags can raise a constructor error containing a private value.
        raise MigrationError(f'{path.name} 不是有效的 YAML 配置。') from None


def _candidate_files(folder):
    def unreadable(error):
        raise MigrationError('无法检查策略目录内的供应商配置。') from error

    for directory, subdirs, names in os.walk(folder, onerror=unreadable):
        here = Path(directory)
        if 'pyvenv.cfg' in names:
            subdirs.clear()
            continue
        subdirs[:] = [d for d in subdirs if d not in ARTIFACT_DIRS]
        for d in subdirs:
            _local(folder, here / d)
        for name in names:
            path = here / name
            if path.suffix not in ('.yaml', '.yml'):
                continue
            if here == folder and (path.stem == 'policy' or name.startswith(('compose.', 'docker-compose.'))):
                continue
            yield path


def _validate(settings, path):
    try:
        validate_prompt_fields(settings, path.name, allow_empty_task=True)
    except ValueError as exc:
        # The worker's validator reports only the field and its expected shape.
        raise MigrationError(str(exc)) from None


def _prompt_fields(profile, template, legacy):
    result = dict(profile)
    for name in ('task', 'prompt', 'response_instructions'):
        default = template[name]
        value = result.setdefault(name, default)
        # Setup may have created a new stock profile before this command ran.
        if value == default and legacy.get(name, '').strip():
            result[name] = legacy[name]
    current = result.get('input_templates', {})
    if isinstance(current, dict):
        result['input_templates'] = {**template['input_templates'], **current}
    return result


def migrate(root, extra_profiles=()):
    root = Path(root)
    if root.is_symlink():
        raise MigrationError('项目根目录是符号链接。')
    root = root.resolve()
    folder = _local(root, root / 'policy/llm_failure')
    _local(root, root / '.setup/backups')
    manifest = _local(root, folder / 'policy.yaml')
    snapshots = {}
    config = _read(manifest, snapshots)
    params = config.get('params')
    if not isinstance(params, dict):
        raise MigrationError('policy.yaml 的 params 需要映射。')
    legacy = {name: params[name] for name in ('task', 'prompt') if name in params}
    if any(not isinstance(value, str) for value in legacy.values()):
        raise MigrationError('旧 params.task 和 params.prompt 需要文本。')

    templates = {}
    factory_types = {}
    for kind, relative in FACTORY.items():
        target = _local(root, folder / relative)
        example = _local(root, folder / (relative + '.example'))
        defaults = _read(example)
        if defaults.get('type') != kind:
            raise MigrationError(f'{example.name} 的供应商类型与出厂模板不符。')
        _validate(defaults, example)
        templates[kind] = example, defaults
        factory_types[target] = kind

    required = set(factory_types) | set(folder.glob('provider.yml'))
    for filename in (params.get('provider_config_path', 'provider.yaml'), *extra_profiles):
        if not isinstance(filename, str) or not filename.strip() or '\0' in filename:
            raise MigrationError('provider_config_path 或 --profile 需要有效路径。')
        path = Path(filename)
        if path.is_relative_to(CONTAINER_FOLDER):
            path = path.relative_to(CONTAINER_FOLDER)
        path = _local(folder, folder / path)
        if path.suffix not in ('.yaml', '.yml'):
            raise MigrationError('供应商配置需要 .yaml 或 .yml 文件。')
        required.add(path)

    changes, creations = [], []
    for path in sorted(required | set(_candidate_files(folder))):
        _local(folder, path)
        if path.exists():
            current = _read(path, snapshots)
            kind = current.get('type')
            retired = kind == 'openai_decisions'
            known_type = isinstance(kind, str) and (kind in FACTORY or retired)
            # A local YAML outside providers/ can be unrelated to this policy's model.
            if (path not in required and not path.is_relative_to(folder / 'providers')
                    and not known_type and 'api_key' not in current and not {'model', 'base_url'} <= current.keys()):
                del snapshots[path]
                continue
            if retired or 'choice_descriptions' in current:
                setting = 'type: openai_decisions' if retired else 'choice_descriptions'
                raise MigrationError(f'{path.relative_to(folder)} 中的 {setting} 已停用。'
                                     '请手动按所选 gpt-decision-* 模板配置题型和失败判据，然后重试。')
        else:
            current = None
            kind = factory_types.get(path)
        if not isinstance(kind, str) or kind not in templates:
            raise MigrationError(f'{path.name} 缺少可用的供应商文件或受支持的 type。')
        example, defaults = templates[kind]
        updated = _prompt_fields(current if current is not None else defaults, defaults, legacy)
        if updated.keys() != defaults.keys():
            raise MigrationError(f'{path.name} 的供应商字段与相应出厂模板不匹配。')
        if (not all(isinstance(updated[key], str) for key in ('model', 'api_key', 'base_url'))
                or type(updated['timeout_s']) not in (int, float) or updated['timeout_s'] <= 0):
            raise MigrationError(f'{path.name} 的模型、连接或超时设置类型无效。')
        _validate(updated, path)
        if updated != current:
            batch = creations if current is None else changes
            batch.append((path, render_yaml_config(path, updated, template=example)))

    # write_updates backs up existing files and replaces each atomically in this order.
    # An interrupted profile write must leave the shared prompts available for a retry.
    if legacy:
        for name in legacy:
            del params[name]
        changes.append((manifest, render_yaml_config(manifest, config)))
    for path, before in snapshots.items():
        if not _local(folder, path).is_file() or path.read_bytes() != before:
            raise MigrationError(CONCURRENT_CHANGE)
    # Setup also seeds missing profiles. Never replace a file that appeared after discovery.
    for path, content in creations:
        config_files.atomic_bytes(_local(folder, path), content, overwrite=False)
        if path.read_bytes() != content:
            raise MigrationError(CONCURRENT_CHANGE)
    config_files.write_updates(root, changes)
    return len(changes) + len(creations)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT, help='项目根目录（默认本项目）')
    parser.add_argument('--profile', action='append', default=[], help='额外迁移的策略目录内 YAML 路径，可重复')
    args = parser.parse_args()
    try:
        count = migrate(args.root, args.profile)
    except MigrationError as exc:
        parser.exit(1, f'迁移未完成。{exc} {MANUAL}\n')
    except ImportError:
        parser.exit(1, '迁移需要宿主 YAML 依赖，请按 docs/setup.md 安装 requirements/setup.lock。\n')
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, f'迁移未完成。请检查配置、文件权限和备份目录，修正后可重试。{MANUAL}\n')
    print(f'迁移完成，更新 {count} 个文件。已有文件的变更前副本位于 .setup/backups/。')


if __name__ == '__main__':
    main()
