"""Native chain definitions and deterministic command namespaces."""
import copy

from rc_policy_sdk.commands import BUILTIN_COMMANDS, command_specs


def chain_commands(members):
    specs, targets, exact, ambiguous = {}, {}, {}, set()
    for member in members:
        identity = member.config["id"]
        for name, spec in command_specs(member.config).items():
            alias = f"{identity}-{name}"
            if alias in BUILTIN_COMMANDS or alias.startswith("monitor-") or alias in exact:
                raise ValueError(f"chain command alias collision: {alias}")
            exact[alias] = (identity, name, spec)
            if name in specs or name in ambiguous:
                specs.pop(name, None)
                targets.pop(name, None)
                ambiguous.add(name)
            else:
                specs[name] = copy.deepcopy(spec)
                targets[name] = (identity, name)
    for alias, (identity, name, spec) in exact.items():
        if alias in specs:
            raise ValueError(f"chain command alias collision: {alias}")
        specs[alias] = copy.deepcopy(spec)
        targets[alias] = (identity, name)
    return specs, targets
