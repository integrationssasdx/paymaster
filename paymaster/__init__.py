"""Paymaster: ERC-4337 账户抽象服务（UserOperation 校验、Gas 代付与打包）。

当前公开入口：

- `paymaster.validation.validate(request)` 静态校验 ERC-4337 v0.7
  UserOperation 请求。
- `paymaster.sponsorship.evaluate_sponsorship(request, policy)` 在校验通过的
  请求上做 Gas 代付决策。
- `paymaster.packing.plan_bundle(document)` 在代付决策基线上做批量打包规划。
- `paymaster.packing.plan_bundle_cost_first(document)` 成本优先的批量打包规划。
- `paymaster.packing.plan_bundle_max_count(document)` 以入选数量最大化为目标的
  批量打包规划。
- `paymaster.packing.plan_bundle_sender_fair(document)` 按 sender 公平约束的
  批量打包规划。
- `paymaster.packing.plan_bundle_nonce_unique(document)` 避免同账户 nonce
  冲突的批量打包规划。
"""

from paymaster.packing import (
    plan_bundle,
    plan_bundle_cost_first,
    plan_bundle_max_count,
    plan_bundle_nonce_unique,
    plan_bundle_sender_fair,
)
from paymaster.sponsorship import evaluate_sponsorship
from paymaster.validation import validate

__all__ = [
    "validate",
    "evaluate_sponsorship",
    "plan_bundle",
    "plan_bundle_cost_first",
    "plan_bundle_max_count",
    "plan_bundle_sender_fair",
    "plan_bundle_nonce_unique",
]
__version__ = "0.1.0"
