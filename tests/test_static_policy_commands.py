import json
from test_controller import setup_controller, ready, until


def test_empty_policy_help_lists_declarations_without_starting(setup_controller):
    controller,_,root=setup_controller
    path=root/'policy/second/policy.json'
    config=json.loads(path.read_text())
    config['tui_commands']={'say':{'handler':'say','args':['str'],'help':'declaration only'}}
    path.write_text(json.dumps(config))
    controller.config['startup_policy']=None
    controller.start()
    until(lambda:controller._connected)
    text=controller.local_help('second')
    assert 'policy second say' in text and 'not_running' in text
    assert controller._worker is None
    operation=controller.submit('policy second say hello')
    assert controller.get_operation(operation)['status']=='failed'
    assert 'not_running' in controller.get_operation(operation)['error']
