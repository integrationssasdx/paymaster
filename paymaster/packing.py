"""ERC-4337 v0.7 批量打包规划。

公开入口 ``plan_bundle(document)`` 与 ``plan_bundle_cost_first(document)``：

- 入参为已解析的 JSON 值，函数不修改入参。``document`` 恰好含三个键：
  - ``requests``：数组，元素为 ``paymaster.validation.validate`` 接受的请求；
  - ``sponsorshipPolicy``：代付策略，规则与 ``paymaster.sponsorship`` 的单笔
    策略相同（``budgetWei``、``maxTotalGas``）；
  - ``bundlePolicy``：打包策略，恰好含 ``maxTotalGas`` 与 ``maxCostWei``，
    均为规范 quantity 且大于 0。
- 返回值永远是字典，不抛业务异常：
  - 结构性失败：``{"ok": False, "error": {"code", "path", "message"}}``；
  - 其他结果：``{"ok": True, "plan": {...}}``，``plan`` 含 ``selected``、
    ``skipped``、``operationCount``、``totalGas``、``estimatedCostWei``。

校验次序：根类型、未知键、缺键、requests（逐项）、策略（sponsorshipPolicy
先于 bundlePolicy）。策略内部次序：根类型、缺键、未知键、字段值。

- 请求错误沿用 ``validate`` 的 code 与 message，path 前加 ``/requests/<下标>``。
- 策略错误码：``E_POLICY_INVALID_FIELD``（根非对象或值非法）、
  ``E_POLICY_MISSING_FIELD``（缺键）、``E_POLICY_UNKNOWN_FIELD``（未知键），
  path 指向 ``/sponsorshipPolicy`` 或 ``/bundlePolicy`` 下的字段。

打包决策按 requests 顺序逐项进行：单笔代付 ``approved`` 为 false 的请求跳过
并沿用其 reason；approved 项在累计 ``totalGas`` 与 ``estimatedCostWei`` 分别
不超过 ``bundlePolicy.maxTotalGas`` 与 ``bundlePolicy.maxCostWei`` 时入选，
否则跳过——gas 先查，超限记 ``E_BUNDLE_GAS``；成本后查，超限记
``E_BUNDLE_BUDGET``。空选择也是成功结果。

``plan_bundle_cost_first`` 使用相同的文档、校验与限额规则，但 approved 项按
``estimatedCostWei`` 升序、``totalGas`` 升序、原下标升序排列后逐项尝试入选，
被拒后继续处理后续较小请求；``selected`` 与 ``skipped`` 均按原下标升序返回。

本模块不验签、不模拟执行、不访问节点、数据库或文件。
"""

from __future__ import annotations

from typing import Any

from paymaster.sponsorship import (
    E_POLICY_INVALID_FIELD,
    E_POLICY_MISSING_FIELD,
    E_POLICY_UNKNOWN_FIELD,
    _POLICY_FIELDS,
    evaluate_sponsorship,
)
from paymaster.validation import (
    E_INVALID_FIELD,
    E_INVALID_JSON,
    E_MISSING_FIELD,
    E_UNKNOWN_FIELD,
    _MAX_QUANTITY_HEX_DIGITS,
    _QUANTITY_RE,
    _pointer,
    validate,
)

REASON_BUNDLE_GAS = "E_BUNDLE_GAS"
REASON_BUNDLE_BUDGET = "E_BUNDLE_BUDGET"

# 根对象的必需键，顺序即缺键检查顺序。
_ROOT_FIELDS = ("requests", "sponsorshipPolicy", "bundlePolicy")

# bundlePolicy 的必需键，顺序即检查顺序（缺键与字段值均按此顺序报错）。
_BUNDLE_POLICY_FIELDS = ("maxTotalGas", "maxCostWei")


def _error(code: str, path: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "path": path, "message": message}}


