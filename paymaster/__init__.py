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
- `paymaster.packing.plan_bundle_nonce_chain(document)` 按账户 nonce 链约束
  的批量打包规划。
- `paymaster.packing.plan_bundle_sender_nonce_chain(document)` 合并 sender
  配额与 nonce 链约束的批量打包规划。
- `paymaster.packing.plan_bundle_sender_budget(document)` 按 sender 聚合 Gas
  与费用预算的批量打包规划。
- `paymaster.packing.plan_bundle_sender_budget_with_usage(document)` 叠加跨
  批次 sender 累计用量的 sender 预算批量打包规划。
- `paymaster.packing.plan_bundle_sender_budget_with_nonce_state(document)`
  叠加跨批次 sender 累计用量与 nonce 状态的 sender 预算批量打包规划。
- `paymaster.batch_state.advance_batch_state(document)` 在上述规划结果上
  结转纯内存批次状态（nextSenderUsage 与 nextSenderNonceState）。
- `paymaster.batch_sequence.plan_batch_sequence(document)` 在单批状态结
  转基线上把 UserOperation 按序分入多个 bundle，并返回下轮 sender 用量与
  nonce 状态。
"""

from paymaster.packing import (
    plan_bundle,
    plan_bundle_cost_first,
    plan_bundle_max_count,
    plan_bundle_nonce_chain,
    plan_bundle_nonce_unique,
    plan_bundle_sender_budget,
    plan_bundle_sender_budget_with_nonce_state,
    plan_bundle_sender_budget_with_usage,
    plan_bundle_sender_fair,
    plan_bundle_sender_nonce_chain,
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
    "plan_bundle_nonce_chain",
    "plan_bundle_sender_nonce_chain",
    "plan_bundle_sender_budget",
    "plan_bundle_sender_budget_with_usage",
    "plan_bundle_sender_budget_with_nonce_state",
    "advance_batch_state",
    "plan_batch_sequence",
]


def __getattr__(name: str):
    # 惰性导入：避免 `python -m paymaster.batch_state` 或
    # `python -m paymaster.batch_sequence` 时包初始化先把该模块装入
    # sys.modules，触发 runpy 的 "found in sys.modules" RuntimeWarning。
    if name == "advance_batch_state":
        from paymaster.batch_state import advance_batch_state

        return advance_batch_state
    if name == "plan_batch_sequence":
        from paymaster.batch_sequence import plan_batch_sequence

        return plan_batch_sequence
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__version__ = "0.1.0"
