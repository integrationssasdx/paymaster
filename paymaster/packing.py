"""ERC-4337 v0.7 Gas 代付的批量打包规划。

公开入口 ``plan_bundle(document)``：

- 入参为已解析的 JSON 值（期望是含 ``requests``、``sponsorshipPolicy`` 与
  ``bundlePolicy`` 的对象），函数不修改入参。
- ``requests`` 为现有单笔代付请求（与 ``paymaster.validation.validate`` 的
  入参格式相同）组成的数组，逐项先走静态校验再做 ``sponsorshipPolicy``
  单笔代付决策。
- ``sponsorshipPolicy`` 与单笔代付策略相同：恰好含 ``budgetWei`` 与
  ``maxTotalGas``，均为规范 quantity 且大于 0。
- ``bundlePolicy`` 恰好含 ``maxTotalGas`` 与 ``maxCostWei``，均为规范
  quantity 且大于 0。
- 返回值永远是字典，不抛业务异常：
  - 结构失败：``{"ok": False, "error": {"code", "path", "message"}}``；
  - 其余结果：``{"ok": True, "plan": {...}}``，``plan`` 含 ``selected``、
    ``skipped``、``operationCount``、``totalGas``、``estimatedCostWei``，
    其中三个汇总数值为十进制字符串。

错误码：

- 文档根与 requests 的结构错误沿用既有码（``E_INVALID_JSON``、
  ``E_UNKNOWN_FIELD``、``E_MISSING_FIELD``、``E_INVALID_FIELD``）。
- 逐项请求错误原样保留 ``code``、``message``，``path`` 前加
  ``/requests/<下标>``。
- 策略结构错误使用 ``E_POLICY_INVALID_FIELD``、``E_POLICY_MISSING_FIELD``、
  ``E_POLICY_UNKNOWN_FIELD``，``path`` 直接指向策略字段。
- 打包超限：``E_BUNDLE_GAS``（累计 gas 超过 ``bundlePolicy.maxTotalGas``）、
  ``E_BUNDLE_BUDGET``（累计成本超过 ``bundlePolicy.maxCostWei``）。

结构检查次序为：根类型、未知键、缺键、requests、策略；策略内部为根类型、
缺键、未知键、字段值。入选顺序严格按 ``requests`` 数组顺序，先查 gas 后查
成本。空选择也可成功。

本模块不验签、不模拟执行、不访问节点、数据库或文件。
"""

from __future__ import annotations

from typing import Any

from paymaster.sponsorship import _validate_policy_fields, evaluate_sponsorship
from paymaster.validation import (
    E_INVALID_FIELD,
    E_INVALID_JSON,
    E_MISSING_FIELD,
    E_UNKNOWN_FIELD,
    _pointer,
    validate,
)

E_BUNDLE_GAS = "E_BUNDLE_GAS"
E_BUNDLE_BUDGET = "E_BUNDLE_BUDGET"

# 文档根的键，顺序即缺键检查与策略校验的次序。
_ROOT_FIELDS = ("requests", "sponsorshipPolicy", "bundlePolicy")

# 两个策略的必需键，顺序即缺键与字段值的检查顺序。
_SPONSORSHIP_POLICY_FIELDS = ("budgetWei", "maxTotalGas")
_BUNDLE_POLICY_FIELDS = ("maxTotalGas", "maxCostWei")


def _error(code: str, path: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "path": path, "message": message}}


def plan_bundle(document: Any) -> dict[str, Any]:
    """对批量代付文档做静态校验并按顺序规划打包。

    只返回字典，不抛业务异常。不验签、不模拟执行、不访问外部系统。
    """
    # 1. 根类型
    if not isinstance(document, dict):
        return _error(E_INVALID_JSON, "", "document root must be a JSON object")

    # 2. 未知键
    for key in document:
        if key not in _ROOT_FIELDS:
            return _error(E_UNKNOWN_FIELD, _pointer(key), f"unknown field {key!r}")

    # 3. 缺键
    for key in _ROOT_FIELDS:
        if key not in document:
            return _error(
                E_MISSING_FIELD, _pointer(key), f"missing required field {key!r}"
            )

    # 4. requests：必须为数组，逐项走现有静态校验，任一结构错误即失败。
    requests = document["requests"]
    if not isinstance(requests, list):
        return _error(
            E_INVALID_FIELD, _pointer("requests"), "field 'requests' must be an array"
        )
    for index, request in enumerate(requests):
        result = validate(request)
        if not result["ok"]:
            err = result["error"]
            return {
                "ok": False,
                "error": {
                    "code": err["code"],
                    "path": f"/requests/{index}{err['path']}",
                    "message": err["message"],
                },
            }

    # 5. 策略：先 sponsorshipPolicy 后 bundlePolicy，内部次序为
    #    根类型、缺键、未知键、字段值（由 _validate_policy_fields 保证）。
    sponsorship_fields, err = _validate_policy_fields(
        document["sponsorshipPolicy"],
        _SPONSORSHIP_POLICY_FIELDS,
        root_label="sponsorshipPolicy",
    )
    if err is not None:
        return err
    assert sponsorship_fields is not None

    bundle_fields, err = _validate_policy_fields(
        document["bundlePolicy"], _BUNDLE_POLICY_FIELDS, root_label="bundlePolicy"
    )
    if err is not None:
        return err
    assert bundle_fields is not None

    # 结构全部通过后，逐项套用单笔代付决策（规则、reason 与单笔一致）。
    decisions = []
    for request in requests:
        outcome = evaluate_sponsorship(request, document["sponsorshipPolicy"])
        # 请求与策略均已校验，此处必然成功。
        assert outcome["ok"], outcome
        decisions.append(outcome["decision"])

    # 按 requests 顺序贪心入选：approved 项受 bundle 累计 gas/成本约束。
    bundle_max_gas = int(bundle_fields["maxTotalGas"], 16)
    bundle_max_cost = int(bundle_fields["maxCostWei"], 16)

    selected: list[int] = []
    skipped: list[dict[str, Any]] = []
    total_gas = 0
    total_cost = 0

    for index, decision in enumerate(decisions):
        if not decision["approved"]:
            # 代付未批准：跳过并沿用单笔 reason。
            skipped.append({"index": index, "reason": decision["reason"]})
            continue

        gas = int(decision["totalGas"])
        cost = int(decision["estimatedCostWei"])

        # 先查 gas，超限记 E_BUNDLE_GAS。
        if total_gas + gas > bundle_max_gas:
            skipped.append({"index": index, "reason": E_BUNDLE_GAS})
            continue
        # 后查成本，超限记 E_BUNDLE_BUDGET。
        if total_cost + cost > bundle_max_cost:
            skipped.append({"index": index, "reason": E_BUNDLE_BUDGET})
            continue

        selected.append(index)
        total_gas += gas
        total_cost += cost

    return {
        "ok": True,
        "plan": {
            "selected": selected,
            "skipped": skipped,
            "operationCount": str(len(selected)),
            "totalGas": str(total_gas),
            "estimatedCostWei": str(total_cost),
        },
    }
