"""common_v3.llm — LLM 呼出統一 wrapper + budget 連携 (β-2 配線 skeleton)

Responsibility
--------------
Anthropic / Gemini / OpenAI / Local LLM の各呼出を統一 wrapper で扱い、
budget cap (common/llm_budget.py) と必ず連動させる:

1. **AnthropicClient** (claude-opus / sonnet / haiku)
2. **GeminiClient** (Free Tier Flash + Pro 都度課金)
3. **OpenAIClient** (gpt-5-nano / o3 系・Hard cap $15/月)
4. **LocalLLMClient** (将来の ollama / lmstudio 連携)

## Why

現状散在問題:
- ``scripts/gemini_verify_phase0.py`` ``gemini_verify_v3.py`` 等の個別 client
- ``common/llm_budget.py`` の budget cap が暗黙の前提 (caller 任せ)
- API rate limit / 429 / token 漏洩の管理が分散

OpenAI 月額 $15 hard cap (memory: project_external_llm_strategy_20260422.md) を
確実に enforce するには **全 LLM 呼出が単一 client 経由** が必須。

## Public API (β-2 後段で実装予定)

- ``LLMClient.complete(model, prompt, max_tokens=...) -> LLMResponse``
  - budget gate 経由で cap 超過時は ``BudgetExhaustedError`` raise
  - critical_reserve 連携 ($3/月の緊急用予算)
  - rate limit 自動 backoff
- ``LLMResponse``: text + tokens_used + cost_usd
- ``estimate_cost(model, prompt) -> float``  # 事前見積もり

## How to apply

β-2 後段で:
1. ``scripts/gemini_*.py`` を本 client 経由に統一
2. ``redteam_review.py`` も統一 wrapper 経由に
3. budget cap の hard enforcement 実装 (現状 soft warning)
4. monthly billing report 自動生成

現状は skeleton。既存 ``common/llm_budget.py`` を re-export してパス統一のみ提供。
"""

__all__ = []


def __getattr__(name):
    if name == "LLMBudget":
        try:
            from common.llm_budget import LLMBudget
            return LLMBudget
        except ImportError:
            raise AttributeError(
                "LLMBudget は common/llm_budget.py 必須 (β-2 後段で common_v3 移植)"
            )
    raise AttributeError(f"module 'common_v3.llm' has no attribute {name!r}")
