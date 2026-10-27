"""Tests consume shipped examples, never a developer's private JSON or .env."""
import json
from pathlib import Path
import shutil


def template_path(path):
    path = Path(path)
    example = path.with_name(path.name + '.example')
    return example if example.is_file() else path


def copy_templates(source, destination, **kwargs):
    source, destination = Path(source), Path(destination)
    previous = kwargs.pop('ignore', None)
    def ignored(directory, names):
        result = set(previous(directory, names) if previous else ())
        result.update(name for name in names if name == '.env' or name == '__pycache__' or
                      name.endswith('.json') and (Path(directory) / (name + '.example')).is_file())
        return result
    shutil.copytree(source, destination, ignore=ignored, **kwargs)
    for path in destination.rglob('*.json.example'):
        target = path.with_suffix('')
        target.write_text(path.read_text())
        target.chmod(0o600)
        path.unlink()
    return destination
