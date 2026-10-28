"""Small, version-negotiated control requests attached to inference results."""
from .wire import ProtocolError


RUNTIME_TOOLS = ("pause", "finish")


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
    if not isinstance(request, dict):
        raise ProtocolError("control_requests must contain an object")
    kind = request.get('type')
    keys = {'type', 'reason'} if kind == 'pause' else {'type', 'outcome', 'message'}
    if set(request) != keys | ({'source'} if chain else set()):
        raise ProtocolError("control_requests has invalid fields")
    if kind == 'pause' and isinstance(request['reason'], str):
        result = {'type': 'pause', 'reason': request['reason']}
    elif (kind == 'finish' and request['outcome'] in ('completed', 'terminated')
          and isinstance(request['message'], str)):
        result = {'type': 'finish', 'outcome': request['outcome'], 'message': request['message']}
    else:
        raise ProtocolError("control_requests must contain a valid pause or finish")
    if chain:
        source = request["source"]
        if (not isinstance(source, dict) or set(source) != {"policy_id", "cell"}
                or not isinstance(source["policy_id"], str) or not source["policy_id"]
                or type(source["cell"]) is not int or source["cell"] < 1):
            raise ProtocolError("control_requests has an invalid chain source")
        result["source"] = dict(source)
    return [result]


def merge_control_requests(current, incoming):
    """Keep the first finish, unless any stage requests an ordinary pause."""
    if incoming and (not current or (incoming[0]['type'] == 'pause' and current[0]['type'] != 'pause')):
        return incoming
    return current
