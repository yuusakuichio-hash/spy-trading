"""concept_verify_20260423.py — Sora Lab 概念修正版 + 進め方を Gemini + o3 に独立検証依頼"""
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
        'generationConfig': {'temperature': 0.2, 'maxOutputTokens': 4096},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'X-goog-api-key': api_key,
    }, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
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


def build_prompt(content: str) -> str:
    return f"""あなたは独立した redteam AI です。以下の Sora Lab 概念修正版 + 進め方案に対して、「抜け」「問題点」「進め方の改善点」を率直に指摘してください。

## 背景
- Sora Lab: ゆうさくさんの目標達成のための自律型 AI チーム（金融・SNS・音楽・全方位）
- Atlas / Chronos: 金融 Bot（現在の主対象・手段の 1 つ）
- 2026-04-22 大転換で「v3 は一からやり直し」方針
- 起草者は Claude Opus 4.7（過去 8 度虚偽完了歴・Redteam/Gemini 外部検証必須の規律）

## 依頼項目
1. 概念修正版に抜けはあるか（Sora Lab / Atlas / Chronos の定義）
2. 進め方に問題あるか（軸 A 実装手法 50 個調査 + 軸 B 優秀トレーダー調査 + 両軸シンセシス）
3. Claude 起草者が気づきにくい盲点
4. 優先順位（A 先 or B 先 or 並列）の妥当性
5. 工数見積 3-4 日の現実性

## 応答形式（JSON・日本語）
{{
  "concept_gaps": ["抜け 1", "抜け 2"],
  "process_issues": ["問題 1"],
  "blindspots_claude_cannot_see": ["..."],
  "priority_recommendation": "軸 A 先 / 軸 B 先 / 並列 + 理由",
  "timing_realism": "3-4 日見積の現実性評価",
  "additional_considerations": ["..."],
  "overall_verdict": "GO" | "CONDITIONAL-GO" | "NO-GO",
  "rationale": "..."
}}

---

## 概念修正版 + 進め方（検証対象）

{content}
"""


def main():
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/Users/yuusakuichio/trading/data/research_v3/concept_verification_input_20260423.md')
    content = input_path.read_text()
    prompt = build_prompt(content)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = Path('/Users/yuusakuichio/trading/data/governance')
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f'[verify] calling gemini... (prompt {len(prompt)} chars)')
    g = call_gemini(prompt)
    if 'error' in g:
        print(f'[gemini] ERROR: {g["error"]}')
    else:
        try:
            gc = g['candidates'][0]['content']['parts'][0]['text']
            out_g = out_dir / f'concept_verify_gemini_{ts}.md'
            out_g.write_text(f'# Gemini concept verify ({ts})\n\n{gc}\n')
            print(f'[gemini] saved: {out_g}')
        except Exception as exc:
            print(f'[gemini] parse error: {exc} / raw={g}')

    print(f'[verify] calling o3...')
    o = call_openai(prompt)
    if 'error' in o:
        print(f'[o3] ERROR: {o["error"]}')
    else:
        try:
            oc = o['choices'][0]['message']['content']
            out_o = out_dir / f'concept_verify_o3_{ts}.md'
            out_o.write_text(f'# o3 concept verify ({ts})\n\n{oc}\n\n## usage\n{json.dumps(o.get("usage", {}), indent=2)}\n')
            print(f'[o3] saved: {out_o}')
        except Exception as exc:
            print(f'[o3] parse error: {exc} / raw={o}')


if __name__ == '__main__':
    main()
