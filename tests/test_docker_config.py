import json
from pathlib import Path
import pytest
from robot_control.config import load_rc, validate_policy, validate_entry


def test_schema2_host_routing_and_legacy_config_rejection(tmp_path):
    base=json.loads((Path(__file__).parents[1]/'configs/rc.mock.json').read_text())
    base.pop('conda_executable', None)
    base.update(schema_version=2, policy_hosts={'hold':{'socket':'/run/robot-control/hold.sock'}})
    path=tmp_path/'rc.json'; path.write_text(json.dumps(base))
    assert load_rc(path)['policy_hosts']['hold']['socket'].endswith('hold.sock')
    base['conda_executable']='conda'; path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match='Conda|conda'): load_rc(path)
    manifest=dict(schema_version=2,id='hold',policy_host='hold',params={},hot_reloadable=[],
        execution=dict(mode='sync',call_every_actions=1,control_mode='qpos'))
    validate_policy(manifest)
    with pytest.raises(ValueError, match='Conda|conda'):
        validate_policy({**manifest,'conda_env':'old'})


def test_rc_entry_check_does_not_interpret_policy_source(tmp_path):
    (tmp_path/'main.py').write_text('type FuturePolicy = int\n')
    validate_entry(tmp_path)
    (tmp_path/'main.py').unlink()
    with pytest.raises(ValueError): validate_entry(tmp_path)
