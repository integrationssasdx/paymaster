"""Paymaster: ERC-4337 账户抽象服务（UserOperation 校验、Gas 代付与打包）。

当前公开入口：

- `paymaster.validation.validate(request)` 静态校验 ERC-4337 v0.7
  UserOperation 请求。
- `paymaster.sponsorship.evaluate_sponsorship(request, policy)` 在校验通过的
  请求上做 Gas 代付决策。
- `paymaster.packing.plan_bundle(document)` 对批量代付文档逐项校验并按顺序
  规划打包。
"""

from paymaster.packing import plan_bundle
from paymaster.sponsorship import evaluate_sponsorship
from paymaster.validation import validate

__all__ = ["validate", "evaluate_sponsorship", "plan_bundle"]
__version__ = "0.1.0"
