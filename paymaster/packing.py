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

``plan_bundle_cost_first`` 使用相同的文档结构、校验次序、错误码与结果结构，
但 approved 项不按输入顺序贪心，而是按 ``estimatedCostWei`` 十进制值升序、
``totalGas`` 十进制值升序、原下标升序排列后逐项尝试入选；被拒项不占用额度，
后续较小请求仍可使用剩余额度。``selected`` 与 ``skipped`` 均按原下标升序
输出。

``plan_bundle_max_count`` 同样沿用文档结构、校验次序、错误码与结果结构，但
approved 项不再按顺序贪心，而是在 ``maxTotalGas`` 与 ``maxCostWei`` 两条限额
下求入选数量最大的子集。数量并列时依次取总 ``totalGas`` 较小、总
``estimatedCostWei`` 较小、``selected`` 下标序列字典序较小的唯一方案。方案
确定后，未入选的 approved 请求单独并入该组：先使总 gas 超限记
``E_BUNDLE_GAS``，否则（使总成本超限）记 ``E_BUNDLE_BUDGET``。

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
    """校验文档并返回 (requests, sponsorshipPolicy, maxTotalGas, maxCostWei)。

    校验失败时返回错误字典（``ok`` 为 False）。校验次序：根类型、未知键、
    缺键、requests（逐项）、sponsorshipPolicy、bundlePolicy。
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

    return (
        requests,
        document["sponsorshipPolicy"],
        int(bundle_policy["maxTotalGas"], 16),
        int(bundle_policy["maxCostWei"], 16),
    )


def _evaluate_all(requests: list[Any], sponsorship_policy: Any) -> list[dict[str, Any]]:
    """逐项做单笔代付评估，返回与原下标对齐的 decision 列表。"""
    # 请求与策略均已通过校验，单笔评估必然得到 decision。
    return [
        evaluate_sponsorship(request, sponsorship_policy)["decision"]
        for request in requests
    ]


def _plan_result(
    selected: list[int],
    skipped: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """汇总入选项为 plan 结果；selected 与 skipped 按原下标升序输出。"""
    selected.sort()
    skipped.sort(key=lambda item: item["index"])
    total_gas = sum(int(decisions[index]["totalGas"]) for index in selected)
    total_cost_wei = sum(
        int(decisions[index]["estimatedCostWei"]) for index in selected
    )
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

    decisions = _evaluate_all(requests, sponsorship_policy)

    selected: list[int] = []
    skipped: list[dict[str, Any]] = []
    total_gas = 0
    total_cost_wei = 0
    for index, decision in enumerate(decisions):
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

    return _plan_result(selected, skipped, decisions)


def plan_bundle_cost_first(document: Any) -> dict[str, Any]:
    """成本优先的批量打包规划：approved 项按成本升序逐项尝试入选。

    文档结构、校验次序、错误码与结果结构与 ``plan_bundle`` 相同；区别在于
    approved 请求按 ``estimatedCostWei`` 升序、``totalGas`` 升序、原下标升序
    排列后贪心入选，被拒项不阻断后续较小请求。只返回字典，不抛业务异常。
    """
    prepared = _prepare(document)
    if isinstance(prepared, dict):
        return prepared
    requests, sponsorship_policy, max_total_gas, max_cost_wei = prepared

    decisions = _evaluate_all(requests, sponsorship_policy)

    selected: list[int] = []
    skipped: list[dict[str, Any]] = []
    candidates: list[tuple[int, int, int]] = []
    for index, decision in enumerate(decisions):
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

    return _plan_result(selected, skipped, decisions)


def _choose_max_count(
    candidates: list[tuple[int, int]], max_total_gas: int, max_cost_wei: int
) -> tuple[tuple[int, ...], int, int]:
    """在两条限额下求入选数量最大的子集。

    ``candidates`` 为按原下标递增排列的 ``(gas, cost)`` 列表，返回
    ``(入选候选序号元组, 总gas, 总成本)``。数量并列时依次取总 gas 较小、总
    cost 较小、候选序号字典序较小的唯一方案。
    """
    n = len(candidates)

    # 二维 0/1 背包的 Pareto 前沿 DP：dp[k] 保存恰好选 k 个候选时互不支配的
    # (总gas, 总成本, 候选序号元组) 列表。同 (gas, cost) 只保留字典序最小的
    # 序号元组；后续候选的序号都更大，接入后字典序关系不变，故被支配/字典序
    # 更大的点不可能再反超为最优。
    dp: list[list[tuple[int, int, tuple[int, ...]]]] = [[] for _ in range(n + 1)]
    dp[0] = [(0, 0, ())]
    for candidate_index, (item_gas, item_cost_wei) in enumerate(candidates):
        for k in range(candidate_index, -1, -1):
            for gas, cost, items in dp[k]:
                new_gas = gas + item_gas
                new_cost = cost + item_cost_wei
                if new_gas > max_total_gas or new_cost > max_cost_wei:
                    continue
                new_items = items + (candidate_index,)
                points = dp[k + 1]
                surviving: list[tuple[int, int, tuple[int, ...]]] = []
                dominated = False
                for old_gas, old_cost, old_items in points:
                    if new_gas < old_gas and new_cost <= old_cost:
                        continue  # 新点严格支配旧点（gas 维）
                    if new_gas <= old_gas and new_cost < old_cost:
                        continue  # 新点严格支配旧点（cost 维）
                    if new_gas == old_gas and new_cost == old_cost:
                        if new_items < old_items:
                            continue  # 同限额下序号字典序更小，替换旧点
                        dominated = True
                        surviving.append((old_gas, old_cost, old_items))
                        continue
                    if old_gas <= new_gas and old_cost <= new_cost:
                        dominated = True  # 旧点不劣于新点
                    surviving.append((old_gas, old_cost, old_items))
                if not dominated:
                    surviving.append((new_gas, new_cost, new_items))
                dp[k + 1] = surviving

    # 从最大 k 向下取第一个有可行方案的；同 k 取 (gas, cost) 字典序最小者。
    for k in range(n, -1, -1):
        if dp[k]:
            gas, cost, chosen = min(dp[k], key=lambda point: (point[0], point[1]))
            return chosen, gas, cost
    return (), 0, 0


def plan_bundle_max_count(document: Any) -> dict[str, Any]:
    """以入选数量最大化为目标的批量打包规划。

    文档结构、校验次序、错误码与结果结构与 ``plan_bundle`` 相同；区别在于
    approved 请求在 ``maxTotalGas`` 与 ``maxCostWei`` 两条限额下全局择优：先
    最大化入选数量，数量并列时依次取总 ``totalGas`` 较小、总
    ``estimatedCostWei`` 较小、下标序列字典序较小的唯一方案。只返回字典，不
    抛业务异常。
    """
    prepared = _prepare(document)
    if isinstance(prepared, dict):
        return prepared
    requests, sponsorship_policy, max_total_gas, max_cost_wei = prepared

    decisions = _evaluate_all(requests, sponsorship_policy)

    skipped: list[dict[str, Any]] = []
    # profiles 按原下标递增压入，平行的 indices 记录原下标；候选序号（enumerate
    # 顺序）与原下标顺序同构，故候选序号元组的字典序即 selected 下标序列字典序。
    profiles: list[tuple[int, int]] = []
    indices: list[int] = []
    for index, decision in enumerate(decisions):
        if not decision["approved"]:
            skipped.append({"index": index, "reason": decision["reason"]})
        else:
            profiles.append(
                (
                    int(decision["totalGas"]),
                    int(decision["estimatedCostWei"]),
                )
            )
            indices.append(index)

    chosen, chosen_gas, _ = _choose_max_count(
        profiles, max_total_gas, max_cost_wei
    )

    chosen_set = set(chosen)
    selected = [indices[item] for item in chosen]
    for item, (item_gas, _) in enumerate(profiles):
        if item in chosen_set:
            continue
        if chosen_gas + item_gas > max_total_gas:
            skipped.append({"index": indices[item], "reason": REASON_BUNDLE_GAS})
        else:
            # chosen 已达最大可行数量，该项并入不可能两条限额都不超。
            skipped.append({"index": indices[item], "reason": REASON_BUNDLE_BUDGET})

    return _plan_result(selected, skipped, decisions)