def _validate_policy(
    policy: Any, fields: tuple[str, ...], segment: str
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """校验策略对象，返回 (规范化字段字典, None) 或 (None, 错误字典)。

    检查顺序：根类型、必需键、未知键、字段值（按 fields 顺序）。错误 path
    以 ``/<segment>`` 为前缀。
    """
    prefix = _pointer(segment)
    if not isinstance(policy, dict):
        return None, _error(
            E_POLICY_INVALID_FIELD, prefix, "policy must be an object"
        )

    for name in fields:
        if name not in policy:
            return None, _error(
                E_POLICY_MISSING_FIELD,
                prefix + _pointer(name),
                f"missing required field {name!r}",
            )

    for key in policy:
        if key not in fields:
            return None, _error(
                E_POLICY_UNKNOWN_FIELD,
                prefix + _pointer(key),
                f"unknown field {key!r}",
            )

    normalized: dict[str, str] = {}
    for name in fields:
        value = policy[name]
        if (
            not isinstance(value, str)
            or not _QUANTITY_RE.match(value)
            or len(value) - 2 > _MAX_QUANTITY_HEX_DIGITS
        ):
            return None, _error(
                E_POLICY_INVALID_FIELD,
                prefix + _pointer(name),
                f"invalid quantity value for field {name!r}",
            )
        if int(value, 16) == 0:
            return None, _error(
                E_POLICY_INVALID_FIELD,
                prefix + _pointer(name),
                f"field {name!r} must be greater than 0",
            )
        normalized[name] = value.lower()
    return normalized, None


def _prepare(
    document: Any,
) -> tuple[list[Any], Any, int, int] | dict[str, Any]:
    """校验文档并提取打包所需数据；失败时返回错误字典。

    成功时返回 ``(requests, sponsorship_policy, max_total_gas, max_cost_wei)``，
    其中 ``sponsorship_policy`` 为文档中的原始策略对象。
    """
    if not isinstance(document, dict):
        return _error(E_INVALID_JSON, "", "document root must be a JSON object")

    for key in document:
        if key not in _ROOT_FIELDS:
            return _error(E_UNKNOWN_FIELD, _pointer(key), f"unknown field {key!r}")
    for key in _ROOT_FIELDS:
        if key not in document:
            return _error(
                E_MISSING_FIELD, _pointer(key), f"missing required field {key!r}"
            )

    requests = document["requests"]
    if not isinstance(requests, list):
        return _error(
            E_INVALID_FIELD, _pointer("requests"), "field 'requests' must be an array"
        )

    for index, request in enumerate(requests):
        result = validate(request)
        if not result["ok"]:
            error = result["error"]
            return _error(
                error["code"],
                _pointer("requests", str(index)) + error["path"],
                error["message"],
            )

    sponsorship_policy, err = _validate_policy(
        document["sponsorshipPolicy"], _POLICY_FIELDS, "sponsorshipPolicy"
    )
    if err is not None:
        return err

    bundle_policy, err = _validate_policy(
        document["bundlePolicy"], _BUNDLE_POLICY_FIELDS, "bundlePolicy"
    )
    if err is not None:
        return err
    assert sponsorship_policy is not None and bundle_policy is not None

    max_total_gas = int(bundle_policy["maxTotalGas"], 16)
    max_cost_wei = int(bundle_policy["maxCostWei"], 16)
    return requests, document["sponsorshipPolicy"], max_total_gas, max_cost_wei


def _plan_result(
    selected: list[int], skipped: list[dict[str, Any]], total_gas: int, total_cost_wei: int
) -> dict[str, Any]:
    return {
        "ok": True,
        "plan": {
            "selected": selected,
            "skipped": skipped,
            "operationCount": str(len(selected)),
            "totalGas": str(total_gas),
            "estimatedCostWei": str(total_cost_wei),
        },
    }


def plan_bundle(document: Any) -> dict[str, Any]:
    """规划批量打包：逐项校验代付，再按 bundlePolicy 限额入选。

    只返回字典，不抛业务异常。不验签、不模拟执行、不访问外部系统。
    """
    prepared = _prepare(document)
    if isinstance(prepared, dict):
        return prepared
    requests, sponsorship_policy, max_total_gas, max_cost_wei = prepared

    selected: list[int] = []
    skipped: list[dict[str, Any]] = []
    total_gas = 0
    total_cost_wei = 0
    for index, request in enumerate(requests):
        # 请求与策略均已通过校验，单笔评估必然得到 decision。
        decision = evaluate_sponsorship(request, sponsorship_policy)["decision"]
        if not decision["approved"]:
            skipped.append({"index": index, "reason": decision["reason"]})
            continue
        item_gas = int(decision["totalGas"])
        item_cost_wei = int(decision["estimatedCostWei"])
        if total_gas + item_gas > max_total_gas:
            skipped.append({"index": index, "reason": REASON_BUNDLE_GAS})
            continue
        if total_cost_wei + item_cost_wei > max_cost_wei:
            skipped.append({"index": index, "reason": REASON_BUNDLE_BUDGET})
            continue
        selected.append(index)
        total_gas += item_gas
        total_cost_wei += item_cost_wei

    return _plan_result(selected, skipped, total_gas, total_cost_wei)


def plan_bundle_cost_first(document: Any) -> dict[str, Any]:
    """成本优先的批量打包规划：按成本升序尝试入选，而非按输入顺序。

    文档要求、校验次序与错误码同 ``plan_bundle``。approved 请求按
    ``estimatedCostWei`` 升序、``totalGas`` 升序、原下标升序排列后逐项尝试
    加入：累计 ``totalGas`` 超限记 ``E_BUNDLE_GAS``，否则累计
    ``estimatedCostWei`` 超限记 ``E_BUNDLE_BUDGET``，均不入选；被拒后继续
    处理后续较小请求。``selected`` 与 ``skipped`` 均按原下标升序返回，
    汇总字段只统计 ``selected``。只返回字典，不抛业务异常。
    """
    prepared = _prepare(document)
    if isinstance(prepared, dict):
        return prepared
    requests, sponsorship_policy, max_total_gas, max_cost_wei = prepared

    skipped: list[dict[str, Any]] = []
    candidates: list[tuple[int, int, int]] = []
    for index, request in enumerate(requests):
        # 请求与策略均已通过校验，单笔评估必然得到 decision。
        decision = evaluate_sponsorship(request, sponsorship_policy)["decision"]
        if not decision["approved"]:
            skipped.append({"index": index, "reason": decision["reason"]})
            continue
        candidates.append(
            (
                int(decision["estimatedCostWei"]),
                int(decision["totalGas"]),
                index,
            )
        )

    candidates.sort()

    selected: list[int] = []
    total_gas = 0
    total_cost_wei = 0
    for item_cost_wei, item_gas, index in candidates:
        if total_gas + item_gas > max_total_gas:
            skipped.append({"index": index, "reason": REASON_BUNDLE_GAS})
            continue
        if total_cost_wei + item_cost_wei > max_cost_wei:
            skipped.append({"index": index, "reason": REASON_BUNDLE_BUDGET})
            continue
        selected.append(index)
        total_gas += item_gas
        total_cost_wei += item_cost_wei

    selected.sort()
    skipped.sort(key=lambda item: item["index"])
    return _plan_result(selected, skipped, total_gas, total_cost_wei)
