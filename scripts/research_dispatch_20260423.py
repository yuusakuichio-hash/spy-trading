"""research_dispatch_20260423.py — 調査依頼を Gemini/o3 に並列送信する汎用 dispatch

usage: python3 research_dispatch_20260423.py <input_md> <model_tag>
  model_tag: gemini | o3
  input_md: そのまま prompt として使用される調査依頼 md
"""
import json, sys, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime


def load_env_key(k: str) -> str:
    for line in Path('/Users/yuusakuichio/trading/.env').read_text().splitlines():
        if line.startswith(f'{k}='):
            return line.split('=', 1)[1].strip()
    raise RuntimeError(f'{k} not found')


def call_gemini(prompt: str, model: str = 'gemini-flash-latest') -> dict:
    api_key = load_env_key('GEMINI_API_KEY')
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
    body = json.dumps({
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.2, 'maxOutputTokens': 8192},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'X-goog-api-key': api_key,
    }, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': {'code': e.code, 'message': e.read().decode()[:500]}}


def call_openai(prompt: str, model: str = 'o3') -> dict:
    api_key = load_env_key('OPENAI_API_KEY')
    url = 'https://api.openai.com/v1/chat/completions'
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': {'code': e.code, 'message': e.read().decode()[:500]}}


def main():
    if len(sys.argv) < 3:
        print("usage: python3 research_dispatch_20260423.py <input_md> <model_tag>")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    model_tag = sys.argv[2]
    content = input_path.read_text()
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    stem = input_path.stem
    out_dir = Path('/Users/yuusakuichio/trading/data/research_v3/results')
    out_dir.mkdir(parents=True, exist_ok=True)

    if model_tag == 'gemini':
        print(f'[gemini] calling... (input {len(content)} chars)')
        r = call_gemini(content)
        if 'error' in r:
            print(f'[gemini] ERROR: {r["error"]}')
            sys.exit(1)
        text = r['candidates'][0]['content']['parts'][0]['text']
        out = out_dir / f'{stem}__gemini_{ts}.md'
        out.write_text(f'# Gemini research ({ts}) — {stem}\n\n{text}\n')
        print(f'[gemini] saved: {out}')

    elif model_tag == 'o3':
        print(f'[o3] calling... (input {len(content)} chars)')
        r = call_openai(content)
        if 'error' in r:
            print(f'[o3] ERROR: {r["error"]}')
            sys.exit(1)
        text = r['choices'][0]['message']['content']
        out = out_dir / f'{stem}__o3_{ts}.md'
        out.write_text(f'# o3 research ({ts}) — {stem}\n\n{text}\n\n## usage\n{json.dumps(r.get("usage", {}), indent=2)}\n')
        print(f'[o3] saved: {out}')

    else:
        print(f'unknown model_tag: {model_tag}')
        sys.exit(1)


if __name__ == '__main__':
    main()
