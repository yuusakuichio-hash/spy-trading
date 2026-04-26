"""
gemini_verify_spec_v3_r1.py — 仕様書 v3 R1 改訂版独立検証（Gemini Flash）

前回 Gemini が CONDITIONAL-GO + MUST-FIX 3 件を出した仕様書 v3 を、R1 改訂で
- Gemini MUST-FIX 3 件（Storage / asyncio / EICAS フォーマット）
- Redteam CRITICAL 11 件
- 案B（sync/async 両対応 ExecutorProvider 抽象化 B16）
反映したバージョンについて、「凍結してよいか」を独立再検証する。

出力:
  data/governance/gemini_verify/spec_v3_r1_raw_<ts>.json
  data/governance/gemini_verify/spec_v3_r1_verdict_<ts>.json
  data/governance/gemini_verify/spec_v3_r1_verdict_<ts>.md
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
FIX_PLAN_PATH = ROOT / 'data' / 'governance' / 'spec_v3_fix_plan_20260423.md'
PREV_VERDICT_PATH = OUT_DIR / 'spec_v3_verdict_20260423_023223.md'

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
        with urllib.request.urlopen(req, timeout=240) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': {'code': e.code, 'message': e.read().decode()[:500]}}
    except Exception as e:
        return {'error': {'code': -1, 'message': f'{type(e).__name__}: {str(e)[:300]}'}}


def read_text(path: Path) -> str:
    try:
        return path.read_text(errors='replace')
    except Exception as e:
        return f'[read error: {e}]'


def build_spec_section() -> str:
    parts = []
    for name, path in SPEC_FILES:
        content = read_text(path)
        parts.append(f'## {name} — `{path.name}`（{len(content)} chars）\n\n```markdown\n{content}\n```')
    return '\n\n---\n\n'.join(parts)


def build_prompt() -> str:
    spec_sec = build_spec_section()
    prev_verdict = read_text(PREV_VERDICT_PATH)
    fix_plan = read_text(FIX_PLAN_PATH)

    prompt = f"""あなたは独立した redteam AI（Gemini Flash / Google 製）。
前回（2026-04-23 02:32 JST）あなた自身が **CONDITIONAL-GO + MUST-FIX 3 件** を出した仕様書 v3 について、
R1 改訂が「凍結可能レベルまで」反映できているかを **厳格再判定** してください。

## 前回のあなたの判定（原文・フル）

```markdown
{prev_verdict}
```

## R1 改訂計画（修正方針 draft）

```markdown
{fix_plan}
```

## R1 改訂の主要変更点（起草者自己申告）

1. **Gemini MUST-FIX Fix 1**: `common_v3/storage/persistence.py` B15 新設
   - StorageBackend Protocol / SQLite + JSONL ハイブリッド
   - パス規約: `data/state_v3/{{orders,positions,idempotency}}.sqlite3` + `{{eicas,kill_switch_audit}}.jsonl`

2. **Gemini MUST-FIX Fix 2**: 非同期ポリシー確定（B16 に格上げ）
   - 同期（sync）既定 + `concurrent.futures.ThreadPoolExecutor` 明示
   - 案B 採用: `TaskExecutor` Protocol で sync/async 差替え可能
   - `asyncio` 直使用は `common_v3/executor/async_impl.py` のみ許可（linter 物理 block 候補）

3. **Gemini MUST-FIX Fix 3**: `EICASRecord` dataclass 固定（B3 に反映）
   - JSONL append / timestamp / level / title / message / source / metadata
   - 保存先 `data/state_v3/eicas.jsonl`

4. **Redteam CRITICAL 11 件反映**（主要なもの）:
   - C-01: Idempotency path 衝突回避（`common_v3/idempotency/store.py` に確定）
   - C-02: Kill Switch activate() 戻り値 bool 冪等化 + FirmScopedKillSwitch 統合経路明記
   - C-03: EICAS Warning と Kill Switch 完全分離（Therac-25 型 interlock 誤動作回避）
   - C-04: MarketDataResult[T] で silent default 禁止（例外 or stale flag 必須）
   - C-05: CircuitBreaker auto_recovery=False 既定・人間承認のみ復帰（Gemini 直言反映）
   - C-06: Deadman beacon path 共通化・3 日 shadow 運用で両方 silent 死を防止
   - C-07: Atlas 戦術 Protocol を 3 分類（Type A/B/C）— 単一 interface 強制による silent failure 回避
   - C-08: percentile 自体も動的算出（PercentileSelector）— C-08 違反 API 禁止
   - C-09: SymbolWhitelist を独立 Interface 化（B14b）・SPX 事故再発防止
   - C-10: MFFU Profit Target/Max Loss を yaml 単一真実源 + MFFURuleMissingError
   - C-11: Part F 未確定事項の解消状況明記

5. **案B（sync/async 両対応抽象化・B16）**:
   - `TaskExecutor` Protocol（sync/async 差替え）
   - `SyncExecutor` 既定 / `AsyncExecutor` は Phase 3 以降のみ
   - Circuit Breaker / Idempotency / Kill Switch は同期経路で必須実行

