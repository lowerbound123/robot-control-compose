from test_backend_switch import profile
from test_example_controller import example_rc, example_mcp
from test_mcp_management import query, tool


def test_structured_backend_tools_discover_and_switch_under_mcp(example_rc, example_mcp):
    rc, service = example_rc, example_mcp
    name = profile(rc)
    listed = query(service, 'backend_profiles')
    assert any(row['name'] == name and row['valid'] for row in listed['result'])
    assert service.submit_tool('switch_backend', {'profile': name}).result(5).is_error
    tool(service, 'take')
    stale = {key: rc.status()[key] for key in ('run_id', 'control_epoch', 'generation')}
    receipt = tool(service, 'switch_backend', profile=name, expected_control=stale)
    assert receipt['result']['backend']['profile'] == name
    assert rc.status()['control_owner'] == 'mcp'
    props = query(service, 'props')
    assert props['robot']['backend'] == 'mock'
    assert props['robot']['initial_qpos'] == [.2]*14
    assert props['backend']['profile'] == name
    rejected = service.submit_tool('switch_backend', {'profile': name, 'expected_control': stale}).result(5)
    assert rejected.is_error


def test_backend_read_tools_do_not_require_control(example_rc, example_mcp):
    rc, service = example_rc, example_mcp
    before = rc.status()
    query(service, 'backend_profiles')
    tool(service, 'command', line='backend list')
    tool(service, 'command', line='backend status')
    after = rc.status()
    assert (after['generation'], after['control_epoch'], after['control_owner']) == (
        before['generation'], before['control_epoch'], before['control_owner'])


def test_motion_receipt_drops_images_encoded_across_backend_change(example_rc, example_mcp, monkeypatch):
    import threading
    from PIL import Image
    rc, service = example_rc, example_mcp
    name = profile(rc)
    tool(service, 'take')
    entered, go = threading.Event(), threading.Event()
    save = Image.Image.save
    def slow_save(image, *args, **kwargs):
        if kwargs.get('format') == 'JPEG':
            entered.set()
            assert go.wait(5)
        return save(image, *args, **kwargs)
    monkeypatch.setattr(Image.Image, 'save', slow_save)
    pending = service.submit_tool('send_command', {'commands': 'left:up:0.001@fast', 'wait': True, 'include_images': True})
    try:
        assert entered.wait(5), pending.result(0) if pending.done() else rc.status()
        receipt = rc.wait_operation(rc.submit('backend switch '+name, origin='mcp'), 10)
        assert receipt['status'] == 'succeeded'
    finally:
        go.set()
    response = pending.result(5)
    assert response.structured_content['status'] == 'succeeded'
    assert response.structured_content['images'] == []
    assert not any(block.type == 'image' for block in response.content)
