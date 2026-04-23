"""common_v3.self_healing — Circuit Breaker / 自己治癒系モジュール

Sprint 0.5: runtime guard のみ物理化
Sprint 1 (C-005): frozen design 全面採用 (ADR-008 案 A)
  - __slots__ + __setattr__ override + __init_subclass__ + __reduce__ + __new__ sentinel
  - sentinel.py: module integrity 検証
"""
from common_v3.self_healing.circuit_breaker import (
    CircuitBreakerAutoRecoveryForbidden,
    CircuitBreakerApproverInvalid,
    CircuitBreakerFrozenViolation,
    CircuitBreakerBackend,
    CircuitBreaker,
)
from common_v3.self_healing.instances import tradovate_breaker, moomoo_breaker
from common_v3.self_healing.sentinel import (
    ModuleIntegrityError,
    verify_module_integrity,
    install_monkey_patch_guard,
)

__all__ = [
    "CircuitBreakerAutoRecoveryForbidden",
    "CircuitBreakerApproverInvalid",
    "CircuitBreakerFrozenViolation",
    "CircuitBreakerBackend",
    "CircuitBreaker",
    "tradovate_breaker",
    "moomoo_breaker",
    "ModuleIntegrityError",
    "verify_module_integrity",
    "install_monkey_patch_guard",
]
