"""
gemini_verify_spec_v3_r2.py — 仕様書 v3 R2/R2a 独立検証（Gemini Flash / Navigator 代替）

R1 改訂後 Redteam が CONDITIONAL-GO + 新規 CRITICAL 4 件（R-01〜R-04）指摘。
R2 改訂で R-01（gamma_scalp Type D）/R-02（Future 型統一）/R-03（AST hook 明記）/R-04（yaml bootstrap）反映。
R2a 改訂で R2-02（TacticBase ABC 追加）反映。
R2 改訂後 Redteam 再検証で CONDITIONAL-GO + 新規 CRITICAL 3 件（R2-01/R2-02/R2-03）+ P0-S1 8 件指摘。
Redteam 自己警告: 「3 サイクルで毎回 CRITICAL 新生 = 収束せず変質・CCF 内側の盲点残存」。

Gemini として:
- R2/R2a の 5 観点評価
- Redteam の「非収束・CCF 限界」警告への独立意見
- GO / CONDITIONAL-GO / NO-GO 判定

出力:
  data/governance/gemini_verify/spec_v3_r2_raw_<ts>.json
  data/governance/gemini_verify/spec_v3_r2_verdict_<ts>.json
  data/governance/gemini_verify/spec_v3_r2_verdict_<ts>.md
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
REDTEAM_R2_PATH = ROOT / 'data' / 'governance' / 'redteam_spec_v3_r2_audit_20260423.md'
PREV_GEMINI_R1_PATH = OUT_DIR / 'spec_v3_r1_verdict_20260423_030518.md'

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
    prev_gemini = read_text(PREV_GEMINI_R1_PATH)
    redteam_r2 = read_text(REDTEAM_R2_PATH)

    prompt = f"""あなたは独立した Navigator 代替 AI（Gemini Flash / Google 製）。
R1 改訂時にあなた自身が **GO 判定** を出した仕様書 v3 について、その後:
- Redteam R1 audit → CONDITIONAL-GO + 新規 CRITICAL 4 件（R-01 gamma_scalp Type 誤分類 / R-02 Future 型曖昧 / R-03 AST hook 未明記 / R-04 yaml bootstrap 不在）
- R2 改訂で R-01〜R-04 反映（Type D 新設 / concurrent.futures.Future 統一 / AST hook 明記 / yaml bootstrap 5 step 追加）
- R2a 微修正で R2-02（TacticBase ABC 追加）反映
- Redteam R2 audit → CONDITIONAL-GO + 新規 CRITICAL 3 件（R2-01 AST hook 物理不在 / R2-02 TacticBase 不在（R2a で解消済）/ R2-03 yaml null 起動不能）+ P0-S1 8 件
- **Redteam 自己警告**: 「3 サイクルで毎回 CRITICAL 新生 = 収束せず変質・CCF 内側（Claude Opus 同士）の盲点構造残存・外部独立レビュー必須」

**あなたの責務**: R2/R2a 改訂を Gemini 視点で独立再検証し、Phase 1 C-2 凍結可否を判定。特に **Redteam の「非収束・CCF 限界」警告を Gemini としてどう評価するか** を最重要観点として答えよ。

---

## 前回（R1）あなた自身の判定（原文抜粋）

```markdown
{prev_gemini}
```

---

## Redteam R2 audit 全文（CONDITIONAL-GO + P0-S1 8 件 + Red Team 自己限界 6 件）

```markdown
{redteam_r2}
```

---

## R2/R2a 改訂後の仕様書 v3（3 本全文）

{spec_sec}

---

## プロジェクト文脈（再掲）

- オーナー: ゆうさくさん（非エンジニア・奈良会社員・家族持ち）
- 目標: 2027/04 月 300 万不労所得・中間 2026-10 月 60 万
- MFFU Flex 既購入・月額固定費発生中
- バグなし絶対最優先・Silent failure ゼロ・虚偽完了ゼロ
- Phase 1 C-2 spec 凍結判定 → GO なら Phase 2 Builder 着手
- 外部 LLM 月額ほぼゼロ要求（Gemini 無料枠主・OpenAI 都度）

---

## 独立再評価の観点（sycophancy 禁止・5 観点）

### 観点 1: Type D 新設 + TacticBase ABC の debug 容易性
- atlas_spec B5 で Type D（Hybrid）追加 + 全戦術が TacticBase ABC 継承必須
- 非エンジニアのゆうさくさんが障害時に「どの Type の戦術で落ちたか」を 5 分以内で特定できる設計か
- Protocol 4 種（A/B/C/D）+ ABC 1 種の構造は認知負荷を許容範囲内に収めているか

