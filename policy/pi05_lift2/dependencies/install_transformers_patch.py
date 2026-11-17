"""Install the five pinned dev inference patches at image build time only."""
import hashlib
from importlib import metadata, util
import json
from pathlib import Path
import shutil


def install():
    if metadata.version('transformers') != '4.57.1':
        raise RuntimeError('Pi05 LIFT2 patches require transformers==4.57.1')
    source = Path(__file__).parent / 'transformers_patch'
    target = Path(util.find_spec('transformers').origin).parent
    hashes = json.loads((source / 'sha256.json').read_text())
    for name, expected in hashes.items():
        path = source / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f'corrupt Pi05 LIFT2 patch: {name}')
    for name in hashes:
        shutil.copyfile(source / name, target / name)
    print(f'Installed {len(hashes)} verified Pi05 LIFT2 Transformers patches')


if __name__ == '__main__':
    install()
