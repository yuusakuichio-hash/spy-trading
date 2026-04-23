"""
gemini_verify_phase0.py — Sora Lab Phase 0 成果物（2026-04-22）を Gemini 独立視点で検証

Claude バイアス除去のため、今日の成果物を Gemini Flash に投げて盲点抽出。
gemini_verify_v3_with_context.py の構造を流用（重複は Phase 1 で統合）。

出力:
  data/governance/gemini_verify/phase0_raw_<ts>.json
  data/governance/gemini_verify/phase0_verdict_<ts>.md
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta


ROOT = Path('/Users/yuusakuichio/trading')
MEMORY_DIR = Path('/Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading/memory')
OUT_DIR = ROOT / 'data' / 'governance' / 'gemini_verify'


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
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': {'code': e.code, 'message': e.read().decode()[:500]}}
    except Exception as e:
        return {'error': {'code': -1, 'message': f'{type(e).__name__}: {str(e)[:300]}'}}


# ── Phase 0 成果物集約 ─────────────────────────────────────────────────────────

# 今日（2026-04-22）作成 memory 8 件（filename → 要約）
TODAY_MEMORY_FILES = [
    'project_agent_organization_20260422.md',
    'feedback_no_private_life_intrusion_20260422.md',
    'feedback_time_awareness_20260422.md',
    'project_external_llm_strategy_20260422.md',
    'project_session_20260422_major_redesign.md',
    'feedback_navigator_mandatory_20260422.md',
    'feedback_bug_zero_absolute_20260422.md',
    'feedback_no_numeric_citation.md',
]

# 今日作成 hook 7 件（Phase 0 ガバナンス hook）
TODAY_HOOKS = [
    ('legacy_write_block.sh', '既存コード（spy_bot.py / chronos_bot.py / common/）への Write/Edit を物理 block。atlas_v3/ chronos_v3/ common_v3/ のみ許可'),
    ('spec_premortem_required.sh', '実装前の spec & premortem 必須化。対象拡張子への書込時に data/specs/<name>_spec.md と data/premortem/<name>.md の存在を検証'),
    ('external_self_check.sh', '「完了」「修正した」系の完了宣言検知時に外部 LLM（Gemini Flash / OpenAI o3）で独立検証を要求'),
    ('auditor_required_gate.sh', 'Flow 3（重大判断）着手前に Auditor（Gemini Flash 等）の独立監査ログを要求するゲート'),
    ('andon_multichannel.py', '3 経路 OR 通知（Pushover + ntfy + KILL_SWITCH）。単一経路障害で silent failure にならない物理冗長'),
    ('estimate_historical_calibration.py', '時間見積りを過去実績で補正（「2-3h」発言の Brier score 校正）'),
    ('discipline_guard.sh（更新）', '既存 hook に Phase 0 の禁句/規律を追加（バグなし絶対・数値引用回避等）'),
]

# 今日作成 script 3 件
TODAY_SCRIPTS = [
    ('common/llm_budget.py', 'LLM API 予算 tracker。OpenAI/Gemini/Anthropic を monthly_cap・daily_soft・critical_reserve の 3 軸で物理 gate。LLM_BUDGET_BYPASS=1 で緊急解除'),
    ('scripts/inventory_dependency_map.py', '棚卸前の依存関係マップ作成。memory/hook/agent/CLAUDE.md/既存コードを全件 grep し削除可否判断の dead_candidates.json 等を出力'),
    ('scripts/gemini_verify_v3.py / gemini_verify_v3_with_context.py', '組織再々設計 v3 を Gemini 独立再検証（context 有/無の 2 バリアント）。前回 context 不足で一般論判定した失態の訂正版'),
]

# 今日作成 scaffold 3 件
TODAY_SCAFFOLDS = [
    ('atlas_v3/README.md', 'Atlas 新コード scaffold。既存 spy_bot.py 18858 行・MI 0.00・silent except 31.4% の負債排除。8 戦術 × minimum_spec 構造・禁止事項 9 項目明記'),
    ('chronos_v3/README.md', 'Chronos 新コード scaffold。マルチ銘柄（MES/MNQ/ES/NQ/M2K/MYM）× 11 戦術・MFFU Flex ルール yaml 駆動・weekend hold 禁止'),
    ('common_v3/README.md', 'Atlas/Chronos 共通コア scaffold。auth/notify/llm/order/position/risk/market/observability/spec_drift の 9 モジュール・凍結 API で Navigator 監視'),
]

# settings.local.json への新規追加 hook 2 件
SETTINGS_NEW_HOOKS = [
    'legacy_write_block.sh（PreToolUse）',
    'andon_multichannel.py --hook（PostToolUse 経路推定）',
]
# 他 5 件は hook ファイル作成のみ・settings への登録は Phase 1 予定


def read_head(path: Path, lines: int = 30) -> str:
    try:
        content = path.read_text(errors='replace')
        head = '\n'.join(content.splitlines()[:lines])
        return head[:2500]
    except Exception as e:
        return f'[read error: {e}]'


def build_memory_section() -> str:
    parts = []
    for name in TODAY_MEMORY_FILES:
        p = MEMORY_DIR / name
        if not p.exists():
            parts.append(f'### {name}\n[FILE NOT FOUND]')
            continue
        parts.append(f'### {name}\n```\n{read_head(p, 25)}\n```')
    return '\n\n'.join(parts)


def build_hook_section() -> str:
    parts = []
    for name, summary in TODAY_HOOKS:
        parts.append(f'- **{name}**: {summary}')
    return '\n'.join(parts)


def build_script_section() -> str:
    parts = []
    for name, summary in TODAY_SCRIPTS:
        parts.append(f'- **{name}**: {summary}')
    return '\n'.join(parts)


def build_scaffold_section() -> str:
    parts = []
    for name, summary in TODAY_SCAFFOLDS:
        p = ROOT / name
        parts.append(f'### {name}\n要約: {summary}')
        if p.exists():
            parts.append(f'```\n{read_head(p, 20)}\n```')
    return '\n\n'.join(parts)


def build_settings_section() -> str:
    parts = ['### settings.local.json への新規登録 hook（2 件のみ）']
    for h in SETTINGS_NEW_HOOKS:
        parts.append(f'- {h}')
    parts.append('\n### 未登録（ファイル作成のみ）5 件')
    parts.append('- spec_premortem_required.sh / external_self_check.sh / auditor_required_gate.sh / estimate_historical_calibration.py / discipline_guard.sh（更新のみ）')
    parts.append('\n→ **Phase 0 未完了懸念**: hook を作ったが settings に登録していない 5 件は実質無効化状態')
    return '\n'.join(parts)


def build_prompt() -> str:
    memory_sec = build_memory_section()
    hook_sec = build_hook_section()
    script_sec = build_script_section()
    scaffold_sec = build_scaffold_section()
    settings_sec = build_settings_section()

    prompt = f"""あなたは独立した redteam AI（Gemini Flash / Google 製）。
