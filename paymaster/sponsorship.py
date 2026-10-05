"""ERC-4337 v0.7 Gas 代付决策。

公开入口 ``evaluate_sponsorship(request, policy)``：

- 入参为已解析的 JSON 值，函数不修改入参。
- ``request`` 与 ``paymaster.validation.validate`` 的入参格式相同，先走现有
  静态校验；校验失败时原样返回既有错误，不判断代付。
- ``policy`` 为代付策略，恰好含 ``budgetWei`` 与 ``maxTotalGas`` 两个键，
  均为规范 quantity 且大于 0。
- 返回值永远是字典，不抛业务异常：
  - request 或 policy 结构性失败：``{"ok": False, "error": {"code", "path",
    "message"}}``；
  - 其他结果：``{"ok": True, "decision": {...}}``，``decision`` 含
    ``approved``、``reason``、``totalGas``、``estimatedCostWei`` 与
    ``effectiveGasPriceWei``，其中三个数值为十进制字符串。

policy 错误码：

- ``E_POLICY_INVALID_FIELD``  policy 根不是对象，或字段值非法
- ``E_POLICY_MISSING_FIELD``  缺少必需键
- ``E_POLICY_UNKNOWN_FIELD``  出现未知键

本模块不验签、不模拟执行、不访问节点、数据库或文件。
"""

from __future__ import annotations

from typing import Any

from paymaster.validation import (
    _MAX_QUANTITY_HEX_DIGITS,
    _QUANTITY_RE,
    _pointer,
    validate,
)

E_POLICY_INVALID_FIELD = "E_POLICY_INVALID_FIELD"
E_POLICY_MISSING_FIELD = "E_POLICY_MISSING_FIELD"
E_POLICY_UNKNOWN_FIELD = "E_POLICY_UNKNOWN_FIELD"

REASON_OK = "OK"
REASON_GAS_LIMIT = "E_GAS_LIMIT"
REASON_BUDGET = "E_BUDGET"

# policy 的必需键，顺序即检查顺序（缺键与字段值均按此顺序报错）。
_POLICY_FIELDS = ("budgetWei", "maxTotalGas")


def _error(code: str, path: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "path": path, "message": message}}


def _validate_policy(policy: Any) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """校验代付策略，返回 (规范化字段字典, None) 或 (None, 错误字典)。

    检查顺序：根类型、必需键、未知键、字段值（budgetWei 先于 maxTotalGas）。
    """
    if not isinstance(policy, dict):
        return None, _error(
            E_POLICY_INVALID_FIELD, "", "policy must be an object"
        )

    for name in _POLICY_FIELDS:
        if name not in policy:
            return None, _error(
                E_POLICY_MISSING_FIELD,
                _pointer(name),
                f"missing required field {name!r}",
            )

    for key in policy:
        if key not in _POLICY_FIELDS:
            return None, _error(
                E_POLICY_UNKNOWN_FIELD, _pointer(key), f"unknown field {key!r}"
            )

    normalized: dict[str, str] = {}
    for name in _POLICY_FIELDS:
        value = policy[name]
        if (
            not isinstance(value, str)
            or not _QUANTITY_RE.match(value)
            or len(value) - 2 > _MAX_QUANTITY_HEX_DIGITS
        ):
            return None, _error(
                E_POLICY_INVALID_FIELD,
                _pointer(name),
                f"invalid quantity value for field {name!r}",
            )
        if int(value, 16) == 0:
            return None, _error(
                E_POLICY_INVALID_FIELD,
                _pointer(name),
                f"field {name!r} must be greater than 0",
            )
        normalized[name] = value.lower()
    return normalized, None


def evaluate_sponsorship(request: Any, policy: Any) -> dict[str, Any]:
    """评估是否为给定 UserOperation 请求代付 Gas。

    只返回字典，不抛业务异常。不验签、不模拟执行、不访问外部系统。
    """
    result = validate(request)
    if not result["ok"]:
        return result

    policy_fields, err = _validate_policy(policy)
    if err is not None:
        return err
    assert policy_fields is not None

    user_op = result["normalized"]["userOperation"]
    context = result["normalized"]["context"]
    total_gas = (
        int(user_op["callGasLimit"], 16)
        + int(user_op["verificationGasLimit"], 16)
        + int(user_op["preVerificationGas"], 16)
        + int(user_op.get("paymasterVerificationGasLimit", "0x0"), 16)
        + int(user_op.get("paymasterPostOpGasLimit", "0x0"), 16)
    )
    # 以基础费率估算：min(maxFeePerGas, baseFeePerGas + maxPriorityFeePerGas)。
    effective_gas_price_wei = min(
        int(user_op["maxFeePerGas"], 16),
        int(context["baseFeePerGas"], 16)
        + int(user_op["maxPriorityFeePerGas"], 16),
    )
    estimated_cost_wei = total_gas * effective_gas_price_wei

    if total_gas > int(policy_fields["maxTotalGas"], 16):
        approved, reason = False, REASON_GAS_LIMIT
    elif estimated_cost_wei > int(policy_fields["budgetWei"], 16):
        approved, reason = False, REASON_BUDGET
    else:
        approved, reason = True, REASON_OK

    return {
        "ok": True,
        "decision": {
            "approved": approved,
            "reason": reason,
            "totalGas": str(total_gas),
            "estimatedCostWei": str(estimated_cost_wei),
            "effectiveGasPriceWei": str(effective_gas_price_wei),
        },
    }
