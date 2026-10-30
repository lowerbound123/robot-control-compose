"""Native chain definitions and deterministic command namespaces."""
import copy
from collections import Counter

from rc_policy_sdk.catalog import policy_id

from rc_policy_sdk.commands import BUILTIN_COMMANDS, command_specs


def chain_instance_ids(identities):
    counts = Counter(identities)
    seen = Counter()
    result = []
    for source in identities:
        policy_id(source)
        identity = f'{source}-{seen[source]}' if counts[source] > 1 else source
        if len(identity) > 64:
            raise ValueError('chain instance ID exceeds 64 characters')
        seen[source] += 1
        if identity in result:
            raise ValueError(f'chain instance ID collision: {identity}')
        result.append(identity)
    return result


def chain_commands(members):
    specs, targets, exact, ambiguous = {}, {}, {}, set()
    for member in members:
        identity = member.instance_id
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
