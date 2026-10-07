"""One-time public download, before practice. No model weights or API key."""
import hashlib
import json
from pathlib import Path
import urllib.request

REVISION = '711ad2ea6aa40cfca18895e8aca02ab92df1a746'
DEST = Path(__file__).resolve().parent / 'data' / 'tokenizer'


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    manifest = {'model': 'deepseek-ai/DeepSeek-R1-Distill-Qwen-32B',
                'revision': REVISION, 'sha256': {}}
    for name in ('tokenizer.json', 'tokenizer_config.json'):
        url = f'https://huggingface.co/{manifest["model"]}/resolve/{REVISION}/{name}'
        with urllib.request.urlopen(url, timeout=90) as response:
            data = response.read()
        json.loads(data)
        temp = DEST / (name + '.tmp')
        temp.write_bytes(data)
        temp.replace(DEST / name)
        manifest['sha256'][name] = hashlib.sha256(data).hexdigest()
        print(f'{name}: {len(data):,} bytes')
    (DEST / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('Tokenizer ready. No model weights downloaded.')


if __name__ == '__main__':
    main()
