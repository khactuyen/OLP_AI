import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cloudflare_client import Cloudflare, MODEL
from app.context_budget import Budget
from app.main import Settings, create_app
from app.openrouter_client import ProviderError
from test_app import FakeCounter, HEADERS, events, session

ACCOUNT = 'a' * 32
BUDGET = Budget([{'role': 'user', 'content': 'hi'}], 10, 100, 0)


def mock_http(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))


def test_cloudflare_catalog_stream_and_export(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers['authorization'] == 'Bearer private-test-token'
        assert request.url.host == 'api.cloudflare.com'
        if request.method == 'GET':
            assert request.url.path == f'/client/v4/accounts/{ACCOUNT}/ai/models/search'
            return httpx.Response(200, json={'success': True, 'result': [{'name': MODEL}]})
        payload = json.loads(request.content)
        assert set(payload) == {'model', 'messages', 'max_tokens', 'temperature', 'stream', 'stream_options'}
        assert payload['model'] == MODEL
        assert payload['stream_options'] == {'include_usage': True}
        assert request.url.path == f'/client/v4/accounts/{ACCOUNT}/ai/v1/chat/completions'
        parts = [{'model': MODEL, 'choices': [{'delta': {'reasoning_content': 'think'}}]},
                 {'choices': [{'delta': {'content': 'answer'}, 'finish_reason': 'stop'}]},
                 {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 8, 'total_tokens': 18}}]
        return httpx.Response(200, text=''.join('data: ' + json.dumps(p) + '\n\n' for p in parts) + 'data: [DONE]\n\n')
    mock_http(monkeypatch, handler)
    app = create_app(Settings(data_dir=tmp_path, key='private-test-token', account_id=ACCOUNT), counter=FakeCounter())
    with TestClient(app, headers=HEADERS) as client:
        status = client.get('/api/status').json()
        assert status['model'] == MODEL and status['backend'] == 'cloudflare'
        assert status['configuration_error'] is None
        sid = session(client)
        assert client.post('/api/availability').json()['available']
        assert len(calls) == 1 and calls[0].method == 'GET'
        response = client.post(f'/api/sessions/{sid}/chat', json={'text': 'hi'})
        assert events(response)[-1][1]['status'] == 'complete'
        export = client.get(f'/api/sessions/{sid}/export')
        assert 'private-test-token' not in export.text
        assert export.json()['generations'][0]['model'] == MODEL
        assert export.json()['generations'][0]['backend'] == 'cloudflare'


@pytest.mark.parametrize('account,key', [('', ''), ('../../evil', 'token'), (ACCOUNT, '')])
def test_invalid_config_never_calls_cloudflare(tmp_path, monkeypatch, account, key):
    def unexpected(request):
        pytest.fail('Invalid credentials must not produce a network request')
    mock_http(monkeypatch, unexpected)
    app = create_app(Settings(data_dir=tmp_path, key=key, account_id=account), counter=FakeCounter())
    with TestClient(app, headers=HEADERS) as client:
        assert client.get('/api/status').json()['configuration_error']
        assert not client.post('/api/availability').json()['available']
        sid = session(client)
        assert client.post(f'/api/sessions/{sid}/chat', json={'text': 'hi'}).status_code == 503


@pytest.mark.parametrize('part', [
    {'model': '@cf/meta/another-model'},
    {'choices': [{'delta': {'tool_calls': [{'id': 'search'}]}}]},
    {'success': False, 'errors': [{'message': 'SECRET must never leak'}]},
])
def test_cloudflare_rejects_fallback_tools_and_errors(monkeypatch, part):
    mock_http(monkeypatch, lambda request: httpx.Response(200, text='data: ' + json.dumps(part) + '\n\n'))
    async def run():
        return [chunk async for chunk in Cloudflare('token', ACCOUNT).stream(BUDGET)]
    with pytest.raises(ProviderError) as error:
        asyncio.run(run())
    assert 'SECRET' not in str(error.value)


@pytest.mark.parametrize('status', [401, 403, 429])
def test_cloudflare_catalog_errors_sanitized(monkeypatch, status):
    mock_http(monkeypatch, lambda request: httpx.Response(status, text='SECRET'))
    with pytest.raises(ProviderError) as error:
        asyncio.run(Cloudflare('token', ACCOUNT).availability())
    assert 'SECRET' not in str(error.value)


def test_cloudflare_catalog_requires_exact_model(monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={
        'success': True, 'result': [{'name': '@cf/deepseek-ai/deepseek-r1-distill-llama-70b'}]}))
    assert not asyncio.run(Cloudflare('token', ACCOUNT).availability())['available']


@pytest.mark.parametrize('backend,expected', [('cloudflare', 'cf-test-token'), ('openrouter', 'or-test-token')])
def test_environment_selects_only_requested_provider(monkeypatch, backend, expected):
    monkeypatch.setenv('OLP_BACKEND', backend)
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN', 'cf-test-token')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'or-test-token')
    monkeypatch.setenv('CLOUDFLARE_ACCOUNT_ID', ACCOUNT)
    settings = Settings.from_env()
    assert settings.backend == backend
    assert settings.key == expected
