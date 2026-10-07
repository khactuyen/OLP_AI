import re

import httpx

from .openrouter_client import OpenRouter, ProviderError

MODEL = '@cf/deepseek-ai/deepseek-r1-distill-qwen-32b'


class Cloudflare(OpenRouter):
    """Direct Workers AI, one fixed model, no gateway or provider fallback."""
    model = MODEL
    label = 'Cloudflare Workers AI'

    def __init__(self, key, account_id, temperature=0.6):
        self.key = key
        self.account_id = account_id
        self.temperature = temperature
        self.provider = 'cloudflare'
        self.base = f'https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1'

    def configuration_error(self):
        if not self.key:
            return 'Điền CLOUDFLARE_API_TOKEN trong .env rồi khởi động lại app.'
        if not re.fullmatch(r'[a-fA-F0-9]{32}', self.account_id):
            return 'Điền CLOUDFLARE_ACCOUNT_ID gồm 32 ký tự hex trong .env rồi khởi động lại app.'
        return None

    async def availability(self):
        if error := self.configuration_error():
            raise ProviderError(error)
        # Authenticated catalog check only: never creates a paid completion.
        url = f'https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/models/search'
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get(url, headers={'Authorization': f'Bearer {self.key}'},
                                        params={'search': 'deepseek-r1-distill-qwen-32b', 'per_page': 100})
        if response.status_code in (401, 403):
            raise ProviderError('Cloudflare từ chối token/tài khoản. Kiểm tra Account ID và quyền Workers AI Read/Edit.')
        if response.status_code != 200:
            raise ProviderError(f'Không kiểm tra được Cloudflare (HTTP {response.status_code}).')
        data = response.json()
        if data.get('success') is not True:
            raise ProviderError('Cloudflare báo lỗi khi kiểm tra danh mục model.')
        found = any(item.get('name') == MODEL for item in data.get('result', []) if isinstance(item, dict))
        return {'available': found, 'endpoint_count': int(found), 'inference_verified': False}

    def payload(self, budget):
        return {'model': MODEL, 'messages': budget.messages, 'max_tokens': budget.max_tokens,
                'temperature': self.temperature, 'stream': True,
                'stream_options': {'include_usage': True}}

    async def stream(self, budget):
        if error := self.configuration_error():
            raise ProviderError(error)
        async for chunk in super().stream(budget):
            yield chunk
