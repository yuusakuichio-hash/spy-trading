"""
gemini_verify_spec_v3.py — Phase 1 C-2 仕様書 v3 独立検証（Gemini Flash）

common_v3 / atlas_v3 / chronos_v3 の Interface 凍結前検証。
Claude 起草者バイアス排除のため異機種 LLM（Gemini Flash）で独立評価。
gemini_verify_phase0.py の構造を流用。

出力:
  data/governance/gemini_verify/spec_v3_raw_<ts>.json
  data/governance/gemini_verify/spec_v3_verdict_<ts>.json
  data/governance/gemini_verify/spec_v3_verdict_<ts>.md
"""
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta


ROOT = Path('/Users/yuusakuichio/trading')
OUT_DIR = ROOT / 'data' / 'governance' / 'gemini_verify'
SPEC_DIR = ROOT / 'data' / 'specs' / 'v3'

SPEC_FILES = [
    ('common_v3', SPEC_DIR / 'common_spec_v3_20260422.md'),
    ('atlas_v3', SPEC_DIR / 'atlas_spec_v3_20260422.md'),
    ('chronos_v3', SPEC_DIR / 'chronos_spec_v3_20260422.md'),
]


def load_env() -> str:
    env_path = ROOT / '.env'
    for line in env_path.read_text().splitlines():
        if line.startswith('GEMINI_API_KEY='):
            return line.split('=', 1)[1].strip()
    raise RuntimeError('GEMINI_API_KEY not found in .env')


