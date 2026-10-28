import asyncio

import pytest

from test_controller import setup_controller as setup_controller, until
from test_mcp import client
from test_mcp_local import local_service as local_service, local_call
from test_policy_results import report


def test_local_wait_keeps_service_responsive_and_query_is_repeatable(local_service):
    controller, _, service = local_service
    local_call(service, 'take')
    waiting = service.submit_tool('return_and_wait', {})
    worker = controller._worker
    until(lambda: worker.requests or waiting.done())
    assert not waiting.done(), waiting.result() if waiting.done() else None
    assert not local_call(service, 'status').is_error
    identity = controller.status()['policy_wait_operation_id']
    pending = local_call(service, 'get_policy_result')
    assert pending.structured_content['operation_id'] == identity
    assert not pending.structured_content['finished']
    duplicate = local_call(service, 'return_and_wait')
    assert duplicate.is_error and duplicate.structured_content['operation_id'] == identity
    report(worker, message='需要检查', outcome='terminated')
    response = waiting.result(3)
    assert not response.is_error
    assert response.structured_content['finished']
    assert response.structured_content['result']['outcome'] == 'terminated'
    assert response.structured_content['controller']['control_owner'] == 'mcp'
    for _ in range(2):
        queried = local_call(service, 'get_policy_result', {'operation_id': identity})
        assert queried.structured_content['result']['message'] == '需要检查'


@pytest.mark.parametrize('timeout', [0, .02])
def test_timeout_only_ends_the_request_not_the_episode(local_service, timeout):
    controller, _, service = local_service
    local_call(service, 'take')
    response = local_call(service, 'return_and_wait', {'timeout_s': timeout})
    assert not response.is_error, response
    identity = response.structured_content['operation_id']
    assert response.structured_content['status'] in ('queued', 'running')
    assert not response.structured_content['finished']
    assert response.structured_content['timed_out']
    worker = controller._worker
    until(lambda: worker.requests)
    report(worker)
    assert controller.wait_operation(identity, 2)['status'] == 'succeeded'
    queried = local_call(service, 'get_policy_result')
    assert queried.structured_content['finished']
    assert queried.structured_content['controller']['control_owner'] == 'mcp'


def test_cancelled_local_wait_with_long_timeout_retains_handback(local_service):
    controller, _, service = local_service
    local_call(service, 'take')
    waiting = service.submit_tool('return_and_wait', {'timeout_s': 500})
    worker = controller._worker
    until(lambda: worker.requests or waiting.done())
    assert not waiting.done(), waiting.result() if waiting.done() else None
    identity = controller.status()['policy_wait_operation_id']
    waiting.cancel()
    local_call(service, 'status')
    assert controller.get_policy_result(identity)['status'] == 'running'
    report(worker)
    controller.wait_operation(identity, 2)
    assert local_call(service, 'get_policy_result').structured_content['controller']['control_owner'] == 'mcp'


@pytest.mark.parametrize('disconnect', [False, True])
def test_http_wait_and_disconnected_client_share_controller_receipt(local_service, disconnect):
    controller, _, service = local_service
    async def scenario():
        async with client(service.url) as session:
            assert not (await session.call_tool('take', {})).is_error
            task = asyncio.create_task(session.call_tool('return_and_wait', {}))
            worker = controller._worker
            for _ in range(300):
                if worker.requests or task.done():
                    break
                await asyncio.sleep(.01)
            assert not task.done(), await task if task.done() else None
            identity = controller.status()['policy_wait_operation_id']
            if disconnect:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                report(worker)
                response = await asyncio.wait_for(task, 3)
                assert response.structured_content['finished']
                assert not response.is_error
        if disconnect:
            assert controller.get_policy_result(identity)['status'] == 'running'
            report(worker)
        await asyncio.to_thread(controller.wait_operation, identity, 2)
        async with client(service.url) as session:
            result = await session.call_tool('get_policy_result', {'operation_id': identity})
            assert result.structured_content['result']['outcome'] == 'completed'
            assert result.structured_content['controller']['control_owner'] == 'mcp'
    asyncio.run(scenario())


def test_invalid_queries_and_timeouts_have_no_control_side_effects(local_service):
    controller, _, service = local_service
    assert local_call(service, 'get_policy_result').structured_content['status'] == 'none'
    assert local_call(service, 'return_and_wait').is_error
    local_call(service, 'take')
    for value in (-1, float('inf'), float('nan')):
        assert local_call(service, 'return_and_wait', {'timeout_s': value}).is_error
        assert controller.status()['control_owner'] == 'mcp'
        assert not controller.status()['policy_wait_operation_id']
    ordinary = local_call(service, 'command', {'line': 'status'}).structured_content['operation_id']
    for identity in ('unknown', ordinary):
        assert local_call(service, 'get_policy_result', {'operation_id': identity}).is_error


def test_manual_pause_and_shutdown_unblock_infinite_wait(local_service):
    controller, _, service = local_service
    local_call(service, 'take')
    waiting = service.submit_tool('return_and_wait', {})
    until(lambda: controller.status().get('policy_wait_operation_id') or waiting.done())
    assert not waiting.done(), waiting.result() if waiting.done() else None
    controller.submit('pause')
    response = waiting.result(3)
    assert response.is_error
    assert response.structured_content['reason'] == '人工接管'
    local_call(service, 'take')
    waiting = service.submit_tool('return_and_wait', {})
    until(lambda: controller.status()['policy_wait_operation_id'])
    controller.submit('exit')
    assert controller.done.wait(3)
    assert waiting.result(3).structured_content['status'] == 'cancelled'
