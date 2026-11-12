"""Read one provider profile locally, without exposing YAML values in errors."""
from pathlib import Path
import sys

from httpx2 import URL, InvalidURL
from ruamel.yaml import YAML
from ruamel.yaml.tokens import AnchorToken, DirectiveToken


def _read_profile(path):
    try:
        if not path.is_file():
            raise ValueError()
        source = path.read_text(encoding='utf-8')
        # Reject the constructs for which ruamel emits warnings quoting private
        # source. Scanning one snapshot also keeps concurrent command threads
        # independent of Python's process-wide warning filters.
        anchors = set()
        for token in YAML(typ='safe', pure=True).scan(source):
            if isinstance(token, DirectiveToken) and token.name == 'YAML' and token.value != (1, 2):
                raise ValueError()
            if isinstance(token, AnchorToken):
                if token.value in anchors:
                    raise ValueError()
                anchors.add(token.value)
        return YAML(typ='safe', pure=True).load(source)
    except Exception:
        # Constructors for explicitly tagged scalars can also raise built-in
        # exceptions containing the scalar value (e.g. KeyError for !!bool).
        raise ValueError(f'{path}: cannot read valid provider YAML') from None


def _valid_base_url(address):
    if not isinstance(address, str) or any(c.isspace() for c in address):
        return False
    try:
        url = URL(address)  # Use the same offline parser as the locked OpenAI client.
        return (url.scheme in ('http', 'https') and bool(url.host) and not url.userinfo
                and '?' not in address and '#' not in address
                and (url.port is None or 0 <= url.port <= 65535))
    except (InvalidURL, ValueError):
        return False


def load_provider_config(filename, policy_root, formats, *, extra_fields=None):
    """Validate the common profile and any additional fields required by the selected backend."""
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError('params.provider_config_path must be a nonempty path')
    path = Path(policy_root) / filename
    settings = _read_profile(path)
    if not isinstance(settings, dict):
        raise ValueError(f'{path}: expected a provider mapping')
    kind = settings.get('type')
    if not isinstance(kind, str) or kind not in formats:
        raise ValueError(f'{path}: missing or unsupported provider type')
    required = {'type', 'model', 'base_url', 'api_key', 'timeout_s'}
    if formats[kind]:
        required.add('output_format')
    if extra_fields:
        required.update(extra_fields.get(kind, ()))
    if settings.keys() != required:
        raise ValueError(f'{path}: expected provider fields {", ".join(sorted(required))}')
    for field in ('model', 'api_key'):
        if not isinstance(settings[field], str) or not settings[field].strip():
            raise ValueError(f'{path}: {field} must be a nonempty string')
    if not _valid_base_url(settings['base_url']):
        raise ValueError(f'{path}: base_url must be HTTP(S) without credentials, query or fragment')
    timeout = settings['timeout_s']
    if type(timeout) not in (int, float) or not 0 < timeout <= sys.float_info.max:
        raise ValueError(f'{path}: timeout_s must be positive and finite')
    if formats[kind] and settings['output_format'] not in formats[kind]:
        raise ValueError(f'{path}: unsupported output_format for this backend')
    return settings
