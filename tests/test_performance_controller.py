"""DEBUG diagnostics through example, self-chain, mock device and recorder."""
import json
from pathlib import Path

import pytest

from config_helpers import read_config, connector_config
from host_helpers import LocalPolicyProcess
from project_templates import copy_templates
from robot_control.controller import RobotController
from test_controller import until
from test_example_controller import ROOT, command


@pytest.mark.parametrize('level,chained', [('INFO',False),('DEBUG',False),('DEBUG',True)])
def test_debug_logging_covers_running_pipeline_and_info_stays_silent(tmp_path, monkeypatch, level, chained):
    folder=copy_templates(ROOT/'policy/example',tmp_path/'policy/example')
    manifest=read_config(folder/'policy.yaml')
    manifest['execution'].update(mode='sync',call_every_actions=1)
    manifest['params'].update(chunk_size=4,inference_delay_s=.001)
    (folder/'policy.yaml').write_text(json.dumps(manifest))
    if chained:copy_templates(ROOT/'policy/example_chain',tmp_path/'policy/example_chain')
    config=read_config(ROOT/'configs/rc.mock.yaml.example')
    config.update(policy_directory='policy',startup_policy='example_chain' if chained else 'example',
                  outputs_directory='outputs',logging={'level':level},
                  timeouts={'initialize_s':10,'inference_s':5,'release_s':3})
    device=tmp_path/'device.yaml'
    device.write_text(json.dumps({'initial_qpos':[.01]*14,'initial_epos':[.02]*14,'image_size':[12,16]}))
    connector_config(config,device=device)
    path=tmp_path/'rc.yaml';path.write_text(json.dumps(config));monkeypatch.chdir(tmp_path)
    rc=RobotController(path,process_factory=LocalPolicyProcess)
    rc.start()
    try:
        until(lambda:rc.status()['state']=='paused' and rc._worker is not None,10)
        command(rc,'record start')
        # The real example / native self-chain must still install results,
        # submit actions and consume full connector ACKs while recording stalls.
        with rc._recorder._lock, rc._recorder._admission:
            command(rc,'resume')
            until(lambda:rc.status()['consumed']>=5,1.5)
            command(rc,'pause')
        recording=command(rc,'record stop')
    finally:
        rc.submit('exit');assert rc.done.wait(15),rc.status()
    assert rc.exit_code==0
    events=[json.loads(l) for l in (tmp_path/'outputs/logs'/f'rc_{rc.run_id}.jsonl').read_text().splitlines()]
    perf=[e for e in events if e.get('event')=='performance']
    if level=='INFO':
        assert not perf
        return
    assert perf,'DEBUG emitted no performance records'
    stages={e['stage'] for e in perf}
    assert {'rc.loop','rc.policy_result','rc.send','connector.receipt','record.sample','record.enqueue'} <= stages
    result=next(e for e in perf if e['stage']=='rc.policy_result' and e.get('outcome')=='result')
    assert result['request_id']>0 and result['observation_id']
    points={p['stage'] for p in result['performance']['points']}
    assert {'worker.policy','worker.images_released','rc.result_installed'} <= points
    connector=next(e for e in perf if e['stage']=='connector.receipt')
    assert {'connector.server_received','connector.driver_begin','connector.driver_end'} <= {p['stage'] for p in connector['performance']['points']}
    loops = [e for e in perf if e['stage'] == 'rc.loop' and e.get('attempt_id')]
    assert loops
    sends = {e['attempt_id']: e for e in perf if e['stage'] == 'rc.send'}
    loop = next(e for e in loops if e['attempt_id'] in sends)
    marks = {p['stage']: p for p in loop['performance']['points']}
    assert {'rc.publications_begin', 'rc.publications_collected', 'rc.observation_collected',
            'rc.tick_decision', 'rc.loop_lock_released'} <= marks.keys()
    assert marks['rc.tick_decision']['due'] == 1
    begin = marks['rc.loop_begin']
    assert 0 < begin['wait_setup_ns'] <= begin['wait_started_ns'] <= begin['wait_returned_ns'] <= begin['monotonic_ns']
    sent = sends[loop['attempt_id']]['performance']['points']
    assert next(p for p in sent if p['stage'] == 'rc.attempt_recorded')['loop_id'] == loop['loop_id']
    assert all(e['level']=='debug' for e in perf)
    recorded=[json.loads(l) for l in (Path(recording['path'])/'events.jsonl').read_text().splitlines()]
    assert any(e.get('event')=='performance' for e in recorded)
