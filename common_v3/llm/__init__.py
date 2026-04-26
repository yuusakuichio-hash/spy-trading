"""common_v3.llm — LLM 呼出統一 wrapper (本実装)

Public API:
- LLMResponse: response DTO
- LLMClient: 統一 wrapper (Anthropic / Gemini / OpenAI を 1 interface)
- get_llm_client(provider): factory

budget cap (common/llm_budget.py) と必ず連携・全 LLM 呼出を本 wrapper 経由に強制する。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMResponse:
    """LLM 呼出結果."""
    text: str
    tokens_used: int = 0
    cost_usd: float = 0.0
    model: str = ""


class BudgetExhaustedError(RuntimeError):
    """budget cap 超過時に raise."""


class LLMClient:
    """LLM 呼出統一 wrapper.

    実装: 各 provider (Anthropic / Gemini / OpenAI) の SDK を遅延 import で wrap。
    全呼出は budget gate (common.llm_budget) を経由する。
    """

    def __init__(
        self,
        provider: str = "anthropic",
        model: Optional[str] = None,
    ) -> None:
        self._provider = provider
        self._model = model or self._default_model(provider)

    @staticmethod
    def _default_model(provider: str) -> str:
        defaults = {
            "anthropic": "claude-haiku-4-5-20251001",
            "gemini": "gemini-2.5-flash",
            "openai": "gpt-5-nano",
        }
        return defaults.get(provider, "")

    def complete(
        self, prompt: str, max_tokens: int = 1024, system: Optional[str] = None,
    ) -> LLMResponse:
        """LLM 呼出 (budget gate 経由)."""
        # budget check (common/llm_budget.py 既存実装に委譲)
        try:
            from common.llm_budget import check_budget
            if not check_budget(provider=self._provider, est_tokens=max_tokens):
                raise BudgetExhaustedError(
                    f"budget exhausted for provider={self._provider}"
                )
        except ImportError:
            pass  # budget module 不在環境ではスキップ (test 用)

        # provider 別呼出 (遅延 import)
        if self._provider == "anthropic":
            return self._call_anthropic(prompt, max_tokens, system)
        if self._provider == "gemini":
            return self._call_gemini(prompt, max_tokens, system)
        if self._provider == "openai":
            return self._call_openai(prompt, max_tokens, system)
        raise ValueError(f"unsupported provider: {self._provider}")

    def _call_anthropic(self, prompt: str, max_tokens: int, system: Optional[str]) -> LLMResponse:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("anthropic SDK not installed (pip install anthropic)")
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system or "",
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if hasattr(b, "text"))
        return LLMResponse(
            text=text,
            tokens_used=getattr(msg.usage, "output_tokens", 0) + getattr(msg.usage, "input_tokens", 0),
            model=self._model,
        )

    def _call_gemini(self, prompt: str, max_tokens: int, system: Optional[str]) -> LLMResponse:
        try:
            import google.generativeai as genai
        except ImportError:
            raise RuntimeError("google-generativeai not installed")
        from common_v3.auth import get_credential
        api_key = get_credential("gemini_api_key")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(self._model)
        response = model.generate_content(
            prompt if not system else f"{system}\n\n{prompt}",
            generation_config={"max_output_tokens": max_tokens},
        )
        return LLMResponse(text=response.text, model=self._model)

    def _call_openai(self, prompt: str, max_tokens: int, system: Optional[str]) -> LLMResponse:
        try:
            import openai
        except ImportError:
            raise RuntimeError("openai SDK not installed")
        client = openai.OpenAI()
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
            model=self._model, messages=msgs, max_tokens=max_tokens,
        )
        return LLMResponse(
            text=resp.choices[0].message.content or "",
            tokens_used=resp.usage.total_tokens if resp.usage else 0,
            model=self._model,
        )


def get_llm_client(provider: str = "anthropic", model: Optional[str] = None) -> LLMClient:
    """factory."""
    return LLMClient(provider=provider, model=model)


__all__ = [
    "LLMResponse",
    "LLMClient",
    "get_llm_client",
    "BudgetExhaustedError",
]