### 観点 2: TaskExecutor Future 型統一 + Iterator 化の並列実装意図明確度
- common_spec B16 で `concurrent.futures.Future` 固定 + `map → Iterator[T]`
- Builder が sync 前提で書きながら将来 async 差替え可能という設計は spec から読み取れるか
- Redteam が指摘した「AsyncExecutor と concurrent.futures.Future の型矛盾」の妥当性をあなたはどう見るか

### 観点 3: AST hook 明記で Kill Switch/Idempotency 物理強制が実装可能に読めるか
- common_spec B16 L478-482 で `executor_sync_only_guard.sh` の 4 パターン AST 検査が明記
- しかし Redteam は「hook 実ファイル物理不在」を R2-01 CRITICAL として指摘
- Gemini 視点で「spec に書いた = 物理化できる」と「hook 物理未整備のまま凍結は危険」のどちらが正しいか

### 観点 4: yaml bootstrap で Builder が Phase 2 に迷わず着手できるか
- chronos_spec B5 L143-169 で 5 step 手順 + schema_version 1.0 + null 値 MFFURuleMissingError raise
- Redteam R2-03: 「null 状態で Chronos 起動不能 → Builder が一時ハードコード or mock 埋込 → shadow 復活」攻撃シナリオ
- dry_run モード契約不在のまま凍結可能か

### 観点 5（最重要）: Redteam の「非収束・CCF 限界」警告への Gemini 独立意見
Redteam 主張:
> 3 サイクル検証で CRITICAL 11 → 4 → 3 と減少は事実だが、**毎サイクルで新規 CRITICAL が 3-4 件新生している**（初回 11 / R1 4 新規 / R2 3 新規）。これは**収束ではなく変質**。同じ穴を埋めては別の穴が開く構造が変わっていない → CCF 内側（Claude Opus 同士）の盲点残存の強い証拠。外部独立レビュー必須。

Gemini として:
- この「非収束・変質」論は妥当か、それとも過度に悲観的か
- Phase 1 C-2 を凍結して Phase 2 で物理化（P0-S1 8 件実装）に移るべきか、それとも spec に追加修正必要か
- Gemini 自身も Google 製で別 persona・別 CCF だが、実機検証なしで 3 本 spec を読むだけで検出できる盲点には限界があることを誠実開示

---

## 応答形式（JSON・日本語・sycophancy 厳禁）

```json
{{
  "overall_verdict": "GO / CONDITIONAL-GO / NO-GO のいずれか",
  "verdict_delta_from_r1": "R1 GO → R2/R2a で何が変わったか（1-3 文）",
  "observation1_type_d_tactic_base": {{
    "adequate": "YES / PARTIAL / NO",
    "reason": "Type D 新設 + TacticBase ABC の debug 容易性評価"
  }},
  "observation2_future_iterator": {{
    "adequate": "YES / PARTIAL / NO",
    "reason": "Future 型統一 + Iterator 化の並列実装意図明確度"
  }},
  "observation3_ast_hook": {{
    "adequate": "YES / PARTIAL / NO",
    "reason": "AST hook 明記で物理強制可能か・hook 実ファイル不在をどう見るか"
  }},
  "observation4_yaml_bootstrap": {{
    "adequate": "YES / PARTIAL / NO",
    "reason": "yaml bootstrap で Builder が迷わず着手できるか・dry_run 不在の扱い"
  }},
  "observation5_redteam_non_convergence_warning": {{
    "gemini_position": "AGREE / PARTIAL_AGREE / DISAGREE",
    "reasoning": "Redteam の非収束・CCF 限界警告への Gemini 独立意見（2-4 文）",
    "recommendation": "Phase 2 物理化移行 / spec 追加修正 / 外部独立レビュー必須 のどれを Gemini として推奨するか"
  }},
  "additional_must_fix_count": "R2/R2a に対して Gemini が追加で要求する MUST-FIX 件数（0-3）",
  "additional_must_fix_items": [
    {{"priority": 1, "action": "Gemini 独自の追加 MUST-FIX（なければ空配列）", "rationale": "なぜ凍結阻止か"}}
  ],
  "gemini_on_redteam_p0_8items": "Redteam P0-S1 8 件の妥当性評価（1-3 文・Phase 2 Sprint 1 実装優先度）",
  "interface_clarity_score": "1-5 段階（R1 で 5/5 だった・R2/R2a 新規要素含めてどうか）",
  "non_engineer_debuggability": "Type D/ABC/Future/Iterator/AST hook/yaml bootstrap の追加が非エンジニア debug 容易性を損なっていないか（1-3 文）",
  "direct_words_to_yuusaku": "ゆうさくさんへの直言（spec 凍結して Phase 2 に移るべきか・Gemini 独自視点・Redteam と立場が違う場合は明示）",
  "gemini_self_limitations": "Gemini Flash 自身の判定限界（実機検証なし・Google CCF 内側でも別 persona だが完璧ではない・誠実開示）"
}}
```

