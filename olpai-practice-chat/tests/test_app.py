import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.context_budget import Budget, BudgetError, DeepSeekCounter, build_budget
from app.main import Settings, create_app
from app.openrouter_client import MODEL, OpenRouter, ProviderError

HEADERS = {'X-Local-Client': 'olpai'}
TOKENIZER_DIR = Path(__file__).resolve().parents[1] / 'data' / 'tokenizer'


class FakeCounter:
    revision = 'test'

    def count(self, messages, generation_prompt=True):
        return 3 + sum(len(m['content']) + 3 for m in messages)


class FakeRouter:
    calls = 0

    def __init__(self, available=True, usage=None, failure=False, delay=0):
        self.available = available
        self.usage = usage
        self.failure = failure
        self.delay = delay
        self.calls = 0

    async def availability(self):
        return {'available': self.available, 'endpoint_count': int(self.available)}

    async def stream(self, budget):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        yield {'type': 'content', 'text': 'answer'}
        if self.failure:
            raise ProviderError('Safe test error')
        if self.usage != 'missing':
            yield {'type': 'usage', 'value': self.usage or {
                'prompt_tokens': budget.prompt_tokens, 'completion_tokens': 6,
                'total_tokens': budget.prompt_tokens + 6}}
        yield {'type': 'finish', 'value': 'stop'}


def setup_client(tmp_path, router=None, now=None, **kwargs):
    settings = Settings(data_dir=tmp_path, key='test-only-key', **kwargs)
    app = create_app(settings, counter=FakeCounter(), router=router or FakeRouter(), now=now)
    return TestClient(app, headers=HEADERS), app


def session(client, policy='new_session'):
    response = client.post('/api/sessions', json={'policy': policy})
    assert response.status_code == 200, response.text
    return response.json()['id']


def events(response):
    out = []
    for block in response.text.split('\n\n'):
        if block.startswith('event: '):
            name, data = block.split('\n', 1)
            out.append((name[7:], json.loads(data[6:])))
    return out


def test_native_tokenizer_vietnamese_code_and_reasoning():
    counter = DeepSeekCounter(TOKENIZER_DIR)
    messages = [{'role': 'user', 'content': 'Giải thích lỗi này:\n```python\nx = [1, 2]\nprint(x[2])\n```'}]
    assert 10 < counter.count(messages) < 100
    with_think = messages + [{'role': 'assistant', 'content': '<think>secret</think>final'}]
    only_final = messages + [{'role': 'assistant', 'content': 'final'}]
    assert counter.count(with_think) == counter.count(only_final)
    budget = build_budget(counter, [], messages[0]['content'], 'new_session', 2048, 64, 768)
    assert budget.prompt_tokens + budget.max_tokens + 64 <= 2048


def test_budget_exact_boundary_and_too_long():
    counter = FakeCounter()
    budget = build_budget(counter, [], 'a' * 1850, 'new_session', 2048, 64, 768)
    assert budget.prompt_tokens == 1856
    assert budget.max_tokens == 128
    with pytest.raises(BudgetError):
        build_budget(counter, [], 'a' * 1851, 'new_session', 2048, 64, 768)


def test_sliding_removes_complete_pairs_and_preserves_current_input():
    history = [{'role': 'user', 'content': 'a' * 900}, {'role': 'assistant', 'content': 'b' * 200},
               {'role': 'user', 'content': 'later'}, {'role': 'assistant', 'content': 'reply'}]
    b = build_budget(FakeCounter(), history, 'question' * 100, 'sliding_window', 2048, 64, 768)
    assert b.dropped_messages == 2
    assert b.messages[0]['content'] == 'later'
    assert b.messages[-1]['content'] == 'question' * 100
    assert b.prompt_tokens + b.max_tokens + 64 <= 2048
    with pytest.raises(BudgetError):
        build_budget(FakeCounter(), history, 'question' * 100, 'new_session', 2048, 64, 768)


def test_chat_persists_and_new_session_has_no_memory(tmp_path):
    client, app = setup_client(tmp_path)
    sid = session(client)
    response = client.post(f'/api/sessions/{sid}/chat', json={'text': 'secret first session'})
    assert response.status_code == 200
    assert events(response)[-1][1]['status'] == 'complete'
    detail = client.get(f'/api/sessions/{sid}').json()
    assert len(detail['messages']) == 2
    assert all(m['status'] == 'complete' for m in detail['messages'])
    other = session(client)
    assert app.state.store.history(other) == []
    export = client.get(f'/api/sessions/{sid}/export')
    assert 'test-only-key' not in export.text
    assert 'test-only-key' not in client.get('/api/status').text
    assert export.json()['tools_enabled'] is False


