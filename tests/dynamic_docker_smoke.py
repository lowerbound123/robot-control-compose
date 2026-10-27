"""Host launcher for isolated live Docker acceptance; no hardware or provider access."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
POLICY='''import sys,time
from pathlib import Path
type Number = float
def initialize(config):
    if config['params'].get('fail'): raise RuntimeError('injected initialize failure')
    return {'frames':0,'delay':config['params'].get('release_delay',0),'external':Path('/external/marker').read_text()}
def policy(context,state,action_chunk,images,config):
    assert all(not image.flags.writeable for image in images.values())
    context['frames']+=1
    return [state['qpos']]
def reset_context(context):pass
def release(context):
    if context:time.sleep(context['delay'])
def info(context):return {**context,'python':list(sys.version_info[:2])}
'''


def main():
    token=uuid.uuid4().hex[:12]
    directory=Path(tempfile.mkdtemp(prefix='rc dynamic '+token+' ',dir='/private/tmp'))
    project='rc-dynamic-test-'+token
    endpoint=json.loads(subprocess.check_output(['docker','context','inspect']))[0]['Endpoints']['docker']['Host'][7:]
    endpoint=str(Path(endpoint).resolve())
    for name in ('configs','.setup','outputs','snapshot','policy'):(directory/name).mkdir()
    external=directory.parent/('rc external '+token);external.mkdir();(external/'marker').write_text('shared')
    socket_gid = int(subprocess.check_output(['docker','run','--rm','--network','none','--entrypoint','stat',
        '--mount','type=bind,source='+endpoint+',target=/socket',
        'robot-control-compose-policy:local','-c','%g','/socket'],text=True).strip())
    context=dict(version=3,deployment_id=token,host_root=str(directory),client_root='/workspace',base_project=project,
        rpc_volume=project+'-rpc',uid=os.getuid(),gid=os.getgid(),docker_socket=endpoint,docker_gid=socket_gid)
    (directory/'.setup/runtime.json').write_text(json.dumps(context))
    config=json.loads((ROOT/'configs/rc.mock.json.example').read_text())
    config.update(startup_policy=None,reset_qpos=[.25]*14,frequency_hz=50)
    config['safety']['enabled']=False
    config['robot']['initial_qpos']=[0]*14
    config['timeouts']=dict(initialize_s=20,inference_s=4,release_s=.2,shutdown_s=22)
    (directory/'configs/rc.mock.json').write_text(json.dumps(config))
    for name,params in [('a',{}),('b',{}),('c',{}),('failed',{'fail':True}),('slow',{'release_delay':40})]:
        folder=directory/'policy'/name;folder.mkdir()
        (folder/'main.py').write_text(POLICY)
        (folder/'policy.json').write_text(json.dumps(dict(schema_version=3,execution=dict(mode='sync',call_every_actions=1,control_mode='qpos'),params=params,
            tui_commands={'info':dict(handler='info',args=[],help='context',timeout_s=2)})))
        (folder/'compose.yaml').write_text(json.dumps({'services':{'policy':{'image':'robot-control-compose-policy:local',
            'volumes':[{'type':'bind','source':str(external),'target':'/external','read_only':True}]}}}))
    for name,members in [('pair',['a','b','a']),('next',['b','c']),('partial',['a','failed'])]:
        folder=directory/'policy'/name;folder.mkdir()
        (folder/'policy.json').write_text(json.dumps(dict(schema_version=3,kind='chain',policies=members)))
    override={'services':{'robot-control':{'image':'robot-control-compose-dynamic-tests:local',
        'command':['python3','/validation/container_dynamic_smoke.py'],
        'environment':{'PYTHONPATH':'/current/src:/current/packages/policy-sdk/src'},
        'volumes':[{'type':'bind','source':str(ROOT/'tests'),'target':'/validation','read_only':True},
                   {'type':'bind','source':str(ROOT),'target':'/current','read_only':True}]}}}
    (directory/'override.json').write_text(json.dumps(override))
    env=dict(os.environ,RC_BASE_PROJECT=project,RC_UID=str(os.getuid()),RC_GID=str(os.getgid()),RC_RPC_VOLUME=context['rpc_volume'],
        RC_DOCKER_GID=str(context['docker_gid']),RC_DOCKER_SOCKET=endpoint,RC_MCP_PORT='0')
    compose=['docker','compose','--env-file','/dev/null','--project-directory',str(directory),'-p',project,
        '-f',str(ROOT/'compose.yaml'),'-f',str(directory/'override.json')]
    evidence=Path('/private/tmp/rc-dynamic-runtime-20261027')
    try:
        subprocess.run(compose+['up','-d','--no-build'],env=env,check=True)
        cid=subprocess.check_output(compose+['ps','-aq','robot-control'],env=env,text=True).strip()
        code=subprocess.check_output(['docker','wait',cid],timeout=240,text=True).strip()
        logs=subprocess.check_output(['docker','logs',cid],text=True,stderr=subprocess.STDOUT)
        (evidence/'container-smoke.log').write_text(logs)
        if (directory/'outputs/smoke.json').exists():shutil.copyfile(directory/'outputs/smoke.json',evidence/'container-smoke.json')
        data=json.loads((directory/'outputs/smoke.json').read_text())
        assert code=='1' and len(data['outcomes'])==10,logs[-6000:]
        # Exercise the real public host down entry before the harness safety net.
        shutil.copyfile(ROOT/'compose.yaml',directory/'compose.yaml')
        (directory/'.env').write_text('')
        (directory/'.setup/base.env').write_text(''.join(key+"='"+value+"'\n" for key,value in env.items()
                                                      if key.startswith('RC_')))
        import sys
        sys.path.insert(0,str(ROOT/'scripts'))
        from compose_project import down
        status=down(directory,['-f',str(directory/'override.json'),'down','-v'])
        assert status==1, 'host down swallowed RC nonzero exit'
        data['host_down_exit_code']=status
        (evidence/'container-smoke.json').write_text(json.dumps(data,indent=2))
        print(logs[-6000:])
        print('public down exit code:',status)
    finally:
        # Exact task-owned project/deployment labels, never broad daemon pruning.
        ids=subprocess.check_output(['docker','ps','-aq','--filter','label=io.robot-control.deployment='+token],text=True).split()
        if ids:subprocess.run(['docker','rm','-f',*ids],check=True)
        ids=subprocess.check_output(['docker','ps','-aq','--filter','label=com.docker.compose.project='+project],text=True).split()
        if ids:subprocess.run(['docker','rm','-f',*ids],check=True)
        subprocess.run(compose+['down','-v','--remove-orphans'],env=env,check=True)
        for name in subprocess.check_output(['docker','network','ls','-q','--filter','label=com.docker.compose.project='+project],text=True).split():
            subprocess.run(['docker','network','rm',name],check=True)
    print('evidence:',evidence/'container-smoke.json')


if __name__=='__main__':main()