JSON 以外の文字禁止。
**GO 判定条件**: 5 観点全て adequate=YES + additional_must_fix=0 + Redteam 警告への対応方針（Phase 2 物理化 or 外部レビュー）が明確。
**CONDITIONAL-GO 判定条件**: 観点のいずれか PARTIAL / additional_must_fix ≥1 / Redteam 警告に部分同意。
**NO-GO 判定条件**: 観点のいずれか NO / 根本修正必須。
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
        f'# Gemini 仕様書 v3 R2/R2a 独立再検証（{ts} JST・Navigator 代替）',
        '',
        f'**総合判定**: {verdict.get("overall_verdict", "UNKNOWN")}',
        '',
        f'**R1 からの差分**: {verdict.get("verdict_delta_from_r1", "-")}',
        '',
        f'**Interface 明確性スコア**: {verdict.get("interface_clarity_score", "-")} / 5',
        '',
        '## 5 観点評価',
        '',
    ]

    for key, label in [
        ('observation1_type_d_tactic_base', '観点 1: Type D + TacticBase ABC debug 容易性'),
        ('observation2_future_iterator', '観点 2: Future 型統一 + Iterator 化'),
        ('observation3_ast_hook', '観点 3: AST hook 物理強制'),
        ('observation4_yaml_bootstrap', '観点 4: yaml bootstrap 手順'),
    ]:
        item = verdict.get(key, {})
        if isinstance(item, dict):
            lines.append(f'### {label}')
            lines.append(f'- adequate: **{item.get("adequate", "-")}**')
            lines.append(f'- 理由: {item.get("reason", "-")}')
            lines.append('')

    obs5 = verdict.get('observation5_redteam_non_convergence_warning', {})
    if isinstance(obs5, dict):
        lines.append('### 観点 5（最重要）: Redteam 非収束・CCF 限界警告への Gemini 独立意見')
        lines.append(f'- Gemini の立場: **{obs5.get("gemini_position", "-")}**')
        lines.append(f'- 根拠: {obs5.get("reasoning", "-")}')
        lines.append(f'- 推奨: {obs5.get("recommendation", "-")}')
        lines.append('')

    lines.extend([
        '## 追加 MUST-FIX（Gemini 独自）',
        f'件数: {verdict.get("additional_must_fix_count", "-")}',
        '',
    ])
    for item in verdict.get('additional_must_fix_items', []) or []:
        if isinstance(item, dict):
            lines.append(f'### Priority {item.get("priority", "?")}: {item.get("action", "-")}')
            lines.append(f'根拠: {item.get("rationale", "-")}')
            lines.append('')

    lines.extend([
        '## Redteam P0-S1 8 件の妥当性評価',
        str(verdict.get('gemini_on_redteam_p0_8items', '-')),
        '',
        '## 非エンジニアの debug 容易性',
        str(verdict.get('non_engineer_debuggability', '-')),
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
    if not REDTEAM_R2_PATH.exists():
        print(f'[FAIL] redteam R2 audit not found: {REDTEAM_R2_PATH}')
        return 2
    if not PREV_GEMINI_R1_PATH.exists():
        print(f'[FAIL] prev gemini R1 verdict not found: {PREV_GEMINI_R1_PATH}')
        return 2

    prompt = build_prompt()
    print(f'[info] prompt length: {len(prompt)} chars')

    print('[info] calling Gemini Flash (gemini-flash-latest)...')
    result = call_gemini(prompt)

    raw_path = OUT_DIR / f'spec_v3_r2_raw_{ts}.json'
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
    md_path = OUT_DIR / f'spec_v3_r2_verdict_{ts}.md'

    if verdict is None:
        md_path.write_text(f'# Gemini 仕様書 v3 R2/R2a 検証（JSON 抽出失敗・生テキスト）\n\n{text}\n')
        print(f'[WARN] JSON parse failed. raw text saved to: {md_path}')
        print(f'[USAGE_INPUT] {usage.get("promptTokenCount", 0)}')
        print(f'[USAGE_OUTPUT] {usage.get("candidatesTokenCount", 0)}')
        return 1

    verdict_json_path = OUT_DIR / f'spec_v3_r2_verdict_{ts}.json'
    verdict_json_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2))
    write_markdown(verdict, md_path, ts)

    print(f'[info] verdict JSON: {verdict_json_path}')
    print(f'[info] verdict MD:   {md_path}')
    print(f'\n[VERDICT] {verdict.get("overall_verdict", "UNKNOWN")}')
    print(f'[Additional MUST-FIX] {verdict.get("additional_must_fix_count", "?")} items')
    print(f'[Interface clarity] {verdict.get("interface_clarity_score", "-")} / 5')
    print(f'[USAGE_INPUT] {usage.get("promptTokenCount", 0)}')
    print(f'[USAGE_OUTPUT] {usage.get("candidatesTokenCount", 0)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
