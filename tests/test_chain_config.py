from config_helpers import read_config
import copy
import json

import pytest

from robot_control.config import PolicyRegistry


def add_policy(root, identity, *, mode="qpos", commands=None):
    folder = root / identity
    folder.mkdir()
    config = {"schema_version": 3, "id": identity,
              "execution": {"mode": "async", "call_every_actions": 3, "control_mode": mode},
              "params": {"value": 1}, "hot_reloadable": ["params.value"], "tui_commands": commands or {}}
    (folder / "policy.yaml").write_text(json.dumps(config))
    (folder / "main.py").write_text("def initialize(config): pass\ndef policy(*args): pass\ndef reset_context(context): pass\ndef release(context): pass\n")
    (folder / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    return config


def add_chain(root, identities):
    folder = root / "default_chain"
    folder.mkdir(exist_ok=True)
    config = {"schema_version": 3, "id": "default_chain", "kind": "chain", "policies": identities}
    (folder / "policy.yaml").write_text(json.dumps(config))
    return config


def test_chain_resolves_independent_occurrences_and_keeps_active_snapshot(tmp_path):
    add_policy(tmp_path, "a")
    add_policy(tmp_path, "b")
    definition = add_chain(tmp_path, ["a", "b", "a"])
    registry = PolicyRegistry(tmp_path)
    record = registry.get("default_chain")
    assert record.config == definition
    assert record.is_chain
    assert [item.instance_id for item in record.members] == ["a-0", "b", "a-1"]
    assert record.execution == {"mode": "async", "call_every_actions": 3, "control_mode": "qpos"}
    updated = copy.deepcopy(registry.get("a").config)
    updated["execution"]["call_every_actions"] = 7
    registry.commit("a", updated)
    assert registry.get("default_chain").execution["call_every_actions"] == 3
    assert record.execution["call_every_actions"] == 3
    assert next(row for row in registry.list() if row["id"] == "default_chain")["valid"]


@pytest.mark.parametrize("ids, error", [([], "invalid policy schema"), (["missing"], "missing"), (["default_chain"], "chain")])
def test_invalid_chain_references_rejected(tmp_path, ids, error):
    add_chain(tmp_path, ids)
    with pytest.raises(ValueError, match=error):
        PolicyRegistry(tmp_path).get("default_chain")


def test_mixed_control_modes_rejected_and_draft_save_does_not_change_effective_chain(tmp_path):
    add_policy(tmp_path, "a")
    add_policy(tmp_path, "b")
    add_policy(tmp_path, "c", mode="mixed")
    add_chain(tmp_path, ["a"])
    registry = PolicyRegistry(tmp_path)
    assert registry.get("default_chain").config["policies"] == ["a"]
    with pytest.raises(ValueError, match="c.*control_mode"):
        registry.propose_chain(["a", "c"])
    path = registry.save_chain(["b", "a", "b"])
    assert registry.get("default_chain").config["policies"] == ["a"]
    assert read_config(path)["policies"] == ["b", "a", "b"]
    assert PolicyRegistry(tmp_path).get("default_chain").config["policies"] == ["b", "a", "b"]


def test_commands_use_unique_load_order_and_reject_ambiguous_exact_aliases(tmp_path):
    command = {"handler": "test", "args": [], "help": "test"}
    add_policy(tmp_path, "a", commands={"test": command})
    add_policy(tmp_path, "b", commands={"test": command})
    add_chain(tmp_path, ["a", "b", "a"])
    record = PolicyRegistry(tmp_path).get("default_chain")
    from robot_control.chain_config import chain_commands
    specs, targets = chain_commands(record.members)
    assert set(specs) == {"a-0-test", "b-test", "a-1-test"}
    assert targets == {"a-0-test": ("a-0", "test"), "b-test": ("b", "test"), "a-1-test": ("a-1", "test")}
    add_policy(tmp_path, "c", commands={"a-test": command})
    with pytest.raises(ValueError, match="collision"):
        PolicyRegistry(tmp_path).propose_chain(["a", "c"])


def test_failed_atomic_save_keeps_disk_and_session(tmp_path, monkeypatch):
    add_policy(tmp_path, "a")
    add_policy(tmp_path, "b")
    add_chain(tmp_path, ["a"])
    registry = PolicyRegistry(tmp_path)
    registry.get("default_chain")
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr("robot_control.config.os.replace", fail)
    with pytest.raises(OSError, match="disk full"):
        registry.save_chain(["b"])
    assert registry.get("default_chain").config["policies"] == ["a"]
    assert read_config(tmp_path / "default_chain/policy.yaml")["policies"] == ["a"]


def test_renamed_chain_is_invalid_and_cannot_alias_original_id(tmp_path):
    add_policy(tmp_path, 'a')
    add_chain(tmp_path, ['a'])
    (tmp_path / 'default_chain').rename(tmp_path / 'renamed')
    registry = PolicyRegistry(tmp_path)
    with pytest.raises(ValueError, match='missing'):
        registry.get('default_chain')
    with pytest.raises(ValueError, match='invalid policy schema'):
        registry.get('renamed')


def test_cached_default_does_not_overwrite_replaced_destination(tmp_path):
    add_policy(tmp_path, 'a')
    add_chain(tmp_path, ['a'])
    registry = PolicyRegistry(tmp_path)
    registry.get('default_chain')
    foreign = add_policy(tmp_path, 'foreign')
    path = tmp_path / 'default_chain' / 'policy.yaml'
    path.write_text(json.dumps(foreign))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='occupied|directory'):
        registry.save_chain(['a'])
    assert path.read_bytes() == before


