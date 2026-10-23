"""Native chain definitions and deterministic command namespaces."""
import copy
import re

from rc_policy_sdk.commands import BUILTIN_COMMANDS, command_specs


def validate_chain(config):
    if set(config) != {"schema_version", "id", "kind", "policies"}:
        raise ValueError("chain accepts only schema_version, id, kind and policies")
    identities = config["policies"]
    if not isinstance(identities, list) or not identities:
        raise ValueError("chain policies must be a nonempty list")
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) for value in identities):
        raise ValueError("chain members must be policy IDs")


def chain_commands(members):
    specs, targets, exact = {}, {}, {}
    for member in members:
        identity = member.config["id"]
        for name, spec in command_specs(member.config).items():
            alias = f"{identity}-{name}"
            if alias in BUILTIN_COMMANDS or alias in exact:
                raise ValueError(f"chain command alias collision: {alias}")
            exact[alias] = (identity, name, spec)
            specs[name] = copy.deepcopy(spec)
            targets[name] = (identity, name)
    for alias, (identity, name, spec) in exact.items():
        if alias in specs:
            raise ValueError(f"chain command alias collision: {alias}")
        specs[alias] = copy.deepcopy(spec)
        targets[alias] = (identity, name)
    return specs, targets