---

## R1 改訂後の仕様書 v3（3 本全文）

{spec_sec}

---

## プロジェクト文脈（前回判定時と同一・再掲）

### オーナー
- ゆうさくさん（奈良・会社員・家族持ち・**非エンジニア**）
- 目標: **2027/04 月 300 万円不労所得**・中間 **2026-10 月 60 万円**
- MFFU Flex 既購入・月額固定費発生中
- 外部 LLM は **月額ほぼゼロ要求**（Gemini 無料枠主・OpenAI 都度少額）

### バグなし絶対規律（絶対）
- Catastrophic バグゼロ / 虚偽完了ゼロ / Silent failure ゼロ
- 「やり直しは無駄なのでバグなし方針」

### 既存コード実測（v3 で書き直し対象）
- silent except 率 31.4% / MI 0.00 / _main_loop CC=338 / 本体カバレッジ 21%
- 虚偽完了 9 回の履歴

### Phase 1 C-2 の位置づけ
- **本検証で GO ならば凍結**→ Phase 2 Builder 実装着手
- Interface 変更は Phase 2 で高コスト（相互影響）
- 穴があれば凍結せず再改訂

---

## 独立再評価の観点（sycophancy 禁止）

以下を **厳格** に判定せよ:

### 観点 1: 前回 MUST-FIX 3 件の反映度（最重要）
- Fix 1（Storage）: B15 の Interface は Builder が迷わず実装できるか。SQLite/JSONL 責務分離は妥当か。パス規約は衝突しないか
- Fix 2（asyncio）: 案B（TaskExecutor 抽象）は「非エンジニア debug 容易性」を保てるか。それとも過度に抽象化して新たな認知負荷を生んでいるか
- Fix 3（EICASRecord）: JSONL フォーマットは LLM 食わせ前提で十分か。timestamp UTC ISO8601 は妥当か

### 観点 2: Redteam CRITICAL 11 件の反映妥当性
- 11 件それぞれ R1 で適切に interface 化されているか
- 特に C-03（EICAS と Kill Switch 分離）/ C-07（Atlas Protocol 3 分類）/ C-10（MFFU yaml 単一真実源）は「バグなし絶対」の要所
- 反映漏れ・中途半端・新たな抜け道があれば指摘

### 観点 3: 案B（TaskExecutor 抽象）の妥当性
- sync/async 両対応抽象は **非エンジニアにとって debug 容易性を損なわないか**
- Builder が「sync 前提」で書きながら将来 async に差替えるという設計は **実際に動くか**（抽象漏れ・leaky abstraction のリスク）
- `asyncio` 禁止ゾーン / 許可ゾーンの境界は明確か

### 観点 4: 非エンジニアのデバッグ容易性（最優先規律）
- R1 で新たに増えた抽象（TaskExecutor / StorageBackend / CircuitBreakerBackend）は
  ゆうさくさんが障害時に **5 分以内でログ読解・原因特定** できる設計か
- 逆に抽象が増えた分「どこで何が起きたか」が追いにくくなっていないか

### 観点 5: 残存 MUST-FIX（凍結阻止項目）
- R1 でも残る重大な凍結阻止項目を 0-3 件で列挙（**なければ 0 件で GO**）

---

## 応答形式（JSON・日本語・sycophancy 禁止）

```json
{{
  "overall_verdict": "GO / CONDITIONAL-GO / NO-GO のいずれか",
  "verdict_delta_from_prev": "前回 CONDITIONAL-GO → R1 で何が変わったか（1-3 文）",
  "fix1_storage_evaluation": {{
    "adequate": "YES / PARTIAL / NO",
    "reason": "Storage Interface B15 が Builder 実装できる粒度か"
  }},
  "fix2_asyncio_evaluation": {{
    "adequate": "YES / PARTIAL / NO",
    "reason": "案B（TaskExecutor 抽象）の debug 容易性評価"
  }},
  "fix3_eicas_evaluation": {{
    "adequate": "YES / PARTIAL / NO",
    "reason": "EICASRecord フォーマット固定の妥当性"
  }},
  "redteam_11_items_coverage": "Redteam CRITICAL 11 件の反映総評（1-3 文）",
  "planB_executor_validity": "案B（TaskExecutor 抽象）の妥当性評価（1-3 文・leaky abstraction 懸念含む）",
  "non_engineer_debuggability": "非エンジニアの debug 容易性評価（1-3 文・抽象増加による認知負荷懸念含む）",
  "interface_clarity_score": "1-5 段階（5=Builder 迷わず実装可 / 1=解釈揺れ多数）",
  "interface_clarity_reason": "スコア根拠",
  "remaining_must_fix": [
    {{"priority": 1, "action": "残存 MUST-FIX1（ない場合は空配列）", "rationale": "なぜ凍結阻止か"}}
  ],
  "new_blind_spots_introduced_by_r1": ["R1 改訂で新たに生じた盲点があれば列挙・なければ空配列"],
  "direct_words_to_yuusaku": "ゆうさくさんへの直言（R1 で凍結してよいか・Gemini 独自視点）",
  "gemini_self_limitations": "Gemini Flash 自身の判定限界の誠実開示"
}}
```

