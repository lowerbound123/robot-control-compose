"""Launch a connector source in its own interpreter; configuration is frozen JSON."""
import argparse
import importlib.util
import json
from pathlib import Path
import signal
import sys

from .server import ConnectorServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--instance-id', default='body')
    parser.add_argument('--config-json', default='{}')
    parser.add_argument('--config-file')
    parser.add_argument('--journal')
    args = parser.parse_args()
    config = json.loads(Path(args.config_file).read_text() if args.config_file else args.config_json)
    source = Path(args.source).resolve()
    sys.path.insert(0, str(source.parent))
    spec = importlib.util.spec_from_file_location('connector_source', source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    driver = module.create_connector(config)
    server = ConnectorServer(driver, args.socket, instance_id=args.instance_id, journal=args.journal)
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, lambda *_: server.stop.set())
    return server.serve()


if __name__ == '__main__': raise SystemExit(main())