def call_gemini(prompt: str, model: str = 'gemini-flash-latest') -> dict:
    api_key = load_env()
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
    body = json.dumps({
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.2, 'maxOutputTokens': 8192},
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={'Content-Type': 'application/json', 'X-goog-api-key': api_key},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': {'code': e.code, 'message': e.read().decode()[:500]}}
    except Exception as e:
        return {'error': {'code': -1, 'message': f'{type(e).__name__}: {str(e)[:300]}'}}


def read_spec(path: Path) -> str:
    try:
        return path.read_text(errors='replace')
    except Exception as e:
        return f'[read error: {e}]'


def build_spec_section() -> str:
    parts = []
    for name, path in SPEC_FILES:
        content = read_spec(path)
        size = len(content)
        parts.append(f'## {name} — `{path.name}`（{size} chars）\n\n```markdown\n{content}\n```')
    return '\n\n---\n\n'.join(parts)


def build_prompt() -> str:
    spec_sec = build_spec_section()

    prompt = f"""あなたは独立した redteam AI（Gemini Flash / Google 製）。
本依頼は **Claude Opus 4.7（ソラ）起草** の仕様書 v3 を **Claude バイアス排除** のために独立評価するもの。
sycophancy（迎合）禁止。ゆうさくさんへの直言を含めること。Claude 起草者が見えない盲点を抽出すること。

## プロジェクト全文脈（厳守）

### オーナー
- ゆうさくさん（奈良・会社員・家族持ち・**非エンジニア・コード書けない**）
- 目標: **2027/04 月 300 万円不労所得**
- 中間: **2026-10 月 60 万円**（退職+FX撤退の損益分岐点）
- Bot 初期元本 120 万円・預金 100 万円（不可侵）
- MFFU Flex 既購入・月額固定費発生中
- 外部 LLM 運用は収益化前なので **月額ほぼゼロ要求**（Gemini Free Tier 主 + OpenAI 都度少額）

### バグなし絶対規律（本日確立・絶対規律）
- 「バグなしの作り方が大前提、絶対ね」「やり直しは無駄なのでバグなし方針」
- バグリスクを受容する選択肢は提示禁止
- Catastrophic バグゼロ・虚偽完了ゼロ・Silent failure ゼロを達成可能目標として再定義

### 4 層構造戦略（目標月 300 万円の内訳）
```
Bot（収益エンジン）
　× SNS 匿名アカウント（集客エンジン）
　× Collective2（信頼証明ツール）
　× 少人数私募ファンド（収益スケーラー）
```
- Bot 自己運用: 月 15-25 万円（複利）
- C2 サブスク: 月 16-43 万円
- 私募ファンド成功報酬: 月 100-150 万円（AUM 1 億 × 5% × 20%）

### 既存コード実測値（v3 で書き直す対象）
- spy_bot.py: 18,858 行
- chronos_bot.py: 4,724 行
- silent except（raise なし）: 559/1782 = **31.4%**（健全値 5%）
- MI: **0.00 / 0.00**（Microsoft 基準底辺）
- _main_loop 循環複雑度: **338**（推奨 10 の 34 倍）
- 本体テストカバレッジ: 21%・Mock 濫用率 88%

### Phase 1 C-2 dry-run の位置づけ
- **Phase 2 Builder 実装前の Interface 凍結検証**
- common_v3 依存 DAG: atlas_v3 / chronos_v3 は common_v3 の凍結 API のみに依存
- Phase 2 実装中の Interface 変更は高コスト（相互影響で手戻り）
- 本検証で穴があれば **Phase 2 着手前** に潰す

### 虚偽完了 9 回の履歴
ソラ（Claude）自身が計 9 回「完了」報告したが実態と乖離していた。
→ 完了宣言の **外部独立検証** を機械化する必要があった。
→ 本検証はその独立検証の実施そのもの。

---

## 検証対象: 仕様書 v3（3 本全文）

{spec_sec}

---

## 独立評価依頼

上記 3 仕様書を踏まえ、**Claude 起草者バイアスで見えない盲点を抽出** してください。
Gemini 独自視点で以下 5 点を評価:

1. **Interface 凍結の妥当性** — Phase 2 で Builder が迷わず実装できるか。曖昧・未定義・解釈揺れリスクはないか
2. **common_v3 依存 DAG の正しさ** — atlas_v3 / chronos_v3 が common_v3 に依存する方向は正しいか。循環依存・漏れはないか
3. **Claude 起草者が見えない盲点** — 起草者が自分の設計を過大評価する構造的盲点を列挙（例: 命名粒度・責務境界・エラー伝播・test 可能性）
4. **ゆうさくさん文脈での妥当性** — 非エンジニア・2027/04 期限・バグなし絶対・月額ほぼゼロ運用で本仕様書は妥当か
5. **Phase 2 着手前の必修対処 Top 3** — Phase 2 実装に進む前に絶対に潰すべき穴を 3 つだけ

## 応答形式（JSON・日本語・sycophancy 禁止）
```json
{{
  "overall_verdict": "GO / CONDITIONAL-GO / NO-GO のどれか",
  "common_v3_verdict": "common_v3 仕様書の個別評価（1-3 文）",
  "atlas_v3_verdict": "atlas_v3 仕様書の個別評価（1-3 文）",
  "chronos_v3_verdict": "chronos_v3 仕様書の個別評価（1-3 文）",
  "interface_clarity_score": "1-5 段階評価（5=Builder 迷わず実装可 / 1=解釈揺れ多数）",
  "interface_clarity_reason": "スコア根拠",
  "dag_correctness": "common_v3 依存 DAG の正しさ評価（循環・漏れ・方向性）",
  "claude_blind_spots": ["盲点1（Claude 起草者バイアス観点）", "盲点2", "盲点3以上"],
  "yuusaku_context_fit": "資金/期限/認知負荷/非エンジニア観点での妥当性評価",
  "must_fix_before_phase2": [
    {{"priority": 1, "action": "必修対処1", "rationale": "なぜ必修か"}},
    {{"priority": 2, "action": "必修対処2", "rationale": "なぜ必修か"}},
    {{"priority": 3, "action": "必修対処3", "rationale": "なぜ必修か"}}
  ],
  "direct_words_to_yuusaku": "ゆうさくさんへの直言（Gemini 独自視点・Claude が言いにくいこと）",
  "gemini_flash_self_limitations": "Gemini Flash 自身の判定限界の誠実開示（仕様書評価 LLM としての盲点）"
}}
```

JSON 以外の文字禁止。ゆうさくさん目標関数（バグなし絶対+自動化95%+2027/04+月額ほぼゼロ）を毀損する推奨は却下。
"""
    return prompt


def extract_json(text: str):
    for fence in ('```json', '```'):
        if fence in text:
            start = text.find(fence) + len(fence)
            end = text.find('```', start)
            if end > start:
                try:
                    return json.loads(text[start:end].strip())
                except json.JSONDecodeError:
                    pass
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


def write_markdown(verdict: dict, md_path: Path, ts: str) -> None:
    lines = [
        f'# Gemini 仕様書 v3 独立検証（{ts} JST）',
        '',
        f'**総合判定**: {verdict.get("overall_verdict", "UNKNOWN")}',
        '',
        f'**Interface 明確性スコア**: {verdict.get("interface_clarity_score", "-")} / 5',
        '',
        f'**スコア根拠**: {verdict.get("interface_clarity_reason", "-")}',
        '',
        '## 個別仕様書評価',
        '',
        f'### common_v3',
        str(verdict.get('common_v3_verdict', '-')),
        '',
        f'### atlas_v3',
        str(verdict.get('atlas_v3_verdict', '-')),
        '',
        f'### chronos_v3',
        str(verdict.get('chronos_v3_verdict', '-')),
        '',
        '## common_v3 依存 DAG 評価',
        str(verdict.get('dag_correctness', '-')),
        '',
        '## Claude 起草者バイアスで見えない盲点',
    ]
    for b in verdict.get('claude_blind_spots', []):
        lines.append(f'- {b}')
    lines.extend([
        '',
        '## ゆうさくさん文脈での妥当性',
        str(verdict.get('yuusaku_context_fit', '-')),
        '',
        '## Phase 2 着手前の必修対処 Top 3',
    ])
    for item in verdict.get('must_fix_before_phase2', []):
        if isinstance(item, dict):
            lines.append(f'### Priority {item.get("priority", "?")}: {item.get("action", "-")}')
            lines.append(f'根拠: {item.get("rationale", "-")}')
            lines.append('')
        else:
            lines.append(f'- {item}')
    lines.extend([
        '',
        '## ゆうさくさんへの直言（Gemini 独自視点）',
        str(verdict.get('direct_words_to_yuusaku', '-')),
        '',
        '## Gemini Flash 自身の限界（誠実開示）',
        str(verdict.get('gemini_flash_self_limitations', '-')),
    ])
    md_path.write_text('\n'.join(lines))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jst = timezone(timedelta(hours=9))
    ts = datetime.now(jst).strftime('%Y%m%d_%H%M%S')

    # 事前チェック: 3 仕様書の存在確認
    for name, path in SPEC_FILES:
        if not path.exists():
            print(f'[FAIL] spec not found: {name} ({path})')
            return 2

    prompt = build_prompt()
    print(f'[info] prompt length: {len(prompt)} chars')
    if len(prompt) > 30000:
        print(f'[warn] prompt > 30k chars (Free Tier safe but long): {len(prompt)}')

    print('[info] calling Gemini Flash...')
    result = call_gemini(prompt)

    raw_path = OUT_DIR / f'spec_v3_raw_{ts}.json'
    raw_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f'[info] raw saved: {raw_path}')

    if 'error' in result:
        print(f'[FAIL] Gemini API error: {result["error"]}')
        return 2

    try:
        text = result['candidates'][0]['content']['parts'][0]['text']
    except (KeyError, IndexError) as e:
        print(f'[FAIL] response shape unexpected: {e}')
        return 3

    usage = result.get('usageMetadata', {})
    print(f'[info] usage: {usage}')

    verdict = extract_json(text)
    md_path = OUT_DIR / f'spec_v3_verdict_{ts}.md'

    if verdict is None:
        md_path.write_text(f'# Gemini 仕様書 v3 検証（JSON 抽出失敗・生テキスト）\n\n{text}\n')
        print(f'[WARN] JSON parse failed. raw text saved to: {md_path}')
        return 1

    verdict_json_path = OUT_DIR / f'spec_v3_verdict_{ts}.json'
    verdict_json_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2))
    write_markdown(verdict, md_path, ts)

    print(f'[info] verdict JSON: {verdict_json_path}')
    print(f'[info] verdict MD:   {md_path}')
    print(f'\n[VERDICT] {verdict.get("overall_verdict", "UNKNOWN")}')
    print(f'[Interface clarity] {verdict.get("interface_clarity_score", "-")} / 5')
    return 0


if __name__ == '__main__':
    sys.exit(main())