def test_oversized_request_never_calls_provider(tmp_path):
    router = FakeRouter()
    client, _ = setup_client(tmp_path, router)
    sid = session(client)
    r = client.post(f'/api/sessions/{sid}/chat', json={'text': 'a' * 1900})
    assert r.status_code == 409
    assert router.calls == 0
    assert client.get(f'/api/sessions/{sid}').json()['messages'] == []


def test_empty_endpoint_fails_without_fallback(tmp_path):
    router = FakeRouter(available=False)
    client, _ = setup_client(tmp_path, router)
    r = client.post(f'/api/sessions/{session(client)}/chat', json={'text': 'hello'})
    assert r.status_code == 503
    assert router.calls == 0


def test_untrusted_client_origin_and_extra_fields_rejected(tmp_path):
    client, _ = setup_client(tmp_path)
    sid = session(client)
    r = client.post(f'/api/sessions/{sid}/chat', json={'text': 'hello', 'model': MODEL + ':online',
                                                     'tools': [{'type': 'web'}], 'messages': []})
    assert r.status_code == 422
    assert client.post('/api/sessions', json={}, headers={'X-Local-Client': ''}).status_code == 403
    assert client.post('/api/sessions', json={}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/', headers={'Host': 'evil.example'}).status_code == 400
    response = client.get('/')
    assert "connect-src 'self'" in response.headers['content-security-policy']
    assert 'frame-ancestors' in response.headers['content-security-policy']


def test_no_key_never_calls(tmp_path):
    settings = Settings(data_dir=tmp_path)
    router = FakeRouter()
    client = TestClient(create_app(settings, counter=FakeCounter(), router=router), headers=HEADERS)
    sid = session(client)
    assert client.post(f'/api/sessions/{sid}/chat', json={'text': 'hello'}).status_code == 503
    assert router.calls == 0


def test_timer_new_session_reload_and_old_session_do_not_bypass(tmp_path):
    frozen = [1_000_000.0]
    client, _ = setup_client(tmp_path, now=lambda: frozen[0])
    old = session(client)
    client.post('/api/exams', json={'policy': 'sliding_window'})
    sid = session(client, 'new_session')
    assert client.get(f'/api/sessions/{sid}').json()['policy'] == 'sliding_window'
    assert client.post(f'/api/sessions/{old}/chat', json={'text': 'hello'}).status_code == 403
    frozen[0] += 18000
    assert client.get('/api/status').json()['exam']['ai_locked'] is True
    assert client.post('/api/sessions', json={}).status_code == 403
    assert client.post(f'/api/sessions/{sid}/chat', json={'text': 'hello'}).status_code == 403
    reopened, _ = setup_client(tmp_path, now=lambda: frozen[0])
    assert reopened.get('/api/status').json()['exam']['ai_locked'] is True
    client.post('/api/exams/end', json={})
    assert client.post(f'/api/sessions/{sid}/chat', json={'text': 'hello'}).status_code == 403
    assert client.post('/api/exams', json={}).status_code == 200


@pytest.mark.parametrize('usage', ['missing', {'prompt_tokens': 2000, 'completion_tokens': 100}])
def test_unverified_or_over_limit_usage_blocks_session(tmp_path, usage):
    router = FakeRouter(usage=usage)
    client, _ = setup_client(tmp_path, router)
    sid = session(client)
    response = client.post(f'/api/sessions/{sid}/chat', json={'text': 'hello'})
    assert any(name == 'warning' for name, _ in events(response))
    assert client.get(f'/api/sessions/{sid}').json()['blocked']
    assert client.post(f'/api/sessions/{sid}/chat', json={'text': 'next'}).status_code == 409


def test_failed_stream_remains_draft_and_does_not_inject_context(tmp_path):
    client, app = setup_client(tmp_path, FakeRouter(failure=True))
    sid = session(client)
    r = client.post(f'/api/sessions/{sid}/chat', json={'text': 'hello'})
    assert events(r)[-1][1]['status'] == 'error'
    assert app.state.store.history(sid) == []
    assert client.get(f'/api/sessions/{sid}').json()['messages'][-1]['content'] == 'answer'
    assert client.get(f'/api/sessions/{sid}').json()['running'] is None


def test_restart_recovers_pending_generation(tmp_path):
    client, app = setup_client(tmp_path)
    sid = session(client)
    s = app.state.store.session(sid)
    budget = build_budget(FakeCounter(), [], 'pending', 'new_session', 2048, 64, 768)
    app.state.store.reserve(s, budget, 'pending')
    assert client.get(f'/api/sessions/{sid}').json()['running']
    reopened, restored = setup_client(tmp_path)
    detail = reopened.get(f'/api/sessions/{sid}').json()
    assert detail['running'] is None
    assert detail['messages'][0]['status'] == 'interrupted'
    assert restored.state.store.history(sid) == []


def test_sliding_history_eviction_is_committed_only_on_success(tmp_path):
    router = FakeRouter()
    client, app = setup_client(tmp_path, router)
    sid = session(client, 'sliding_window')
    client.post(f'/api/sessions/{sid}/chat', json={'text': 'a' * 900})
    client.post(f'/api/sessions/{sid}/chat', json={'text': 'b' * 900})
    detail = client.get(f'/api/sessions/{sid}').json()
    assert [m['active'] for m in detail['messages']] == [0, 0, 1, 1]
    assert [m['content'] for m in app.state.store.history(sid)] == ['b' * 900, 'answer']


def test_two_tabs_stop_and_lock_release(tmp_path):
    async def scenario():
        app = create_app(Settings(data_dir=tmp_path, key='test-only'), counter=FakeCounter(), router=FakeRouter(delay=0.5))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver', headers=HEADERS) as client:
            sid = (await client.post('/api/sessions', json={})).json()['id']
            first = asyncio.create_task(client.post(f'/api/sessions/{sid}/chat', json={'text': 'first'}))
            for _ in range(50):
                await asyncio.sleep(0.01)
                running = app.state.store.one("SELECT id FROM generations WHERE status='running'")
                if running:
                    break
            assert running
            second = await client.post(f'/api/sessions/{sid}/chat', json={'text': 'second'})
            assert second.status_code == 409
            await client.post(f'/api/sessions/{sid}/stop', json={})
            response = await first
            assert events(response)[-1][1]['status'] == 'cancelled'
            assert app.state.store.history(sid) == []
            third = await client.post(f'/api/sessions/{sid}/chat', json={'text': 'third'})
            assert events(third)[-1][1]['status'] == 'complete'
    asyncio.run(scenario())


def test_upstream_whitelist_and_tool_call_block(monkeypatch):
    captured = []
    real_client = httpx.AsyncClient
    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"search"}}]}}]}\n\n',
                              headers={'content-type': 'text/event-stream'})
    monkeypatch.setattr('app.openrouter_client.httpx.AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    router = OpenRouter('test-only', 'fixed-provider')
    budget = Budget([{'role': 'user', 'content': 'Search https://example.com'}], 10, 100, 0)
    async def run():
        with pytest.raises(ProviderError, match='tool'):
            async for _ in router.stream(budget):
                pass
    asyncio.run(run())
    payload = captured[0]
    assert set(payload) == {'model', 'messages', 'max_tokens', 'temperature', 'stream', 'provider'}
    assert payload['model'] == MODEL
    assert payload['provider'] == {'require_parameters': True, 'allow_fallbacks': False, 'only': ['fixed-provider']}
    assert payload['messages'][-1]['content'] == 'Search https://example.com'


def test_openrouter_parses_reasoning_final_usage_and_length(monkeypatch):
    real_client = httpx.AsyncClient
    parts = [
        {'id': 'gen-test', 'model': MODEL, 'choices': [{'delta': {'reasoning': 'think'}}]},
        {'model': MODEL, 'choices': [{'delta': {'content': 'partial code'}}]},
        {'choices': [{'delta': {}, 'finish_reason': 'length'}],
         'usage': {'prompt_tokens': 10, 'completion_tokens': 100,
                   'completion_tokens_details': {'reasoning_tokens': 80}, 'total_tokens': 110}}
    ]
    body = ''.join('data: ' + json.dumps(p) + '\n\n' for p in parts) + 'data: [DONE]\n\n'
    monkeypatch.setattr('app.openrouter_client.httpx.AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)), **kw))
    async def run():
        return [chunk async for chunk in OpenRouter('test-only').stream(Budget([{'role': 'user', 'content': 'hi'}], 10, 100, 0))]
    chunks = asyncio.run(run())
    assert next(c['text'] for c in chunks if c['type'] == 'reasoning') == 'think'
    assert next(c['value'] for c in chunks if c['type'] == 'finish') == 'length'
    assert next(c['value'] for c in chunks if c['type'] == 'usage')['completion_tokens'] == 100


def test_inflight_generation_stops_at_exam_deadline(tmp_path):
    frozen = [1_000_000.0]
    class ClockRouter(FakeRouter):
        async def stream(self, budget):
            yield {'type': 'content', 'text': 'before deadline'}
            frozen[0] += 18000
            yield {'type': 'content', 'text': 'after deadline'}
            yield {'type': 'finish', 'value': 'stop'}
    client, _ = setup_client(tmp_path, ClockRouter(), now=lambda: frozen[0])
    client.post('/api/exams', json={})
    sid = session(client)
    r = client.post(f'/api/sessions/{sid}/chat', json={'text': 'hi'})
    stream_events = events(r)
    assert stream_events[-1][1]['status'] == 'cancelled'
    assert 'after deadline' not in ''.join(d['text'] for n, d in stream_events if n == 'delta')
    assert client.get(f'/api/sessions/{sid}').json()['running'] is None