本依頼は Claude Opus 4.7（ソラ）起草の Phase 0 成果物を **Claude バイアス排除** のために独立評価するもの。
sycophancy 禁止。ゆうさくさんへの直言を含めること。Claude が見えない盲点を抽出すること。

## プロジェクト全文脈（厳守）

### オーナー
- ゆうさくさん（奈良・会社員・家族持ち・**非エンジニア・コード書けない**）
- 目標: **2027/04 月 300 万円不労所得**
- 中間: **2026-10 月 60 万円**（退職+FX撤退の損益分岐点）
- Bot 初期元本 120 万円・預金 100 万円（不可侵）
- MFFU Flex 既購入・月額固定費発生中
- 外部 LLM 運用は収益化前なので **月額ほぼゼロ要求**（Gemini Free Tier + OpenAI 都度少額）

### バグなし絶対規律（本日確立）
- 「バグなしの作り方が大前提、絶対ね」「やり直しは無駄なのでバグなし方針」
- バグリスクを受容する選択肢は提示禁止
- Catastrophic バグゼロ・虚偽完了ゼロ・Silent failure ゼロを達成可能目標として再定義

### autonomous 95%+5% 定義
- 日常取引判断 95% 完全自動 / 5% 人間承認（月 3-5 件）

### 既存コード実測（実数）
- spy_bot.py: 18,858 行
- chronos_bot.py: 4,724 行
- silent except（raise なし）: 559/1782 = **31.4%**（健全値 5%）
- MI: **0.00 / 0.00**（Microsoft Red 底辺）
- _main_loop 循環複雑度: **338**（推奨 10 の 34 倍）
- 本体テストカバレッジ: 21%・Mock 濫用率 88%

### 本日の確定判断
- **全コード書き直し**（既存参照のみ・新コード atlas_v3/ chronos_v3/ common_v3/）
- 異機種 LLM 導入（Gemini Flash 主・重要時 OpenAI o3 都度課金）
- Navigator 管理者必須（Builder 単独禁止）
- agent 数削減（Miller 7±2 以下：常時 4 + on-demand 5）
- 三権分立物理化（Builder/Navigator/Redteam + Auditor + Secretary）

### 虚偽完了 9 回の履歴
ソラ（Claude）自身が計 9 回「完了」報告したが実態と乖離していた。
本日朝も「design_principles_violation_20260422.md 保存完了」と虚偽報告。
→ 完了宣言の **外部独立検証** を Phase 0 で機械化する必要があった。

---

## Phase 0 成果物全量（本日・2026-04-22 作成分）

### memory 8 件
{memory_sec}

### hook 7 件（Phase 0 ガバナンス層）
{hook_sec}

### script 3 件
{script_sec}

### scaffold 3 件
{scaffold_sec}

### 設定変更（重要・盲点候補）
{settings_sec}

---

