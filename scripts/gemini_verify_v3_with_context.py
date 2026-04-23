"""
gemini_verify_v3_with_context.py — ゆうさくさん全文脈を含めた Gemini 独立再検証

前回（v3_verify_20260422_153403）は context 不足で一般論評価だった失態を訂正する。
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
    raise RuntimeError('GEMINI_API_KEY not found')


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
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': {'code': e.code, 'message': e.read().decode()[:500]}}


def build_prompt() -> str:
    v3 = Path('/Users/yuusakuichio/trading/data/research/org_redesign_v3_20260422.md').read_text()
    return f"""あなたは独立した redteam AI です（Gemini Flash / Google 製）。

## 🚨 前回検証の失態
前回（同日 153403 JST）、ソラ（Claude）はあなたに Sora Lab の context を伝えずに評価依頼しました。
あなたは一般論で判定し以下を推奨:
- agent 常時 2 体
- 全書き直し中止・シナリオ C（リファクタリング）推奨
- 2026-06-20 日付削除

**これらはゆうさくさん固有の context（資金制約・期限・既存コード負債）を知らずに出した一般論判定**でした。
今回は以下の全 context を踏まえて再評価してください。

## プロジェクト固有の context

### オーナー
- ゆうさくさん（奈良・会社員・家族持ち・非エンジニア・コード書けない）
- 目標: **2027/04 までに月 300 万円の不労所得**
- 中間マイルストーン: **2026-10 に月 60 万円達成**（退職+FX撤退の損益分岐点）
- Bot 初期元本 120 万円・預金 100 万円（不可侵）
- MFFU Flex 既購入（月額固定費発生中）
- 外部 LLM は収益化前なので月額ほぼゼロ運用要求（Gemini Free Tier + OpenAI 都度）

### バグなし絶対規律（今日確定）
> 「バグなしの作り方が大前提、絶対ね！！一度も間違えないでね、これから！」
> 「やり直しは無駄な作業なのでバグなしの方針で」
> 「バクリスクは絶対にあげない、それは目標実現が遠のく選択だから」

### autonomous 95% の定義
- 日常取引判断の 95% は完全自動
- 5% は人間承認（Kill switch 解除 / プロップルール変更反映 / 新戦略採用）
- 月 3-5 件承認想定
- 「95% 介入なし」ではなく「95% 自動+5% 承認ゲート」

### 既存コード実測（実測値・codebase_metrics_20260422.md）
- spy_bot.py: 18,858 行
- chronos_bot.py: 4,724 行
- silent except（raise なし）: 559 / 1,782 = 31.4%（業界健全値 5%）
- MI（Maintainability Index）: 0.00 / 0.00（Microsoft Red 底辺）
- _main_loop 循環複雑度: 338（推奨 10 の 34 倍）
- テストカバレッジ本体: 21%（Mock 濫用率 88%）
- 循環 import: 0（救済可能）・既存テスト 2,928 件

### 今日の大規模調査結論（19 本レポート・合計 344 件外部知見）
- 最小版推定: Atlas 3,000 + Chronos 1,250 + 共通 1,330 = 5,580 行
- 書き直し成功率: 20-30%（22 事例）
- バグゼロは不可達・Catastrophic barrier が現実的目標
- 5 agent 独立結論: 完全書き直し NO・段階分解 YES（目標関数が「リスク最小化」の場合）
- **ゆうさくさんの目標関数は「バグなし絶対 + 自動化 95% + 2027/04 期限」** で、段階分解 85% では構造的に到達困難という論理で全書き直し採用

### ゆうさくさん確定判断（今日・2026-04-22）
- 全コード書き直し（既存は参照のみ・新コード atlas_v3/ chronos_v3/ common_v3/）
- 異機種 LLM 導入 OK（月額ほぼゼロ）
- ソラの管理者必須（Auditor がソラも監督）
- agent 数削減（Miller 7±2 以下）
- Gemini Pro は Free Tier 不可 → Flash 主・重要時 OpenAI o3 都度課金

### ゆうさくさんの今日の状態
- 朝 8:17 から連続 7 時間以上の判断継続（疲弊気味）
- 前セッション応答途絶事故（画像送信で prompt cache 崩壊）を経験
- Chronos 44h 放置と ソラ 9 度目虚偽完了を同日に発覚

## 対象文書（組織再々設計案 v3 サマリ）

{v3}

## 🎯 再評価依頼

上記全 context を踏まえた上で、以下を独立評価してください:

