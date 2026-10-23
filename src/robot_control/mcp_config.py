"""Cold HTTP settings and the --mcp HOST:PORT override."""
import copy
import ipaddress

DEFAULTS = {"host": "127.0.0.1", "port": 4000, "token_env": "ROBOT_CONTROL_MCP_TOKEN",
            "allowed_hosts": ["localhost:*", "127.0.0.1:*", "[::1]:*"],
            "allowed_origins": ["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"]}


def mcp_settings(raw=None, override=None, *, allow_ephemeral=False):
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("unknown mcp configuration field")
    config = copy.deepcopy(DEFAULTS)
    config.update(copy.deepcopy(raw))
    if override is not None:
        try:
            host, port = override.rsplit(":", 1)
            config.update(host=host, port=int(port))
        except (ValueError, AttributeError) as exc:
            raise ValueError("--mcp requires HOST:PORT") from exc
    host = config["host"]
    if not isinstance(host, str) or not host or any(c.isspace() for c in host) or any(c in host for c in "/?#@"):
        raise ValueError("mcp.host must be a hostname or IP address")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ValueError("invalid IPv6 host; use [::1]:PORT") from exc
    config["host"] = host
    if type(config["port"]) is not int or not (0 if allow_ephemeral else 1) <= config["port"] <= 65535:
        raise ValueError("mcp.port must be an integer in [1,65535]")
    if not isinstance(config["token_env"], str) or not config["token_env"]:
        raise ValueError("mcp.token_env must name an environment variable")
    for name in ("allowed_hosts", "allowed_origins"):
        if not isinstance(config[name], list) or any(not isinstance(v, str) or not v for v in config[name]):
            raise ValueError(f"mcp.{name} must be a string list")
    if host not in ("0.0.0.0", "::"):
        authority = f"[{host}]" if ":" in host else host
        for key, value in (("allowed_hosts", f"{authority}:*"), ("allowed_origins", f"http://{authority}:*")):
            if value not in config[key]:
                config[key].append(value)
    return config
