import json

import pytest
from fastapi.testclient import TestClient

from app.context_budget import DeepSeekCounter
from app.main import Settings, create_app
from test_app import HEADERS, TOKENIZER_DIR, events, session

REASON = 'Sai lệch template vượt dự phòng. Phiên đã khóa để kiểm tra.'
ANSWER = '<think>' + 'Thinking about arithmetic. ' * 80 + '</think>\n2'


class HostedHistoryRouter:
    """Emulate a host that retains reasoning if the caller sends it."""
    model = '@cf/deepseek-ai/deepseek-r1-distill-qwen-32b'

    def __init__(self, counter):
        self.counter = counter
        self.sent = []

    async def availability(self):
        return {'available': True, 'endpoint_count': 1}

    async def stream(self, budget):
        self.sent.append(budget.messages)
        extra = 0
        for item in budget.messages:
            if item['role'] == 'assistant' and '</think>' in item['content']:
                raw = item['content']
                final = raw.rsplit('</think>', 1)[-1]
                extra += len(self.counter.tokenizer.encode(raw).ids) - len(self.counter.tokenizer.encode(final).ids)
        prompt = budget.prompt_tokens + extra + 2
        yield {'type': 'content', 'text': ANSWER}
        yield {'type': 'usage', 'value': {'prompt_tokens': prompt, 'completion_tokens': 410}}
        yield {'type': 'finish', 'value': 'stop'}


def test_three_turns_send_only_answer_but_preserve_displayed_reasoning(tmp_path):
    counter = DeepSeekCounter(TOKENIZER_DIR)
    router = HostedHistoryRouter(counter)
    app = create_app(Settings(data_dir=tmp_path, key='test-key'), counter=counter, router=router)
    with TestClient(app, headers=HEADERS) as client:
        sid = session(client)
        for text in ('1+1?', '2+2?', '3+3?'):
            response = client.post(f'/api/sessions/{sid}/chat', json={'text': text})
            assert response.status_code == 200
            done = events(response)[-1][1]
            assert done['status'] == 'complete' and done['warning'] is None
            assert client.get(f'/api/sessions/{sid}').json()['blocked'] is None
        assert all('<think>' not in m['content'] for request in router.sent for m in request if m['role'] == 'assistant')
        assert router.sent[-1][1]['content'] == '\n2'
        stored = client.get(f'/api/sessions/{sid}').json()['messages']
        assert all(m['content'] == ANSWER for m in stored if m['role'] == 'assistant')
        assert len(stored) == 6


@pytest.mark.parametrize('blocked,prompt,completion,expected_clear', [
    (REASON, 114, 768, True),
    (REASON, 1800, 768, False),
    (REASON, None, 768, False),
    ('Usage thực vượt giới hạn context.', 114, 768, False),
    ('Đã hết thời gian dùng AI.', 114, 768, False),
])
def test_recovery_repairs_only_safe_template_blocks(tmp_path, blocked, prompt, completion, expected_clear):
    from app.context_budget import Budget
    from app.storage import Store
    store = Store(tmp_path / 'practice.sqlite3')
    store.execute('INSERT INTO sessions(id,title,created,policy,blocked) VALUES(?,?,?,?,?)',
                  ('s', 'unchanged', 1, 'new_session', blocked))
    budget = Budget([{'role': 'user', 'content': 'hi'}], 10, 768, 0)
    gid = store.reserve(store.session('s'), budget, 'hi', 'cloudflare', HostedHistoryRouter.model)
    usage = {'prompt_tokens': prompt, 'completion_tokens': completion}
    store.finish('s', gid, budget, ANSWER, '', 'complete', usage=usage, error=blocked)
    assert store.recover_reasoning_template_blocks(2048) == int(expected_clear)
    assert (store.session('s')['blocked'] is None) == expected_clear
    # Audit entries and the original answer are preserved.
    assert store.one('SELECT error FROM generations WHERE id=?', (gid,))['error'] == blocked
    assert store.messages('s')[-1]['content'] == ANSWER
