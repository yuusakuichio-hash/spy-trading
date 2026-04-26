"""
openai_o3_verify.py — Sprint 1 計画の OpenAI o3 独立レビュー

ADR-012（Sprint 1 計画）を o3 に投げて、Claude 起草者の盲点を抽出する。
ADR-001 計画線上の「Sprint 0.5 末 o3 先行レビュー」実装。
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime


def load_env_key(key_name: str) -> str:
    env_path = Path('/Users/yuusakuichio/trading/.env')
    for line in env_path.read_text().splitlines():
        if line.startswith(f'{key_name}='):
            return line.split('=', 1)[1].strip()
    raise RuntimeError(f'{key_name} not found in .env')


def call_openai(prompt: str, model: str = 'o3') -> dict:
    api_key = load_env_key('OPENAI_API_KEY')
    url = 'https://api.openai.com/v1/chat/completions'
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': {'code': e.code, 'message': e.read().decode()[:1000]}}


def build_prompt(plan_content: str) -> str:
    return f"""あなたは独立した redteam AI です（OpenAI / o3）。
Claude（Anthropic 製）が起草した以下の Sprint 1 計画を独立視点で精査してください。

## 背景
- Sora Lab という自動売買 Bot プロジェクトの Phase 2 Sprint 1 計画
- Sprint 0.5（物理化のみ 3 日計画）は 2026-04-23 実測 7h で達成
- Sprint 1 は Bot 取引ロジック + runtime guard 化 + carryover C-001〜C-011 対応
- 2026-04-27 ペーパー 30 日実走開始が目標
- 起草者は Claude Opus 4.7。過去に 8 度の虚偽完了記録あり

## 依頼
Claude 起草者が気づけない盲点・欠陥を指摘してください。特に以下の観点:
1. 工数見積 2-3 日（並列 Builder 3-4 名）の現実性
2. carryover C-001〜C-011 を並列で対応する統合リスク
3. runtime guard 化（frozen design / `@sync_only` / `MFFUFlexRules.__init__` raise 等）の副作用
4. ペーパー開始の前提条件漏れ
5. Bot 取引ロジック v3 実装の設計上の盲点
6. Claude 起草者が気づけない blind spot（同じ web corpus 学習バイアス等）

## 応答形式（必ず JSON・日本語）
{{
  "overall_verdict": "GO" | "CONDITIONAL-GO" | "NO-GO",
  "critical_findings": ["具体的欠陥 1", "具体的欠陥 2"],
  "high_findings": ["..."],
  "medium_findings": ["..."],
  "blindspots_claude_cannot_see": ["Claude 起草者が気づきにくい盲点 1", "..."],
  "timing_realism": "2-3 日見積の現実性評価",
  "carryover_integration_risks": "C-001〜C-011 並列対応の統合リスク",
  "paper_start_prerequisites_missing": ["見逃している前提条件..."],
  "verdict_rationale": "総合判定の根拠"
}}

---

## Sprint 1 計画全文

{plan_content}
"""


def main():
    plan_path = Path('/Users/yuusakuichio/trading/data/decisions/ADR-012-phase2-sprint1-plan.md')
    plan_content = plan_path.read_text()
    prompt = build_prompt(plan_content)

    print(f'[openai_o3_verify] calling o3 (prompt length={len(prompt)} chars)...')
    result = call_openai(prompt, model='o3')

    if 'error' in result:
        print(f'[openai_o3_verify] ERROR: {result["error"]}')
        sys.exit(1)

    content = result['choices'][0]['message']['content']
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = Path(f'/Users/yuusakuichio/trading/data/governance/o3_review_sprint1_{ts}.md')
    out_path.parent.mkdir(parents=True, exist_ok=True)

    meta = {k: v for k, v in result.items() if k != 'choices'}
    out_path.write_text(
        f'# o3 Sprint 1 Review ({ts})\n\n'
        f'Model: o3\n'
        f'Plan: ADR-012\n\n'
        f'---\n\n'
        f'{content}\n\n'
        f'---\n\n'
        f'## Response meta\n\n'
        f'```json\n{json.dumps(meta, indent=2, ensure_ascii=False)}\n```\n'
    )
    print(f'[openai_o3_verify] saved: {out_path}')
    print(f'[openai_o3_verify] usage: {result.get("usage", {})}')
    print(f'[openai_o3_verify] ---content preview---')
    print(content[:800])


if __name__ == '__main__':
    main()
