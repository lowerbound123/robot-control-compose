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


def validate_policy(config: dict) -> None:
    if not isinstance(config, dict):
        raise ValueError("policy 配置必须为 JSON 对象")
    if config.get("schema_version") != 2:
        raise ValueError("policy schema_version 必须为 2（Docker 配置）")
    if "conda_env" in config:
        raise ValueError("Conda conda_env 已移除；请迁移到 policy_host")
    identity = config.get("id")
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", identity):
        raise ValueError("policy id 必须是非空、安全的标识符")
    if config.get("kind") == "chain":
        from .chain_config import validate_chain
        validate_chain(config)
        return
    if config.get("kind", "python") != "python":
        raise ValueError("unknown policy kind")
    environment = config.get("policy_host")
    if not isinstance(environment, str) or not environment.strip():
        raise ValueError("policy 必须指定 policy_host")
    execution = config.get("execution", {})
    if not isinstance(execution, dict):
        raise ValueError("execution 必须为 JSON 对象")
    if execution.get("mode") not in {"sync", "async"}:
        raise ValueError("execution.mode 只允许 sync 或 async")
    interval = execution.get("call_every_actions")
    if type(interval) is not int or interval <= 0:
        raise ValueError("call_every_actions 必须为正整数")
    if execution.get("control_mode") not in {"qpos", "epos", "mixed"}:
        raise ValueError("control_mode 只允许 qpos、epos 或 mixed")
    if not isinstance(config.get("params"), dict):
        raise ValueError("policy params 必须为 JSON 对象")
    from rc_policy_sdk.commands import command_specs
    command_specs(config)
    paths = config.get("hot_reloadable", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError("hot_reloadable 必须是字段路径列表")
    for path in paths:
        if path == "execution.control_mode" or path == "execution.tui_command_workers" or path.split(".")[0] == "tui_commands":
            raise ValueError(f"{path} 必须是冷参数")
        if path.split(".")[0] in {"id", "schema_version", "policy_host", "hot_reloadable"}:
            raise ValueError(f"进程/身份字段不允许热改：{path}")
        if isinstance(dotted_get(config, path), dict):
            raise ValueError(f"热改必须指定具体叶字段：{path}")
    json.dumps(config, allow_nan=False)


def changed_config(config: dict, path: str, value: Any) -> tuple[dict, bool]:
    if path == "id" or path.startswith("id."):
        raise ValueError("policy id 不允许重命名；请创建另一 policy 目录")
    result = copy.deepcopy(config)
    dotted_set(result, path, value)
    validate_policy(result)
    return result, path in config.get("hot_reloadable", [])


def load_rc(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        config = json.load(stream)
    if config.get("schema_version") != 2:
        raise ValueError("RC schema_version 必须为 2（Docker 配置）")
    if 'conda_executable' in config:
        raise ValueError('Conda conda_executable 已移除；请迁移到 policy_hosts')
    hosts = config.get('policy_hosts', {})
    if not isinstance(hosts, dict): raise ValueError('policy_hosts 必须为对象')
    for name, host in hosts.items():
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name)
                or not isinstance(host, dict) or set(host) != {'socket'}
                or not isinstance(host['socket'], str) or not host['socket'].startswith('/')
                or len(host['socket'].encode()) > 100):
            raise ValueError('policy_hosts 必须映射合法 host 到短绝对 UDS socket 路径')
    config['policy_hosts'] = hosts
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
    for key in ("initialize_s", "inference_s", "release_s"):
        timeout = config.get("timeouts", {}).get(key, {"initialize_s": 600, "inference_s": 60, "release_s": 30}[key])
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
        records = []
        if not self.directory.is_dir():
            return records
        for folder in sorted(self.directory.iterdir()):
            if not folder.is_dir() or not (folder / "policy.json").is_file():
                continue
            try:
                with (folder / "policy.json").open(encoding="utf-8") as stream:
                    config = json.load(stream)
                validate_policy(config)
                if config.get("kind") != "chain":
                    validate_entry(folder)
                effective = self._records[config["id"]].config if config["id"] in self._records else config
                records.append({"id": config["id"], "directory": str(folder), "valid": True,
                                "kind": effective.get("kind", "python"),
                                "policy_host": effective.get("policy_host"),
                                "control_mode": effective.get("execution", {}).get("control_mode")})
            except (ValueError, OSError, TypeError) as exc:
                records.append({"id": folder.name, "directory": str(folder), "valid": False, "error": str(exc)})
        return records

    def get(self, identity: str) -> PolicyRecord:
        record = self._get_raw(identity)
        if record.is_chain and (not record.members or any(
                self._records[item.config['id']].version != item.version for item in record.members)):
            record = self.resolve_chain(record)
            self._records[identity] = record
        return record

    def _get_raw(self, identity: str) -> PolicyRecord:
        if identity in self._records:
            return self._records[identity]
        matches = [item for item in self.list() if item["id"] == identity]
        if len(matches) != 1:
            raise ValueError(f"policy '{identity}' 不存在、不合法或标识重复")
        if not matches[0]["valid"]:
            raise ValueError(matches[0]["error"])
        directory = Path(matches[0]["directory"])
        with (directory / "policy.json").open(encoding="utf-8") as stream:
            config = json.load(stream)
        record = PolicyRecord(directory, config)
        self._records[identity] = record
        return record

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
        config = {"schema_version": 2, "id": "default_chain", "kind": "chain", "policies": list(identities)}
        existing = self._records.get("default_chain")
        if existing is not None and not existing.is_chain:
            raise ValueError("default_chain is occupied by an ordinary policy")
        folder = self.directory / "default_chain"
        # Check disk ownership even with a cached record; neither a renamed ID
        # nor a replaced destination may overwrite another policy's manifest.
        if ((existing is not None and existing.directory != folder)
                or any(row['id'] == 'default_chain' and Path(row['directory']) != folder for row in self.list())):
            raise ValueError("default_chain must be the native chain in policy/default_chain")
        path = folder / 'policy.json'
        if path.exists():
            on_disk = json.loads(path.read_text(encoding='utf-8'))
            validate_policy(on_disk)
            if on_disk.get('kind') != 'chain' or on_disk['id'] != 'default_chain':
                raise ValueError('policy/default_chain is occupied by another policy')
            if existing is None:
                existing = self._get_raw('default_chain')
        record = PolicyRecord(folder, config, existing.version + 1 if existing else 1)
        return self.resolve_chain(record, overrides)

    def install_chain(self, record):
        self._records[record.config["id"]] = record

    def save_chain(self, identities):
        record = self.propose_chain(identities)
        path = record.directory / "policy.json"
        atomic_json(path, record.config)
        return path

    def commit(self, identity: str, config: dict) -> PolicyRecord:
        validate_policy(config)
        record = self._get_raw(identity)
        if config["id"] != identity:
            raise ValueError("不能修改 policy 身份")
        record.config = config
        record.version += 1
        return record

    def save(self, identity: str) -> Path:
        record = self.get(identity)
        path = record.directory / "policy.json"
        atomic_json(path, record.config)
        return path
