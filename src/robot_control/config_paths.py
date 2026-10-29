"""Main-config naming and legacy checks, without parser dependencies."""
from pathlib import Path


def _present(path):
    return path.exists() or path.is_symlink()


def _legacy(path):
    raise ValueError(f'{path}: manual migration to YAML required; 旧 JSON 主配置请手动迁移')


def rc_config_path(path):
    path = Path(path)
    if path.suffix not in ('.yaml', '.yml'):
        raise ValueError(f'{path}: RC configuration requires .yaml or .yml; 请手动迁移')
    for old in (path.with_suffix('.json'), path.with_suffix('.json.example')):
        if _present(old):
            _legacy(old)
    sibling = path.with_suffix('.yml' if path.suffix == '.yaml' else '.yaml')
    if _present(path) and _present(sibling):
        raise ValueError(f'{path}: conflicting .yaml/.yml main configurations')
    return path


def policy_config_path(directory):
    folder = Path(directory)
    for old in (folder/'policy.json', folder/'policy.json.example'):
        if _present(old):
            _legacy(old)
    if _present(folder/'policy.yml') or _present(folder/'policy.yml.example'):
        raise ValueError(f'{folder}: policy main configuration must be named policy.yaml')
    return folder/'policy.yaml'


def is_rc_config_name(name):
    name = name.removesuffix('.example')
    path = Path(name)
    return (path.suffix in ('.json', '.yaml', '.yml') and
            (path.stem == 'rc' or path.stem.startswith('rc.')))


def is_main_json(root, path):
    root, path = Path(root), Path(path)
    name = path.name.removesuffix('.example')
    return (name.endswith('.json') and
            ((path.parent in (root, root/'configs') and is_rc_config_name(name)) or
             (path.parent.parent == root/'policy' and name == 'policy.json')))


def main_config_targets(root):
    """Return (destination, optional template); never recurse into checkpoints."""
    root = Path(root)
    result = []
    for directory in (root, root/'configs'):
        if not directory.is_dir():
            continue
        names = {p.name.removesuffix('.example') for p in directory.iterdir() if is_rc_config_name(p.name)}
        stems = sorted({Path(name).stem for name in names})
        for stem in stems:
            canonical, short = directory/(stem+'.yaml'), directory/(stem+'.yml')
            for old in (directory/(stem+'.json'), directory/(stem+'.json.example')):
                if _present(old):
                    _legacy(old)
            target = short if _present(short) and not _present(canonical) else canonical
            rc_config_path(target)
            templates = [p for p in (directory/(stem+'.yaml.example'), directory/(stem+'.yml.example')) if p.is_file()]
            if len(templates) > 1:
                raise ValueError(f'{directory/stem}: conflicting YAML templates')
            result.append((target, templates[0] if templates else None))
    policy_root = root/'policy'
    if policy_root.is_dir():
        for folder in sorted(policy_root.iterdir()):
            if not folder.is_dir() or not any(_present(folder/name) for name in (
                    'policy.yaml','policy.yaml.example','policy.yml','policy.yml.example','policy.json','policy.json.example')):
                continue
            target = policy_config_path(folder)
            template = folder/'policy.yaml.example'
            result.append((target, template if template.is_file() else None))
    return result
