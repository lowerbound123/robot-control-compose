"""Launch a connector source in its own interpreter; configuration is frozen JSON."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import signal
import tempfile

from .command_process import CommandProcess, FeedbackWatermarks, load_source
from .server import ConnectorServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--instance-id', default='body')
    parser.add_argument('--config-json', default='{}')
    parser.add_argument('--config-file')
    parser.add_argument('--journal')
    parser.add_argument('--debug-timing', action='store_true')
    args = parser.parse_args()
    config = json.loads(Path(args.config_file).read_text() if args.config_file else args.config_json)
    source = Path(args.source).resolve()
    module = load_source(source)
    with ExitStack() as stack:
        command = None
        if hasattr(module, 'create_split_connector'):
            shared = stack.enter_context(tempfile.TemporaryFile())
            watermarks = FeedbackWatermarks(shared.fileno())
            shared.truncate(watermarks.record.size)
            driver = module.create_split_connector(config, role='query', watermarks=watermarks)
            command = CommandProcess(source, config, watermarks, config.get('connect_timeout_s', 10))
        else:
            driver = module.create_connector(config)
        server = ConnectorServer(driver, args.socket, instance_id=args.instance_id, journal=args.journal,
                                 debug_timing=args.debug_timing)
        for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, lambda *_: server.stop.set())
        return server.serve(command)


if __name__ == '__main__': raise SystemExit(main())
