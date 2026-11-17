"""Load and warm up a local checkpoint without RC, ROS or robot commands."""
import argparse
import json
from pathlib import Path
import traceback

from .backend import Pi05Backend
from .configuration import load_model_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='local Pi05 model.json path')
    args = parser.parse_args(argv)
    backend = None
    try:
        path = Path(args.config).expanduser().resolve()
        config = load_model_config(path, project_root=path.parent)
        config['model']['warmup_runs'] = max(1, config['model']['warmup_runs'])
        backend = Pi05Backend(config)
        backend.load()
        backend.warmup()
        report = {'status': 'ready', 'checkpoint': config['model']['checkpoint'],
                  'device': config['model']['device'], 'model_chunk_size': config['model']['chunk_size'],
                  'action_shape': [backend.chunk_size, 14], 'warmup_runs': config['model']['warmup_runs'],
                  'synthetic_observations': True}
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        if backend is not None:
            backend.close()
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