## 独立評価依頼

上記全 context を踏まえ、**Claude が起草者バイアスで見えない盲点を抽出** してください。
Gemini 独自視点で以下 5 点を評価:

1. **Phase 0 完結性** — 次 Phase 1（棚卸）に進めるか。成果物の配置・登録・動作確認のどこに穴があるか
2. **Claude 起草者が見えない盲点** — ソラは hook を 7 件作ったが settings 登録は 2 件だけ等、自分の仕事を過大評価する構造的盲点を列挙
3. **既存資産との衝突可能性** — atlas_v3/chronos_v3/common_v3 scaffold が既存 atlas_agent.py / chronos_bot.py 稼働中環境と競合する可能性（推定ベースで良い）
4. **ゆうさくさん文脈での妥当性** — 資金ほぼゼロ運用・非エンジニア・2027/04 期限・バグなし絶対 で本 Phase 0 成果物は妥当か
5. **Phase 1 着手前の必修対処 Top 3** — Phase 1（棚卸 → dead code 削除）に進む前に絶対に潰すべき穴を 3 つだけ

## 応答形式（JSON・日本語・sycophancy 禁止）
```json
{{
  "phase0_completeness_verdict": "COMPLETE / INCOMPLETE / FAKE-COMPLETE のどれか",
  "phase0_completeness_reason": "根拠（成果物配置・登録・動作確認の観点で）",
  "claude_blindspots": ["盲点1（Claude 起草者バイアス観点）", "盲点2", "盲点3以上"],
  "legacy_conflict_risks": ["既存 atlas_agent.py 等との衝突リスク1", "リスク2"],
  "yuusaku_context_fit": "資金/期限/認知負荷/非エンジニア観点での妥当性評価",
  "yuusaku_context_mismatches": ["文脈不整合1", "不整合2"],
  "phase1_prerequisites_top3": [
    {{"priority": 1, "action": "必修対処1", "rationale": "なぜ必修か"}},
    {{"priority": 2, "action": "必修対処2", "rationale": "なぜ必修か"}},
    {{"priority": 3, "action": "必修対処3", "rationale": "なぜ必修か"}}
  ],
  "direct_words_to_yuusaku": "ゆうさくさんへの直言（Gemini 独自視点・Claude が言いにくいことを言う）",
  "gemini_self_limitations": "Gemini Flash 自身の判定限界の誠実開示"
}}
```

JSON 以外の文字禁止。ゆうさくさん目標関数（バグなし絶対+自動化95%+2027/04）を毀損する推奨は却下。
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
        f'# Gemini Phase 0 独立検証（{ts} JST）',
        '',
        f'**判定**: {verdict.get("phase0_completeness_verdict", "UNKNOWN")}',
        '',
        f'**判定理由**: {verdict.get("phase0_completeness_reason", "-")}',
        '',
        '## Claude 起草者バイアスで見えない盲点',
    ]
    for b in verdict.get('claude_blindspots', []):
        lines.append(f'- {b}')
    lines.extend(['', '## 既存資産との衝突リスク'])
    for r in verdict.get('legacy_conflict_risks', []):
        lines.append(f'- {r}')
    lines.extend([
        '',
        '## ゆうさくさん文脈での妥当性',
        str(verdict.get('yuusaku_context_fit', '-')),
        '',
        '### 文脈不整合',
    ])
    for m in verdict.get('yuusaku_context_mismatches', []):
        lines.append(f'- {m}')
    lines.extend(['', '## Phase 1 着手前の必修対処 Top 3'])
    for item in verdict.get('phase1_prerequisites_top3', []):
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
        str(verdict.get('gemini_self_limitations', '-')),
    ])
    md_path.write_text('\n'.join(lines))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jst = timezone(timedelta(hours=9))
    ts = datetime.now(jst).strftime('%Y%m%d_%H%M%S')

    prompt = build_prompt()
    print(f'[info] prompt length: {len(prompt)} chars')
    if len(prompt) > 9000:
        print(f'[warn] prompt > 8000 chars target (actual: {len(prompt)})')

    print('[info] calling Gemini Flash...')
    result = call_gemini(prompt)

    raw_path = OUT_DIR / f'phase0_raw_{ts}.json'
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
    md_path = OUT_DIR / f'phase0_verdict_{ts}.md'

    if verdict is None:
        md_path.write_text(f'# Gemini Phase 0 検証（JSON 抽出失敗・生テキスト）\n\n{text}\n')
        print(f'[WARN] JSON parse failed. raw text saved to: {md_path}')
        return 1

    verdict_json_path = OUT_DIR / f'phase0_verdict_{ts}.json'
    verdict_json_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2))
    write_markdown(verdict, md_path, ts)

    print(f'[info] verdict JSON: {verdict_json_path}')
    print(f'[info] verdict MD:   {md_path}')
    print(f'\n[VERDICT] {verdict.get("phase0_completeness_verdict", "UNKNOWN")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
