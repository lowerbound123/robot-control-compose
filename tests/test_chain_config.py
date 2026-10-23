import copy
import json

import pytest

from robot_control.config import PolicyRegistry


def add_policy(root, identity, *, mode="qpos", commands=None):
    folder = root / identity
    folder.mkdir()
    config = {"schema_version": 2, "id": identity, "policy_host": "env-" + identity,
              "execution": {"mode": "async", "call_every_actions": 3, "control_mode": mode},
              "params": {"value": 1}, "hot_reloadable": ["params.value"], "tui_commands": commands or {}}
    (folder / "policy.json").write_text(json.dumps(config))
    (folder / "main.py").write_text("def initialize(config): pass\ndef policy(*args): pass\ndef reset_context(context): pass\ndef release(context): pass\n")
    return config


def add_chain(root, identities):
    folder = root / "default_chain"
    folder.mkdir(exist_ok=True)
    config = {"schema_version": 2, "id": "default_chain", "kind": "chain", "policies": identities}
    (folder / "policy.json").write_text(json.dumps(config))
    return config


def test_chain_resolves_unique_members_without_imports_and_follows_head(tmp_path):
    add_policy(tmp_path, "A")
    add_policy(tmp_path, "B")
    definition = add_chain(tmp_path, ["A", "B", "A"])
    registry = PolicyRegistry(tmp_path)
    record = registry.get("default_chain")
    assert record.config == definition
    assert record.is_chain
    assert [item.config["id"] for item in record.members] == ["A", "B"]
    assert record.execution == {"mode": "async", "call_every_actions": 3, "control_mode": "qpos"}
    updated = copy.deepcopy(registry.get("A").config)
    updated["execution"]["call_every_actions"] = 7
    registry.commit("A", updated)
    assert registry.get("default_chain").execution["call_every_actions"] == 7
    assert record.execution["call_every_actions"] == 3
    assert next(row for row in registry.list() if row["id"] == "default_chain")["valid"]


@pytest.mark.parametrize("ids, error", [([], "nonempty"), (["missing"], "missing"), (["default_chain"], "chain")])
def test_invalid_chain_references_rejected(tmp_path, ids, error):
    add_chain(tmp_path, ids)
    with pytest.raises(ValueError, match=error):
        PolicyRegistry(tmp_path).get("default_chain")


def test_mixed_control_modes_rejected_and_draft_save_does_not_change_effective_chain(tmp_path):
    add_policy(tmp_path, "A")
    add_policy(tmp_path, "B")
    add_policy(tmp_path, "C", mode="mixed")
    add_chain(tmp_path, ["A"])
    registry = PolicyRegistry(tmp_path)
    assert registry.get("default_chain").config["policies"] == ["A"]
    with pytest.raises(ValueError, match="C.*control_mode"):
        registry.propose_chain(["A", "C"])
    path = registry.save_chain(["B", "A", "B"])
    assert registry.get("default_chain").config["policies"] == ["A"]
    assert json.loads(path.read_text())["policies"] == ["B", "A", "B"]
    assert PolicyRegistry(tmp_path).get("default_chain").config["policies"] == ["B", "A", "B"]


def test_commands_use_unique_load_order_and_reject_ambiguous_exact_aliases(tmp_path):
    command = {"handler": "test", "args": [], "help": "test"}
    add_policy(tmp_path, "A", commands={"test": command})
    add_policy(tmp_path, "B", commands={"test": command})
    add_chain(tmp_path, ["A", "B", "A"])
    record = PolicyRegistry(tmp_path).get("default_chain")
    from robot_control.chain_config import chain_commands
    specs, targets = chain_commands(record.members)
    assert set(specs) == {"test", "A-test", "B-test"}
    assert targets == {"test": ("B", "test"), "A-test": ("A", "test"), "B-test": ("B", "test")}
    add_policy(tmp_path, "C", commands={"A-test": command})
    with pytest.raises(ValueError, match="collision"):
        PolicyRegistry(tmp_path).propose_chain(["A", "C"])


def test_failed_atomic_save_keeps_disk_and_session(tmp_path, monkeypatch):
    add_policy(tmp_path, "A")
    add_policy(tmp_path, "B")
    add_chain(tmp_path, ["A"])
    registry = PolicyRegistry(tmp_path)
    registry.get("default_chain")
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr("robot_control.config.os.replace", fail)
    with pytest.raises(OSError, match="disk full"):
        registry.save_chain(["B"])
    assert registry.get("default_chain").config["policies"] == ["A"]
    assert json.loads((tmp_path / "default_chain/policy.json").read_text())["policies"] == ["A"]


@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('occupied', [False, True])
def test_cached_chain_in_wrong_directory_cannot_create_duplicate_default_id(tmp_path, cached, occupied):
    add_policy(tmp_path, 'A')
    add_chain(tmp_path, ['A'])
    (tmp_path / 'default_chain').rename(tmp_path / 'renamed')
    registry = PolicyRegistry(tmp_path)
    if cached:
        registry.get('default_chain')
    if occupied:
        add_policy(tmp_path, 'foreign')
        (tmp_path / 'foreign').rename(tmp_path / 'default_chain')
    path = tmp_path / 'default_chain' / 'policy.json'
    before = path.read_bytes() if occupied else None
    with pytest.raises(ValueError, match='policy/default_chain'):
        registry.save_chain(['A'])
    if occupied:
        assert path.read_bytes() == before
    else:
        assert not (tmp_path / 'default_chain').exists()


def test_cached_default_does_not_overwrite_replaced_destination(tmp_path):
    add_policy(tmp_path, 'A')
    add_chain(tmp_path, ['A'])
    registry = PolicyRegistry(tmp_path)
    registry.get('default_chain')
    foreign = add_policy(tmp_path, 'foreign')
    path = tmp_path / 'default_chain' / 'policy.json'
    path.write_text(json.dumps(foreign))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='occupied'):
        registry.save_chain(['A'])
    assert path.read_bytes() == before
