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

``plan_bundle_max_count`` 使用相同的文档结构、校验次序、错误码与结果结构，
但选择以入选数量最大化为唯一目标：在同时满足
``bundlePolicy.maxTotalGas`` 与 ``bundlePolicy.maxCostWei`` 的所有 approved
子集中取入选数最多者。数量并列时依次取总 ``totalGas`` 较小、总
``estimatedCostWei`` 较小、``selected`` 下标序列字典序较小的唯一方案。
最终方案确定后，未入选的 approved 请求按“加入该组是否先超 gas”判定：
加入后总 gas 超限记 ``E_BUNDLE_GAS``，否则（必超成本）记
``E_BUNDLE_BUDGET``。

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
    candidates: list[tuple[int, int, int]], max_total_gas: int, max_cost_wei: int
) -> list[int]:
    """在双约束下选取入选数量最大的 approved 子集。

    ``candidates`` 每项为 ``(gas, cost, 原下标)``。在同时满足 gas 与成本
    上限的所有子集中取数量最多者；数量并列取总 gas 较小，再并列取总成本
    较小，仍并列取下标序列字典序较小者。

    双约束下的基数选择是双目标 0/1 背包，按入选数分层维护 Pareto 非支配
    前沿：``frontier[count]`` 为 ``(gas, cost, 下标序列)`` 列表，按总 gas
    严格递增、总成本严格递减——gas 更小且 cost 不更大的状态支配其余状态。
    前沿长度只与候选数及权重的不同组合数有关，与上限的数值大小无关
    （上限为 32 字节 quantity 时不能按 gas 数值建表）。
    """
    n = len(candidates)
    frontier: list[list[tuple[int, int, tuple[int, ...]]]] = [
        [(0, 0, ())]
    ]
    for _ in range(n):
        frontier.append([])

    for item_gas, item_cost, index in candidates:
        for count in range(n - 1, -1, -1):
            if not frontier[count]:
                continue
            added = [
                (gas + item_gas, cost + item_cost, indices + (index,))
                for gas, cost, indices in frontier[count]
                if gas + item_gas <= max_total_gas
                and cost + item_cost <= max_cost_wei
            ]
            if not added:
                continue
            base = frontier[count + 1]

            # 两路均按 gas 升序；先归并，相同 gas 只保留 (cost, 下标序列)
            # 最小者。
            merged: list[tuple[int, int, tuple[int, ...]]] = []
            bi = ai = 0
            while bi < len(base) or ai < len(added):
                if ai == len(added) or (
                    bi < len(base) and base[bi][0] < added[ai][0]
                ):
                    candidate = base[bi]
                    bi += 1
                elif bi == len(base) or added[ai][0] < base[bi][0]:
                    candidate = added[ai]
                    ai += 1
                else:
                    candidate = (
                        base[bi]
                        if (base[bi][1], base[bi][2])
                        <= (added[ai][1], added[ai][2])
                        else added[ai]
                    )
                    bi += 1
                    ai += 1

                gas, cost, indices = candidate
                if merged and merged[-1][0] == gas:
                    if (cost, indices) < (merged[-1][1], merged[-1][2]):
                        merged[-1] = candidate
                    continue
                merged.append(candidate)

            # 按 gas 升序扫描，仅保留 cost 严格小于此前最小 cost 的状态；
            # 其余被“gas 更小且 cost 不更大”的状态支配。
            kept: list[tuple[int, int, tuple[int, ...]]] = []
            min_cost: int | None = None
            for state in merged:
                if min_cost is None or state[1] < min_cost:
                    kept.append(state)
                    min_cost = state[1]
            frontier[count + 1] = kept

    for count in range(n, -1, -1):
        # gas 升序即 cost 递减；第一个成本达标的状态 gas 最小，同 gas 同
        # cost 的字典序较小者已在归并时保留，故即平局规则下的唯一最优。
        for gas, cost, indices in frontier[count]:
            if cost <= max_cost_wei:
                return list(indices)
    return []


def plan_bundle_max_count(document: Any) -> dict[str, Any]:
    """以入选数量最大化为目标的批量打包规划。

    文档结构、校验次序、错误码与结果结构与 ``plan_bundle`` 相同；区别在于
    从全部 approved 请求中精确求双约束（``maxTotalGas``、``maxCostWei``）
    下入选数量最大的子集，数量并列时依次取总 gas 较小、总成本较小、下标
    序列字典序较小者。只返回字典，不抛业务异常。
    """
    prepared = _prepare(document)
    if isinstance(prepared, dict):
        return prepared
    requests, sponsorship_policy, max_total_gas, max_cost_wei = prepared

    decisions = _evaluate_all(requests, sponsorship_policy)

    skipped: list[dict[str, Any]] = []
    candidates: list[tuple[int, int, int]] = []
    for index, decision in enumerate(decisions):
        if not decision["approved"]:
            skipped.append({"index": index, "reason": decision["reason"]})
            continue
        candidates.append(
            (
                int(decision["totalGas"]),
                int(decision["estimatedCostWei"]),
                index,
            )
        )
    candidates.sort(key=lambda item: item[2])

    chosen = _choose_max_count(candidates, max_total_gas, max_cost_wei)
    selected_set = set(chosen)
    total_gas = sum(int(decisions[index]["totalGas"]) for index in chosen)

    for item_gas, _item_cost, index in candidates:
        if index in selected_set:
            continue
        if total_gas + item_gas > max_total_gas:
            skipped.append({"index": index, "reason": REASON_BUNDLE_GAS})
        else:
            # 最优方案再加入本项后不可能两项都不超，否则数量还能增加。
            skipped.append({"index": index, "reason": REASON_BUNDLE_BUDGET})

    return _plan_result(chosen, skipped, decisions)
