"""Count with the pinned DeepSeek tokenizer; never download weights."""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, StrictUndefined
from tokenizers import Tokenizer


class BudgetError(ValueError):
    pass


@dataclass(frozen=True)
class Budget:
    messages: list
    prompt_tokens: int
    max_tokens: int
    dropped_messages: int


def prepare_messages(messages):
    """Match the pinned template's treatment of past assistant reasoning.

    The hosted API must receive the same content that we count locally.
    Cloudflare may otherwise retain <think> text that the HF template drops.
    Stored/displayed messages remain untouched.
    """
    prepared = []
    for message in messages:
        item = dict(message)
        if item['role'] == 'assistant' and '</think>' in item['content']:
            item['content'] = item['content'].rsplit('</think>', 1)[-1]
        prepared.append(item)
    return prepared


class DeepSeekCounter:
    def __init__(self, directory: Path):
        manifest = json.loads((directory / 'manifest.json').read_text('utf-8'))
        for name, digest in manifest['sha256'].items():
            if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
                raise ValueError(f'Tokenizer cache không hợp lệ: {name}')
        config = json.loads((directory / 'tokenizer_config.json').read_text('utf-8'))
        self.tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
        self.template = Environment(undefined=StrictUndefined).from_string(config['chat_template'])
        self.bos = config['bos_token']
        if isinstance(self.bos, dict):
            self.bos = self.bos['content']
        self.revision = manifest['revision']

    def count(self, messages, generation_prompt=True):
        rendered = self.template.render(messages=messages, bos_token=self.bos,
                                        add_generation_prompt=generation_prompt)
        return len(self.tokenizer.encode(rendered, add_special_tokens=False).ids)


def build_budget(counter, history, text, policy, limit, margin, output_target):
    if policy not in ('new_session', 'sliding_window'):
        raise BudgetError('Chính sách context không hợp lệ.')
    messages = prepare_messages(history) + [{'role': 'user', 'content': text}]
    dropped = 0
    # Sliding policy reserves the usual output target when possible, rather than
    # reducing every answer to the minimum before dropping old pairs.
    while True:
        prompt = counter.count(messages)
        remaining = limit - prompt - margin
        desired = output_target if policy == 'sliding_window' else 128
        if remaining >= desired or len(messages) <= 1 or policy == 'new_session':
            break
        if len(messages) < 3 or [m['role'] for m in messages[:2]] != ['user', 'assistant']:
            raise BudgetError('Lịch sử phiên không hợp lệ.')
        messages = messages[2:]
        dropped += 2
    if remaining < 128:
        raise BudgetError('Context 2K không còn đủ cho câu trả lời. Rút ngắn câu hỏi hoặc tạo phiên mới.')
    return Budget(messages, prompt, min(output_target, remaining), dropped)
