"""Paymaster: ERC-4337 账户抽象服务（UserOperation 校验、Gas 代付与打包）。

当前公开入口：`paymaster.validation.validate(request)` 静态校验
ERC-4337 v0.7 UserOperation 请求；`paymaster.sponsorship.evaluate_sponsorship`
(request, policy)` 在同一请求上做纯静态 Gas 代付决策。后续在此基线上增加打包。
"""

from paymaster.sponsorship import evaluate_sponsorship
from paymaster.validation import validate

__all__ = ["validate", "evaluate_sponsorship"]
__version__ = "0.1.0"
