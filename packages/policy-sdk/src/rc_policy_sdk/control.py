"""Small, version-negotiated control requests attached to inference results."""
from .wire import ProtocolError


RUNTIME_TOOLS = ("pause",)


def runtime_tools(value):
    """Read capability names, accepting future names without enabling them."""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProtocolError("runtime_tools must be a list of names")
    return frozenset(value).intersection(RUNTIME_TOOLS)


def control_requests(value, *, chain=False):
    """Validate and copy result metadata; only the chain host may add a source."""
    if not isinstance(value, list) or len(value) > 1:
        raise ProtocolError("control_requests must be a list with at most one request")
    if not value:
        return []
    request = value[0]
    keys = {"type", "reason", "source"} if chain else {"type", "reason"}
    if (not isinstance(request, dict) or set(request) != keys
            or request["type"] != "pause" or not isinstance(request["reason"], str)):
        raise ProtocolError("control_requests must contain a pause with a string reason")
    result = {"type": "pause", "reason": request["reason"]}
    if chain:
        source = request["source"]
        if (not isinstance(source, dict) or set(source) != {"policy_id", "cell"}
                or not isinstance(source["policy_id"], str) or not source["policy_id"]
                or type(source["cell"]) is not int or source["cell"] < 1):
            raise ProtocolError("control_requests has an invalid chain source")
        result["source"] = dict(source)
    return [result]