1. **全書き直し決定は正しいか？**（silent except 31.4%・MI 0.00 の既存を許容しながらリファクタリングで目標関数 [バグなし+95%+2027/04] 達成可能か）
2. **agent 常時 2 体提案の現実性**（MFFU ルール遵守 + ペーパー検証 + 実装 + 監視を 2 体で回せるか）
3. **2027/04 期限の達成可能性**（既存資産 + 資金制約を踏まえて）
4. **autonomous 95%+5% の現実性**（Free Tier + 少額 OpenAI で）
5. **バグなし絶対 vs 現実**の具体的トレードオフ提案
6. **あなた自身（Gemini Flash）の判定限界**（Pro ではない・context window 限界等）

## 応答形式（JSON・日本語）
```json
{{
  "overall_verdict_for_yuusaku_goal": "ゆうさくさん目標関数での判定 (GO/CONDITIONAL-GO/NO-GO)",
  "full_rewrite_vs_refactor_revised": "ゆうさくさん文脈を踏まえた全書き直し vs シナリオC の再評価",
  "agent_count_2_feasibility": "常時 2 体の現実性（具体的な無理な仕事を列挙）",
  "deadline_feasibility_2027_04": "2027/04 月 300 万達成可能性（確率付き）",
  "deadline_feasibility_2026_10": "2026-10 月 60 万達成可能性",
  "autonomous_95_feasibility": "月額ほぼゼロで 95%+5% の現実性",
  "bug_zero_vs_reality_tradeoff": "バグなし絶対と現実の具体的トレードオフ案",
  "gemini_flash_self_limitations": "自身の判定限界の誠実な開示",
  "previous_verification_errors": "前回の自分の判定で訂正すべき箇所",
  "recommended_actions_next_7_days": ["次 7 日で採るべきアクション"]
}}
```

JSON 以外の文字禁止。sycophancy 禁止。ゆうさくさんの目標関数を毀損する推奨は却下。"""


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


def main():
    prompt = build_prompt()
    print(f'[info] prompt length: {len(prompt)} chars')
    print('[info] calling Gemini flash with full context...')

    result = call_gemini(prompt)
    jst = timezone(timedelta(hours=9))
    ts = datetime.now(jst).strftime('%Y%m%d_%H%M%S')

    out_dir = Path('/Users/yuusakuichio/trading/data/governance/gemini_verify')
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / f'v3_verify_contexted_raw_{ts}.json'
    raw_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    if 'error' in result:
        print(f'[FAIL] {result["error"]}')
        sys.exit(2)

    text = result['candidates'][0]['content']['parts'][0]['text']
    usage = result.get('usageMetadata', {})
    print(f'[info] usage: {usage}')

    verdict = extract_json(text)
    if verdict is None:
        md_path = out_dir / f'v3_verify_contexted_{ts}.md'
        md_path.write_text(f'# Gemini 再検証 (JSON 抽出失敗・生テキスト)\n\n{text}\n')
        print(f'[WARN] JSON not parsed, saved as md: {md_path}')
        return

    verdict_path = out_dir / f'v3_verify_contexted_{ts}.json'
    verdict_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2))
    print(f'[info] parsed verdict saved: {verdict_path}')

    md_path = out_dir / f'v3_verify_contexted_{ts}.md'
    lines = [
        f'# Gemini 再検証（ゆうさくさん全文脈込み・{ts} JST）',
        '',
        f'**Verdict**: {verdict.get("overall_verdict_for_yuusaku_goal", "UNKNOWN")}',
        '',
    ]
    for key, label in [
        ('previous_verification_errors', '前回の誤判定・訂正すべき箇所'),
        ('full_rewrite_vs_refactor_revised', '全書き直し vs リファクタリング（文脈反映後）'),
        ('agent_count_2_feasibility', 'agent 常時 2 体の現実性'),
        ('deadline_feasibility_2027_04', '2027/04 月 300 万達成可能性'),
        ('deadline_feasibility_2026_10', '2026-10 月 60 万達成可能性'),
        ('autonomous_95_feasibility', 'autonomous 95%+5% の現実性'),
        ('bug_zero_vs_reality_tradeoff', 'バグなし絶対 vs 現実のトレードオフ提案'),
        ('gemini_flash_self_limitations', 'Gemini Flash 自身の限界（誠実開示）'),
    ]:
        lines.extend([f'## {label}', str(verdict.get(key, '-')), ''])
    lines.extend(['## 次 7 日のアクション'])
    for a in verdict.get('recommended_actions_next_7_days', []):
        lines.append(f'- {a}')

    md_path.write_text('\n'.join(lines))
    print(f'[info] markdown: {md_path}')
    print(f'\n[VERDICT] {verdict.get("overall_verdict_for_yuusaku_goal", "UNKNOWN")}')


if __name__ == '__main__':
    main()
