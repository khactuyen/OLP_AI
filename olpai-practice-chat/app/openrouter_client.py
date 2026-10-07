import asyncio
import json
import httpx

MODEL = 'deepseek/deepseek-r1-distill-qwen-32b'
BASE = 'https://openrouter.ai/api/v1'


class ProviderError(Exception):
    pass


class OpenRouter:
    model = MODEL
    base = BASE
    label = 'OpenRouter'

    def __init__(self, key, provider='', temperature=0.6):
        self.key = key
        self.provider = provider
        self.temperature = temperature

    async def availability(self):
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get(f'{BASE}/models/{MODEL}/endpoints')
            response.raise_for_status()
            endpoints = response.json().get('data', {}).get('endpoints', [])
            return {'available': bool(endpoints), 'endpoint_count': len(endpoints)}

    def payload(self, budget):
        # Whitelist constructed on server. No tools, plugins, model fallbacks,
        # :online routing, RAG, or client-controlled API parameters.
        provider = {'require_parameters': True, 'allow_fallbacks': False}
        if self.provider:
            provider['only'] = [self.provider]
        return {'model': MODEL, 'messages': budget.messages, 'max_tokens': budget.max_tokens,
                'temperature': self.temperature, 'stream': True, 'provider': provider}

    async def stream(self, budget):
        headers = {'Authorization': f'Bearer {self.key}', 'Content-Type': 'application/json',
                   'X-OpenRouter-Title': 'OlpAI Practice Chat'}
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15), follow_redirects=False) as client:
            async with client.stream('POST', f'{self.base}/chat/completions',
                                     headers=headers, json=self.payload(budget)) as response:
                if response.status_code != 200:
                    await response.aread()
                    labels = {401: 'API key không hợp lệ.', 403: 'Token thiếu quyền hoặc sai tài khoản.', 402: 'Tài khoản không đủ credit.',
                              404: 'Model chưa có endpoint phục vụ.', 429: 'Đang bị giới hạn tốc độ.'}
                    raise ProviderError(labels.get(response.status_code,
                                        f'{self.label} trả lỗi HTTP {response.status_code}.'))
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    raw = line[5:].strip()
                    if raw == '[DONE]':
                        break
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        raise ProviderError('Stream không đúng định dạng.') from None
                    if data.get('error') or data.get('success') is False or data.get('errors'):
                        raise ProviderError('Provider báo lỗi trong lúc sinh câu trả lời.')
                    if data.get('model') and data['model'] != self.model:
                        raise ProviderError('Provider trả về model khác cấu hình; đã dừng.')
                    for choice in data.get('choices', []):
                        if choice.get('error'):
                            raise ProviderError('Provider báo lỗi trong stream.')
                        delta = choice.get('delta', {})
                        if delta.get('tool_calls') or delta.get('annotations') or choice.get('message', {}).get('annotations'):
                            raise ProviderError('Provider yêu cầu gọi tool; ứng dụng đã chặn.')
                        if delta.get('content'):
                            yield {'type': 'content', 'text': delta['content']}
                        if delta.get('reasoning'):
                            yield {'type': 'reasoning', 'text': delta['reasoning']}
                        if delta.get('reasoning_content'):
                            yield {'type': 'reasoning', 'text': delta['reasoning_content']}
                        if choice.get('finish_reason'):
                            yield {'type': 'finish', 'value': choice['finish_reason']}
                    if data.get('usage'):
                        if data['usage'].get('server_tool_use', {}).get('web_search_requests', 0):
                            raise ProviderError('Provider báo dùng web search; ứng dụng đã dừng.')
                        yield {'type': 'usage', 'value': data['usage']}
                    if data.get('id'):
                        yield {'type': 'id', 'value': data['id']}


class DemoRouter:
    """Explicit UI preview, never substitutes silently for a real model."""
    key = ''
    provider = 'demo'

    async def availability(self):
        return {'available': True, 'endpoint_count': 0, 'demo': True}

    async def stream(self, budget):
        question = budget.messages[-1]['content'].lower()
        if any(word in question for word in ('search', 'http://', 'https://', 'tìm trên web')):
            answer = 'Đây là câu trả lời giả để kiểm tra giao diện. Trợ lý không có công cụ search hoặc tải URL. Hãy dán nội dung cần phân tích vào chat.'
        else:
            answer = ('Đây là **câu trả lời giả**, chưa gọi DeepSeek.\n\n'
                      'Khi có endpoint và API key, câu trả lời thực sẽ xuất hiện tại đây. '
                      'Ví dụ định dạng code:\n\n```python\n'
                      'from sklearn.pipeline import make_pipeline\n'
                      'from sklearn.feature_extraction.text import TfidfVectorizer\n'
                      'from sklearn.svm import LinearSVC\n\n'
                      'model = make_pipeline(TfidfVectorizer(), LinearSVC())\n```')
        for offset in range(0, len(answer), 18):
            await asyncio.sleep(0.025)
            yield {'type': 'content', 'text': answer[offset:offset + 18]}
        yield {'type': 'finish', 'value': 'stop'}
