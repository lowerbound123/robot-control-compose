"""Validated manifests, exact dotted edits, and session-local configuration."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def atomic_json(path: Path, value: Any) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def dotted_get(config: dict, path: str) -> Any:
    value: Any = config
    for part in path.split("."):
        if not part or not isinstance(value, dict) or part not in value:
            raise ValueError(f"未知配置字段：{path}")
        value = value[part]
    return value


def dotted_set(config: dict, path: str, value: Any) -> None:
    dotted_get(config, path)
    parts = path.split(".")
    parent = config
    for part in parts[:-1]:
        parent = parent[part]
    parent[parts[-1]] = value


from rc_policy_sdk.catalog import validate_policy
from .policy_catalog import read_descriptor, discover
from .config_paths import rc_config_path, policy_config_path
from .yaml_config import load_yaml_config, save_yaml_config


def changed_config(config: dict, path: str, value: Any) -> tuple[dict, bool]:
    if path == "id" or path.startswith("id."):
        raise ValueError("policy id 不允许重命名；请创建另一 policy 目录")
    result = copy.deepcopy(config)
    dotted_set(result, path, value)
    validate_policy(result)
    return result, path in config.get("hot_reloadable", [])


def load_rc(path: Path) -> dict:
    path = rc_config_path(path)
    config = load_yaml_config(path)
    try:
        return _validate_rc(config)
    except (ValueError, TypeError, KeyError):
        raise ValueError(f'{path}: invalid RC schema') from None


def _validate_rc(config):
    if config.get("schema_version") != 3:
        raise ValueError("RC schema_version 必须为 3")
    if {'policy_hosts', 'conda_executable'} & config.keys():
        raise ValueError('legacy policy_hosts/conda_executable fields are not supported')
    hz = config.get("frequency_hz", 30)
    if isinstance(hz, bool) or not isinstance(hz, (int, float)) or not math.isfinite(hz) or hz <= 0:
        raise ValueError("frequency_hz 必须为有限正数")
    if config.get("robot", {}).get("backend") not in {"mock", "acone"}:
        raise ValueError("robot.backend 必须为 mock 或 acone")
    from .safety import finite_qpos, ModeLimiter
    from .direct import control_settings
    from .mcp_config import mcp_settings
    from .tui_config import tui_settings
    finite_qpos(config["reset_qpos"])
    ModeLimiter(config["safety"], hz)
    config["direct_control"] = control_settings(config.get("direct_control"))
    config["mcp"] = mcp_settings(config.get("mcp"))
    config["tui"] = tui_settings(config.get("tui"))
    from .global_state_config import settings as global_settings
    config['global_state'] = global_settings(config.get('global_state'))
    for key in ("initialize_s", "inference_s", "release_s", "shutdown_s"):
        timeout = config.get("timeouts", {}).get(key, {"initialize_s": 600, "inference_s": 60, "release_s": 30, "shutdown_s": 115}[key])
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"timeouts.{key} 必须为有限正数")
    for key in ("policy_directory", "outputs_directory"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError(f"RC 必须指定 {key}")
    return config


@dataclass
class PolicyRecord:
    directory: Path
    config: dict
    version: int = 1
    members: tuple = ()

    @property
    def is_chain(self):
        return self.config.get("kind") == "chain"

    @property
    def execution(self):
        return self.members[0].config["execution"] if self.is_chain else self.config["execution"]


def validate_entry(directory: Path) -> None:
    # Source belongs to the host interpreter, which may be newer than RC.
    if not (directory / 'main.py').is_file():
        raise ValueError('policy main.py 不存在')


class PolicyRegistry:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self._records: dict[str, PolicyRecord] = {}

    def list(self) -> list[dict]:
        return discover(self.directory)

    def get(self, identity: str, *, refresh=False) -> PolicyRecord:
        if refresh:
            self.refresh(identity)
        record = self._get_raw(identity)
        if record.is_chain:
            record = self.resolve_chain(record)
            self._records[identity] = record
        return record

    def snapshot(self, identity):
        from rc_policy_sdk.catalog import policy_id
        policy_id(identity)
        descriptor = read_descriptor(self.directory / identity)
        old = self._records.get(identity)
        record = PolicyRecord(Path(descriptor['directory']), descriptor['config'], old.version + 1 if old else 1)
        if record.is_chain:
            children = {}
            for name in dict.fromkeys(record.config['policies']):
                item = read_descriptor(self.directory / name)
                previous = self._records.get(name)
                children[name] = PolicyRecord(Path(item['directory']), item['config'], previous.version+1 if previous else 1)
            if any(child.is_chain for child in children.values()):
                raise ValueError('nested native chains are not supported')
            record = self.resolve_chain(record, children)
        return record

    def install(self, record):
        for child in record.members:
            self._records[child.config['id']] = child
        self._records[record.config['id']] = record

    def refresh(self, identity=None):
        from rc_policy_sdk.catalog import policy_id
        identities = [identity] if identity is not None else [row['id'] for row in self.list() if row['valid']]
        for name in identities:
            policy_id(name)
            self.install(self.snapshot(name))

    def _get_raw(self, identity: str) -> PolicyRecord:
        if identity not in self._records:
            self.refresh(identity)
        return self._records[identity]

    def resolve_chain(self, record, overrides=None):
        from .chain_config import chain_commands
        validate_policy(record.config)
        members = []
        for identity in dict.fromkeys(record.config["policies"]):
            child = (overrides or {}).get(identity) or self._get_raw(identity)
            if child.is_chain:
                raise ValueError(f"{identity}: native chain cannot be a chain member")
            validate_policy(child.config)
            validate_entry(child.directory)
            child = PolicyRecord(child.directory, copy.deepcopy(child.config), child.version)
            if members and child.execution["control_mode"] != members[0].execution["control_mode"]:
                raise ValueError(f"{identity}: control_mode differs from chain head")
            members.append(child)
        chain_commands(members)
        return PolicyRecord(record.directory, copy.deepcopy(record.config), record.version, tuple(members))

    def propose_chain(self, identities, overrides=None):
        config = {"schema_version": 3, "id": "default_chain", "kind": "chain", "policies": list(identities)}
        existing = self._records.get("default_chain")
        if existing is not None and not existing.is_chain:
            raise ValueError("default_chain is occupied by an ordinary policy")
        folder = self.directory / "default_chain"
        # Check disk ownership even with a cached record; neither a renamed ID
        # nor a replaced destination may overwrite another policy's manifest.
        if ((existing is not None and existing.directory != folder)
                or any(row['id'] == 'default_chain' and Path(row['directory']) != folder for row in self.list())):
            raise ValueError("default_chain must be the native chain in policy/default_chain")
        path = policy_config_path(folder)
        if path.exists():
            on_disk = load_yaml_config(path)
            validate_policy(on_disk, folder.name)
            if on_disk.get('kind') != 'chain' or on_disk.get('id', folder.name) != 'default_chain':
                raise ValueError('policy/default_chain is occupied by another policy')
            if existing is None:
                existing = self._get_raw('default_chain')
        record = PolicyRecord(folder, config, existing.version + 1 if existing else 1)
        return self.resolve_chain(record, overrides)

    def save_chain(self, identities):
        record = self.propose_chain(identities)
        path = policy_config_path(record.directory)
        save_yaml_config(path, record.config)
        return path

    def commit(self, identity: str, config: dict) -> PolicyRecord:
        validate_policy(config)
        record = self._get_raw(identity)
        if config["id"] != identity:
            raise ValueError("不能修改 policy 身份")
        record = PolicyRecord(record.directory, copy.deepcopy(config), record.version + 1, record.members)
        self._records[identity] = record
        return record

    def save(self, identity: str) -> Path:
        record = self.get(identity)
        path = policy_config_path(record.directory)
        save_yaml_config(path, record.config)
        return path
