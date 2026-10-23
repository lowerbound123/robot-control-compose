"""Exact-leaf, atomic hot patch validation shared by supervisor and worker."""

from .wire import encode_message

IMMUTABLE_FIELDS = {"schema_version", "id", "policy_host", "hot_reloadable", "tui_commands"}


def prepare_patch(config, patch):
    if not isinstance(patch, dict) or not patch:
        raise ValueError("config patch must be a nonempty mapping")
    encode_message(patch)
    allowed = config.get("hot_reloadable", [])
    assignments = []
    for path, value in patch.items():
        if (not isinstance(path, str) or path not in allowed or path.split(".")[0] in IMMUTABLE_FIELDS
                or path in {"execution.control_mode", "execution.tui_command_workers"}):
            raise ValueError(f"config path is not hot reloadable: {path}")
        parts = path.split(".")
        current = config
        for part in parts[:-1]:
            if not isinstance(current, dict) or part not in current:
                raise ValueError(f"config path does not exist: {path}")
            current = current[part]
        if not isinstance(current, dict) or parts[-1] not in current or isinstance(current[parts[-1]], dict):
            raise ValueError(f"config path is not an existing leaf: {path}")
        if isinstance(value, dict):
            raise ValueError(f"hot config value must be a leaf: {path}")
        assignments.append((current, parts[-1], value))
    return assignments


def apply_patch(config, patch):
    assignments = prepare_patch(config, patch)
    for parent, name, value in assignments:
        parent[name] = value