JSON 以外の文字禁止。
**GO 判定条件の目安**: 前回 MUST-FIX 3 件すべて adequate=YES + remaining_must_fix が空 + new_blind_spots が空または軽微。
不足があれば CONDITIONAL-GO（残存 MUST-FIX を列挙）。重大な抜けがあれば NO-GO。
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
        f'# Gemini 仕様書 v3 R1 独立再検証（{ts} JST）',
        '',
        f'**総合判定**: {verdict.get("overall_verdict", "UNKNOWN")}',
        '',
        f'**前回からの差分**: {verdict.get("verdict_delta_from_prev", "-")}',
        '',
        f'**Interface 明確性スコア**: {verdict.get("interface_clarity_score", "-")} / 5',
        '',
        f'**スコア根拠**: {verdict.get("interface_clarity_reason", "-")}',
        '',
        '## 前回 MUST-FIX 3 件の反映度',
        '',
    ]

    for key, label in [
        ('fix1_storage_evaluation', 'Fix 1 (Storage / B15)'),
        ('fix2_asyncio_evaluation', 'Fix 2 (asyncio / 案B TaskExecutor)'),
        ('fix3_eicas_evaluation', 'Fix 3 (EICASRecord)'),
    ]:
        item = verdict.get(key, {})
        if isinstance(item, dict):
            lines.append(f'### {label}')
            lines.append(f'- adequate: **{item.get("adequate", "-")}**')
            lines.append(f'- 理由: {item.get("reason", "-")}')
            lines.append('')

    lines.extend([
        '## Redteam CRITICAL 11 件の反映評価',
        str(verdict.get('redteam_11_items_coverage', '-')),
        '',
        '## 案B（TaskExecutor 抽象）の妥当性',
        str(verdict.get('planB_executor_validity', '-')),
        '',
        '## 非エンジニアのデバッグ容易性',
        str(verdict.get('non_engineer_debuggability', '-')),
        '',
        '## 残存 MUST-FIX（凍結阻止項目）',
    ])

    remaining = verdict.get('remaining_must_fix', [])
    if not remaining:
        lines.append('- なし（凍結可能）')
    else:
        for item in remaining:
            if isinstance(item, dict):
                lines.append(f'### Priority {item.get("priority", "?")}: {item.get("action", "-")}')
                lines.append(f'根拠: {item.get("rationale", "-")}')
                lines.append('')

    lines.extend([
        '',
        '## R1 改訂で新たに生じた盲点',
    ])
    new_bs = verdict.get('new_blind_spots_introduced_by_r1', [])
    if not new_bs:
        lines.append('- なし')
    else:
        for b in new_bs:
            lines.append(f'- {b}')

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

    for name, path in SPEC_FILES:
        if not path.exists():
            print(f'[FAIL] spec not found: {name} ({path})')
            return 2
    if not FIX_PLAN_PATH.exists():
        print(f'[FAIL] fix plan not found: {FIX_PLAN_PATH}')
        return 2
    if not PREV_VERDICT_PATH.exists():
        print(f'[FAIL] prev verdict not found: {PREV_VERDICT_PATH}')
        return 2

    prompt = build_prompt()
    print(f'[info] prompt length: {len(prompt)} chars')

    print('[info] calling Gemini Flash (gemini-flash-latest)...')
    result = call_gemini(prompt)

    raw_path = OUT_DIR / f'spec_v3_r1_raw_{ts}.json'
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
    md_path = OUT_DIR / f'spec_v3_r1_verdict_{ts}.md'

    if verdict is None:
        md_path.write_text(f'# Gemini 仕様書 v3 R1 検証（JSON 抽出失敗・生テキスト）\n\n{text}\n')
        print(f'[WARN] JSON parse failed. raw text saved to: {md_path}')
        # usage は返す（予算記録用）
        print(f'[USAGE_INPUT] {usage.get("promptTokenCount", 0)}')
        print(f'[USAGE_OUTPUT] {usage.get("candidatesTokenCount", 0)}')
        return 1

    verdict_json_path = OUT_DIR / f'spec_v3_r1_verdict_{ts}.json'
    verdict_json_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2))
    write_markdown(verdict, md_path, ts)

    print(f'[info] verdict JSON: {verdict_json_path}')
    print(f'[info] verdict MD:   {md_path}')
    print(f'\n[VERDICT] {verdict.get("overall_verdict", "UNKNOWN")}')
    print(f'[Interface clarity] {verdict.get("interface_clarity_score", "-")} / 5')
    print(f'[Remaining MUST-FIX] {len(verdict.get("remaining_must_fix", []))} items')
    print(f'[USAGE_INPUT] {usage.get("promptTokenCount", 0)}')
    print(f'[USAGE_OUTPUT] {usage.get("candidatesTokenCount", 0)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
