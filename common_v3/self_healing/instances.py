"""common_v3/self_healing/instances.py

spec ref: data/specs/v3/common_spec_v3_20260422.md B14 L382-L383
production-ready 名義 CircuitBreaker インスタンス

利用側は本 module から import 必須。
直接 CircuitBreaker() instantiation の hook 警告は Sprint 1 で物理化予定
(ADR-008: data/decisions/ADR-008-frozen-design-final-enforcement.md)。

spec 仕様:
  - tradovate_breaker: fail_max=3, auto_recovery=False (default)
  - moomoo_breaker:    fail_max=5, auto_recovery=False (default)
"""
from common_v3.self_healing.circuit_breaker import CircuitBreaker

tradovate_breaker: CircuitBreaker = CircuitBreaker(
    name="tradovate",
    fail_max=3,
    # auto_recovery=False (default): CircuitBreaker.__init__ runtime guard で強制
)

moomoo_breaker: CircuitBreaker = CircuitBreaker(
    name="moomoo",
    fail_max=5,
    # auto_recovery=False (default): CircuitBreaker.__init__ runtime guard で強制
)

__all__ = ["tradovate_breaker", "moomoo_breaker"]
