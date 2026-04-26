"""
gemini_verify_v3.py — 組織再々設計案 v3 の Gemini 独立検証 (P0-0)

redteam 起草の org_redesign_v3_20260422.md を Gemini Flash に投げて、
Claude 起草者が気づけない盲点・欠陥を抽出する。
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta


def load_env():
    env_path = Path('/Users/yuusakuichio/trading/.env')
    for line in env_path.read_text().splitlines():
        if line.startswith('GEMINI_API_KEY='):
            return line.split('=', 1)[1].strip()
    raise RuntimeError('GEMINI_API_KEY not found in .env')


def call_gemini(prompt: str, model: str = 'gemini-flash-latest') -> dict:
    api_key = load_env()
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
    body = json.dumps({
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {
            'temperature': 0.2,
            'maxOutputTokens': 4096,
        }
    }).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'X-goog-api-key': api_key,
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': {'code': e.code, 'message': e.read().decode()[:500]}}


def build_prompt(v3_content: str) -> str:
    return f"""あなたは独立した redteam AI です（Gemini / Google 製）。
Claude（Anthropic 製）が起草した以下の組織再々設計案を独立視点で攻撃的に精査してください。

## 背景
- これは Sora Lab というトレーディング Bot プロジェクトの組織体制設計案
- 起草者は Claude Opus 4.7
- 過去 8 度の虚偽完了があり、Claude 自身のバイアスが起草に入っている可能性が高い
- Red Team 自身が「別機種独立検証なしで GO は NO-GO」と明記している

## 依頼
Claude 起草者が気づけない盲点・欠陥を指摘してください。特に以下の観点：

1. **CCF（Common Cause Failure）除去**：異機種 LLM 導入で本当に除去されるか？ 同じ web corpus で学習された LLM 群は本当に独立か？
2. **認知負荷（Miller 7±2）**：常時 4 agent・on-demand 5 agent でゆうさくさん認知は持つか？
3. **時間見積もり**：Phase 0 今日中 / Phase 1 10-13 営業日 / 本番移行 2026-06-20 は現実的か？
4. **Sentinel daemon（非 LLM）** に過度の期待をしていないか？
5. **全コード書き直し方針**の Challenger 型楽観バイアス
6. **Claude 起草者が気づけない Blind Spot**

## 応答形式（必ず JSON・日本語）
```json
{{
  "overall_verdict": "GO" or "CONDITIONAL-GO" or "NO-GO",
  "critical_findings": ["具体的欠陥 1", "具体的欠陥 2", ...],
  "high_findings": ["..."],
  "medium_findings": ["..."],
  "blindspots_claude_cannot_see": ["Claude 独特のバイアスで見逃している可能性が高い盲点 1", "..."],
  "timing_realism": "時間見積もりの現実性評価",
  "ccf_removal_real_effectiveness": "異機種 LLM で CCF は本当に除去されるかの率直評価",
  "recommended_modifications": ["変更すべき点 1", "..."],
  "disagree_with_redteam": "Red Team 起草に賛同しない箇所"
}}
```

## 対象文書

{v3_content}

---

厳しく・具体的に・sycophancy 禁止。JSON 以外の文字を返さないでください。"""


def extract_json(text: str) -> dict | None:
    """Gemini 応答から JSON 部分を抽出"""
    # ```json ... ``` ブロック
    if '```json' in text:
        start = text.find('```json') + 7
        end = text.find('```', start)
        if end > start:
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
    # ```  ... ``` ブロック
    if '```' in text:
        start = text.find('```') + 3
        end = text.find('```', start)
        if end > start:
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
    # そのままJSON
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


def main():
    v3_path = Path('/Users/yuusakuichio/trading/data/research/org_redesign_v3_20260422.md')
    if not v3_path.exists():
        print(f'ERROR: {v3_path} not found', file=sys.stderr)
        sys.exit(1)

    v3_content = v3_path.read_text()
    prompt = build_prompt(v3_content)

    print(f'[info] prompt length: {len(prompt)} chars')
    print(f'[info] calling Gemini (gemini-flash-latest)...')

    result = call_gemini(prompt)
    jst = timezone(timedelta(hours=9))
    ts = datetime.now(jst).strftime('%Y%m%d_%H%M%S')

    out_dir = Path('/Users/yuusakuichio/trading/data/governance/gemini_verify')
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / f'v3_verify_raw_{ts}.json'
    raw_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f'[info] raw response saved: {raw_path}')

    if 'error' in result:
        print(f'[FAIL] Gemini API error: {result["error"]}', file=sys.stderr)
        sys.exit(2)

    try:
        text = result['candidates'][0]['content']['parts'][0]['text']
        usage = result.get('usageMetadata', {})
        print(f'[info] usage: {usage}')
    except (KeyError, IndexError) as e:
        print(f'[FAIL] unexpected Gemini response structure: {e}', file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False)[:500], file=sys.stderr)
        sys.exit(3)

    verdict = extract_json(text)
    if verdict is None:
        # JSON 抽出失敗時は生テキストを保存
        md_path = out_dir / f'v3_verify_{ts}.md'
        md_path.write_text(f'# Gemini 独立検証 (生テキスト・JSON 抽出失敗)\n\n{text}\n')
        print(f'[WARN] JSON not extracted, saved as markdown: {md_path}')
        sys.exit(0)

    verdict_path = out_dir / f'v3_verify_{ts}.json'
    verdict_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2))
    print(f'[info] parsed verdict saved: {verdict_path}')

    md_path = out_dir / f'v3_verify_{ts}.md'
    lines = [
        f'# Gemini 独立検証結果（{ts} JST）',
        '',
        f'**Model**: gemini-flash-latest',
        f'**Input**: {v3_path}',
        f'**Verdict**: {verdict.get("overall_verdict", "UNKNOWN")}',
        '',
        '## CRITICAL 所見',
    ]
    for f in verdict.get('critical_findings', []):
        lines.append(f'- {f}')
    lines.extend(['', '## HIGH 所見'])
    for f in verdict.get('high_findings', []):
        lines.append(f'- {f}')
    lines.extend(['', '## MEDIUM 所見'])
    for f in verdict.get('medium_findings', []):
        lines.append(f'- {f}')
    lines.extend(['', '## Claude が見えていない盲点'])
    for f in verdict.get('blindspots_claude_cannot_see', []):
        lines.append(f'- {f}')
    lines.extend(['', '## 時間見積もり現実性'])
    lines.append(verdict.get('timing_realism', '-'))
    lines.extend(['', '## CCF 除去の実効性（独立評価）'])
    lines.append(verdict.get('ccf_removal_real_effectiveness', '-'))
    lines.extend(['', '## 推奨修正'])
    for f in verdict.get('recommended_modifications', []):
        lines.append(f'- {f}')
    lines.extend(['', '## Red Team への反論'])
    lines.append(verdict.get('disagree_with_redteam', '-'))

    md_path.write_text('\n'.join(lines))
    print(f'[info] markdown report saved: {md_path}')

    print(f'\n[VERDICT] {verdict.get("overall_verdict", "UNKNOWN")}')
    print(f'  CRITICAL: {len(verdict.get("critical_findings", []))}')
    print(f'  HIGH: {len(verdict.get("high_findings", []))}')
    print(f'  MEDIUM: {len(verdict.get("medium_findings", []))}')


if __name__ == '__main__':
    main()