@pytest.mark.parametrize("sources,expected", [(["a"],["a"]), (["a","a"],["a-0","a-1"]), (["a","b","a","b","a"],["a-0","b-0","a-1","b-1","a-2"])])
def test_instance_names_follow_each_source_occurrence(sources, expected):
    from robot_control.chain_config import chain_instance_ids
    assert chain_instance_ids(sources) == expected


def test_instance_name_collision_is_rejected(tmp_path):
    for name in ("a", "a-0"): add_policy(tmp_path, name)
    with pytest.raises(ValueError, match="collision"):
        PolicyRegistry(tmp_path).propose_chain(["a", "a", "a-0"])


def test_instance_reload_and_save_preserve_siblings_and_source_identity(tmp_path):
    from dataclasses import replace
    add_policy(tmp_path, "a")
    add_chain(tmp_path, ["a", "a"])
    registry = PolicyRegistry(tmp_path)
    original = registry.get("default_chain")
    changed = copy.deepcopy(original.members[0])
    changed.config["params"]["value"] = 9
    changed.version += 1
    proposed = registry.resolve_chain(replace(original, version=original.version+1), {"a-0": changed})
    registry.install(proposed)
    assert [m.config["params"]["value"] for m in registry.get("default_chain").members] == [9,1]
    assert registry.get("a").config["params"]["value"] == 1
    assert proposed.members[0].runtime_config["id"] == "a-0"
    with pytest.raises(ValueError, match="a-0.*a-1"):
        registry.resolve_member(proposed, "a")
    registry.save_record(registry.resolve_member(proposed, "a-0"))
    saved = read_config(tmp_path / "a/policy.yaml")
    assert saved["id"] == "a" and saved["params"]["value"] == 9
    assert not (tmp_path / "a-0").exists()
    assert [m.config["params"]["value"] for m in registry.get("default_chain").members] == [9,1]
    assert [m.config["params"]["value"] for m in registry.snapshot("default_chain").members] == [9,9]


def test_reapply_reads_current_source_defaults_after_cached_source_and_switch(tmp_path):
    original = add_policy(tmp_path, 'a')
    add_chain(tmp_path, ['a', 'a'])
    registry = PolicyRegistry(tmp_path)
    assert registry.get('a').config['params']['value'] == 1
    original['params']['value'] = 9
    (tmp_path / 'a/policy.yaml').write_text(json.dumps(original))
    switched = registry.snapshot('default_chain')
    registry.install(switched)
    assert [m.config['params']['value'] for m in switched.members] == [9, 9]
    reapplied = registry.propose_chain(['a', 'a'])
    assert [m.config['params']['value'] for m in reapplied.members] == [9, 9]
