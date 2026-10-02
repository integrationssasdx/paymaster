"""Gas 代付决策：``evaluate_sponsorship(request, policy)``。

公开入口只处理已解析的 JSON 值，不修改入参：

- ``request`` 沿用 :func:`paymaster.validation.validate` 的格式并先走该校验；
  非法值、字段或组合原样返回既有错误，不判断代付。
- 然后结构性校验 ``policy``：恰好含 ``budgetWei`` 与 ``maxTotalGas``，
  均为规范 quantity 且大于 0。
- request 或 policy 结构性失败返回
  ``{"ok": False, "error": {"code", "path", "message"}}``；
  其余结果返回 ``{"ok": True, "decision": {...}}``。

decision 中 ``totalGas`` 与 ``estimatedCostWei`` 为十进制字符串。
本模块不验签、不模拟链上执行、不访问节点、数据库或文件。
"""

from __future__ import annotations

from typing import Any

from paymaster.validation import (
    _KIND_QUANTITY,
    _pointer,
    _validate_value,
    validate,
)

E_POLICY_INVALID_FIELD = "E_POLICY_INVALID_FIELD"
E_POLICY_MISSING_FIELD = "E_POLICY_MISSING_FIELD"
E_POLICY_UNKNOWN_FIELD = "E_POLICY_UNKNOWN_FIELD"

E_GAS_LIMIT = "E_GAS_LIMIT"
E_BUDGET = "E_BUDGET"
OK = "OK"

# policy 字段顺序即缺键与值检查的顺序。
_POLICY_FIELDS = ("budgetWei", "maxTotalGas")

# totalGas 累加的 UserOperation gas 字段（后两个可选）。
_TOTAL_GAS_FIELDS = (
    "callGasLimit",
    "verificationGasLimit",
    "preVerificationGas",
    "paymasterVerificationGasLimit",
    "paymasterPostOpGasLimit",
)


def _error(code: str, path: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "path": path, "message": message}}


def _validate_policy(policy: Any) -> tuple[dict[str, int] | None, dict[str, Any] | None]:
    """校验 policy，返回 (整数字段字典, None) 或 (None, 错误字典)。

    检查顺序：根类型、必需键（缺键先于未知键）、未知键、字段值
    （依次 budgetWei、maxTotalGas）。
    """
    if not isinstance(policy, dict):
        return None, _error(
            E_POLICY_INVALID_FIELD, "", "policy root must be a JSON object"
        )

    for name in _POLICY_FIELDS:
        if name not in policy:
            return None, _error(
                E_POLICY_MISSING_FIELD,
                _pointer(name),
                f"missing required policy field {name!r}",
            )

    for key in policy:
        if key not in _POLICY_FIELDS:
            return None, _error(
                E_POLICY_UNKNOWN_FIELD,
                _pointer(key),
                f"unknown policy field {key!r}",
            )

    values: dict[str, int] = {}
    for name in _POLICY_FIELDS:
        norm = _validate_value(_KIND_QUANTITY, policy[name])
        if norm is None:
            return None, _error(
                E_POLICY_INVALID_FIELD,
                _pointer(name),
                f"invalid quantity value for policy field {name!r}",
            )
        value = int(norm, 16)
        if value == 0:
            return None, _error(
                E_POLICY_INVALID_FIELD,
                _pointer(name),
                f"policy field {name!r} must be greater than 0",
            )
        values[name] = value
    return values, None


def evaluate_sponsorship(request: Any, policy: Any) -> dict[str, Any]:
    """依据 policy 对已校验的 UserOperation 请求做纯静态 Gas 代付决策。

    只返回字典，不抛业务异常，不修改入参，不访问任何外部系统。
    """
    result = validate(request)
    if result.get("ok") is not True:
        return result
    user_op = result["normalized"]["userOperation"]

    limits, err = _validate_policy(policy)
    if err is not None:
        return err
    assert limits is not None

    total_gas = sum(
        int(user_op[name], 16) for name in _TOTAL_GAS_FIELDS if name in user_op
    )
    estimated_cost_wei = total_gas * int(user_op["maxFeePerGas"], 16)

    if total_gas > limits["maxTotalGas"]:
        approved, reason = False, E_GAS_LIMIT
    elif estimated_cost_wei > limits["budgetWei"]:
        approved, reason = False, E_BUDGET
    else:
        approved, reason = True, OK

    return {
        "ok": True,
        "decision": {
            "approved": approved,
            "reason": reason,
            "totalGas": str(total_gas),
            "estimatedCostWei": str(estimated_cost_wei),
        },
    }
