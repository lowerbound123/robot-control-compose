"""Stateless JSON MCP must not occupy an unused browser SSE connection."""
import asyncio
from contextlib import ExitStack

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from robot_control.mcp_server import MCPService
from test_controller import setup_controller, ready, until


@pytest.fixture
def json_service(setup_controller, monkeypatch):
    controller, _, _ = setup_controller
    ready(controller)
    monkeypatch.setenv('RC_JSON_TEST_TOKEN', 'json-transport-test')
    service = MCPService(controller, {'port': 0, 'token_env': 'RC_JSON_TEST_TOKEN'}, allow_ephemeral=True)
    service.start()
    try:
        yield service, controller
    finally:
        service.close()


AUTH = {'Authorization': 'Bearer json-transport-test'}


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('suffix', ['', '/'])
def test_json_mcp_rejects_standalone_stream_without_holding_connection(json_service, method, suffix):
    service, _ = json_service
    with httpx.Client(trust_env=False, timeout=1, follow_redirects=False) as http:
        response = http.request(method, service.url + suffix,
                                headers={**AUTH, 'Accept': 'text/event-stream'})
        assert response.status_code == 405
        # DELETE already returns 405 in the stateless MCP transport: only POST
        # is supported, even though the SDK's generic Allow header lists GET.
        assert response.headers['Allow'] == 'POST'
        assert 'text/event-stream' not in response.headers.get('Content-Type', '')


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('headers,status', [
    ({'Origin': 'https://untrusted.example', 'Host': 'untrusted.example'}, 401),
    ({**AUTH, 'Host': 'untrusted.example', 'Origin': 'https://untrusted.example'}, 421),
    ({**AUTH, 'Origin': 'https://untrusted.example'}, 403),
])
def test_json_mcp_preserves_authentication_host_and_origin_priority(json_service, method, headers, status):
    service, _ = json_service
    with httpx.Client(trust_env=False, timeout=1) as http:
        response = http.request(method, service.url, headers={**headers, 'Accept': 'text/event-stream'})
        assert response.status_code == status
        assert 'Allow' not in response.headers


def test_json_mcp_sdk_posts_still_work_and_live_streams_release(json_service):
    service, controller = json_service
    before = controller.status()
    base = service.url.removesuffix('/mcp')
    with httpx.Client(trust_env=False, timeout=3, headers=AUTH) as http, ExitStack() as streams:
        # Keep iterators alive as well as responses: finalizing an HTTPX body
        # iterator closes its underlying stream before the context exits.
        readers = []
        state = streams.enter_context(http.stream('GET', base + '/state/stream'))
        assert state.status_code == 200
        readers.append(state.iter_lines())
        assert next(line for line in readers[-1] if line.startswith('data: '))
        for camera in ('head', 'left_wrist', 'right_wrist'):
            response = streams.enter_context(http.stream('GET', base + '/preview/' + camera + '.mjpg'))
            assert response.status_code == 200
            readers.append(response.iter_bytes())
            assert b'--frame' in next(readers[-1])
        assert service.state_feed.subscriber_count == 1
        assert all(row['subscribers'] == 1 for row in service.preview.status()['cameras'].values())

        async def scenario():
            async with httpx2.AsyncClient(trust_env=False, headers=AUTH) as transport:
                async with streamable_http_client(service.url, http_client=transport) as channels:
                    async with ClientSession(channels[0], channels[1]) as client:
                        await client.initialize()
                        assert 'status' in {tool.name for tool in (await client.list_tools()).tools}
                        result = await client.call_tool('status', {})
                        assert not result.is_error
                        assert result.structured_content['run_id'] == before['run_id']
        asyncio.run(scenario())
        assert http.delete(service.url).status_code == 405
    until(lambda: service.state_feed.subscriber_count == 0
          and all(row['subscribers'] == 0 for row in service.preview.status()['cameras'].values()))
    after = controller.status()
    for key in ('state', 'generation', 'control_owner', 'control_epoch'):
        assert after[key] == before[key]
