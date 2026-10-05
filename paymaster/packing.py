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

``plan_bundle_sender_fair`` 的文档恰好含四个键：``requests``、
``sponsorshipPolicy``、``bundlePolicy`` 与 ``fairnessPolicy``。
``fairnessPolicy`` 恰好含 ``maxPerSender``（规范 quantity 且大于 0），限制
同一 sender 的入选数量。校验次序、错误码与结果结构沿用上述约定，三个策略
按 sponsorshipPolicy、bundlePolicy、fairnessPolicy 的顺序校验。approved 项
在 sender 配额与两条 bundle 限额下求最优子集：先最大化入选数量，再最大化
不同 sender 数，之后依次取总 ``totalGas`` 较小、总 ``estimatedCostWei`` 较
小、``selected`` 下标序列字典序较小的唯一方案。未入选的 approved 请求只记
一个原因：其 sender 入选数已达 ``maxPerSender`` 记 ``E_SENDER_QUOTA``；否则
并入后先超 gas 记 ``E_BUNDLE_GAS``，不先超 gas 但超成本记
``E_BUNDLE_BUDGET``。

``plan_bundle_nonce_unique`` 的文档结构、校验次序、错误码与结果结构沿用
``plan_bundle``（恰好含 ``requests``、``sponsorshipPolicy``、
``bundlePolicy`` 三个键）。approved 项按规范化结果判冲突：sender 取规范小写
地址，nonce 取规范 quantity；同一 sender 的同一 nonce 至多入选一项，其他组合
不受限制。在该约束与 ``maxTotalGas``、``maxCostWei`` 两条限额下求最优子集：
先最大化入选数量，之后依次取总 ``totalGas`` 较小、总
``estimatedCostWei`` 较小、``selected`` 下标序列字典序较小的唯一方案。未入选
的 approved 请求只记一个原因：最终组已有同 sender 同 nonce 记
``E_NONCE_CONFLICT``；否则单独并入后先超 gas 记 ``E_BUNDLE_GAS``，不先超
gas 但超成本记 ``E_BUNDLE_BUDGET``。

``plan_bundle_nonce_chain`` 的文档结构、校验次序、错误码与结果结构同样沿用
``plan_bundle``。approved 项按规范 sender 与 nonce 链分组：nonce 整数值除以
2 的 64 次方，商为 key、余数为 sequence；同一 (sender, key) 组的入选项按
requests 下标递增检查时，sequence 必须严格递增且相邻恰好相差 1（同一
sequence 至多一项，且下标顺序与 sequence 顺序一致）。在该链约束与
``maxTotalGas``、``maxCostWei`` 两条限额下求最优子集：先最大化入选数量，之
后依次取总 ``totalGas`` 较小、总 ``estimatedCostWei`` 较小、``selected``
下标序列字典序较小的唯一方案。未入选的 approved 请求只记一个原因：最终组已
有同 sender、key、sequence 记 ``E_NONCE_CONFLICT``；并入后破坏连续链（下标
递增时 sequence 不再严格递增且相邻相差 1）记 ``E_NONCE_GAP``；否则单独并入
后先超 gas 记 ``E_BUNDLE_GAS``，不先超 gas 但超成本记 ``E_BUNDLE_BUDGET``。

``plan_bundle_sender_nonce_chain`` 的文档恰好含四个键：``requests``、
``sponsorshipPolicy``、``bundlePolicy`` 与 ``fairnessPolicy``（校验次序、
错误码、path 与结果结构同 ``plan_bundle_sender_fair``）。approved 项在
``plan_bundle_nonce_chain`` 的链约束之上再叠加 sender 配额：同一规范 sender
的入选数量不超过 ``fairnessPolicy.maxPerSender``，不同 (sender, key) 组各自
成链且共享该 sender 的配额。目标依次：入选数量最大、不同 sender 数最大、总
``totalGas`` 较小、总 ``estimatedCostWei`` 较小、``selected`` 下标序列字典
序较小。未入选的 approved 请求只记一个原因，按序判定：最终组已有同 sender、
key、sequence 记 ``E_NONCE_CONFLICT``；单独并入破坏连续链记
``E_NONCE_GAP``；否则其 sender 入选数已达配额记 ``E_SENDER_QUOTA``；否则并
入后先超 gas 记 ``E_BUNDLE_GAS``，不先超 gas 但超成本记
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
REASON_SENDER_QUOTA = "E_SENDER_QUOTA"
REASON_NONCE_CONFLICT = "E_NONCE_CONFLICT"
REASON_NONCE_GAP = "E_NONCE_GAP"

# nonce 链分组：nonce 整数值除以 2 的 64 次方，商为 key、余数为 sequence。
_NONCE_SEQUENCE_MOD = 1 << 64

# 根对象的必需键，顺序即缺键检查顺序。
_ROOT_FIELDS = ("requests", "sponsorshipPolicy", "bundlePolicy")

# bundlePolicy 的必需键，顺序即检查顺序（缺键与字段值均按此顺序报错）。
_BUNDLE_POLICY_FIELDS = ("maxTotalGas", "maxCostWei")

# sender 公平文档的根对象必需键，顺序即缺键检查顺序。
_SENDER_FAIR_ROOT_FIELDS = (
    "requests",
    "sponsorshipPolicy",
    "bundlePolicy",
    "fairnessPolicy",
)

# fairnessPolicy 的必需键（只有一个）。
_FAIRNESS_POLICY_FIELDS = ("maxPerSender",)


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


def _prepare_sender_fair(
    document: Any,
) -> tuple[list[Any], Any, int, int, int] | dict[str, Any]:
    """校验 sender 公平文档并返回 (requests, sponsorshipPolicy, maxTotalGas,
    maxCostWei, maxPerSender)。

    校验失败时返回错误字典（``ok`` 为 False）。校验次序：根类型、未知键、
    缺键、requests（逐项）、sponsorshipPolicy、bundlePolicy、fairnessPolicy。
    """
    if not isinstance(document, dict):
        return _error(E_INVALID_JSON, "", "document root must be a JSON object")

    for key in document:
        if key not in _SENDER_FAIR_ROOT_FIELDS:
            return _error(E_UNKNOWN_FIELD, _pointer(key), f"unknown field {key!r}")
    for key in _SENDER_FAIR_ROOT_FIELDS:
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

    fairness_policy, err = _validate_policy(
        document["fairnessPolicy"], _FAIRNESS_POLICY_FIELDS, "fairnessPolicy"
    )
    if err is not None:
        return err
    assert sponsorship_policy is not None and bundle_policy is not None
    assert fairness_policy is not None

    return (
        requests,
        document["sponsorshipPolicy"],
        int(bundle_policy["maxTotalGas"], 16),
        int(bundle_policy["maxCostWei"], 16),
        int(fairness_policy["maxPerSender"], 16),
    )


def _prune_frontier(
    points: list[tuple[int, int, tuple[int, ...]]],
) -> list[tuple[int, int, tuple[int, ...]]]:
    """裁剪 (gas, cost, 序号元组) 列表为 Pareto 前沿。

    返回列表按 gas 升序、cost 严格递减；被支配点（另有一点 gas 与 cost 都不更
    大）删除，同 (gas, cost) 只保留序号元组字典序最小者。
    """
    points.sort()
    frontier: list[tuple[int, int, tuple[int, ...]]] = []
    min_cost: int | None = None
    for gas, cost, items in points:
        if min_cost is not None and cost >= min_cost:
            continue
        frontier.append((gas, cost, items))
        min_cost = cost
    return frontier


def _sender_frontiers(
    items: list[tuple[int, int, int]],
    limit: int,
    max_total_gas: int,
    max_cost_wei: int,
) -> list[list[tuple[int, int, tuple[int, ...]]]]:
    """单 sender 的子集前沿：``dp[k]`` 为恰好选 k 项时的 Pareto 前沿。

    ``items`` 为按候选序号递增的 ``(候选序号, gas, cost)`` 列表，``limit``
    为该 sender 最多入选项数。已超两条限额的中间点不可能进入可行方案，直接
    丢弃。候选序号递增处理，故同 (gas, cost) 时字典序更大的点不可能再反超
    （后续并入的序号都更大，字典序关系不变）。
    """
    dp: list[list[tuple[int, int, tuple[int, ...]]]] = [
        [] for _ in range(limit + 1)
    ]
    dp[0] = [(0, 0, ())]
    for position, item_gas, item_cost in items:
        for k in range(limit - 1, -1, -1):
            additions = []
            for gas, cost, chosen in dp[k]:
                new_gas = gas + item_gas
                new_cost = cost + item_cost
                if new_gas > max_total_gas or new_cost > max_cost_wei:
                    continue
                additions.append((new_gas, new_cost, chosen + (position,)))
            if additions:
                dp[k + 1] = _prune_frontier(dp[k + 1] + additions)
    return dp


def _choose_sender_fair(
    candidates: list[tuple[Any, int, int]],
    max_per_sender: int,
    max_total_gas: int,
    max_cost_wei: int,
) -> tuple[tuple[int, ...], int, int]:
    """在 sender 配额与两条限额下求最优入选子集。

    ``candidates`` 为按原下标递增的 ``(sender, gas, cost)`` 列表。目标依次：
    入选数量最大、不同 sender 数最大、总 gas 较小、总 cost 较小、候选序号序
    列字典序较小。返回 ``(入选候选序号元组, 总gas, 总成本)``。
    """
    # 按 sender 分组，组内保持候选序号递增；sender 顺序按首次出现。
    groups: dict[Any, list[tuple[int, int, int]]] = {}
    for position, (sender, item_gas, item_cost) in enumerate(candidates):
        groups.setdefault(sender, []).append((position, item_gas, item_cost))

    # 状态 (入选数, 不同 sender 数) -> (gas, cost, 候选序号元组) 的 Pareto
    # 前沿。同一状态内的被支配点不可能反超：后续并入的 sender 选项对两点相
    # 同，gas 与 cost 都不更差者恒不更差；同 (gas, cost) 时序号元组字典序在
    # 有序归并下保持（归并保持字典序）。
    states: dict[tuple[int, int], list[tuple[int, int, tuple[int, ...]]]] = {
        (0, 0): [(0, 0, ())]
    }
    for items in groups.values():
        frontiers = _sender_frontiers(
            items, min(max_per_sender, len(items)), max_total_gas, max_cost_wei
        )
        merged_states: dict[
            tuple[int, int], list[tuple[int, int, tuple[int, ...]]]
        ] = {}
        for (count, sender_count), points in states.items():
            for taken, frontier in enumerate(frontiers):
                if not frontier:
                    continue
                key = (count + taken, sender_count + (1 if taken > 0 else 0))
                additions = merged_states.setdefault(key, [])
                for gas, cost, chosen in points:
                    for add_gas, add_cost, add_chosen in frontier:
                        new_gas = gas + add_gas
                        new_cost = cost + add_cost
                        if new_gas > max_total_gas or new_cost > max_cost_wei:
                            continue
                        additions.append(
                            (new_gas, new_cost, tuple(sorted(chosen + add_chosen)))
                        )
        states = {
            key: _prune_frontier(points)
            for key, points in merged_states.items()
        }

    # 前沿按 (gas, cost, 序号元组) 升序，首元素即该状态的最优点。
    best_key: tuple[Any, ...] | None = None
    best: tuple[tuple[int, ...], int, int] = ((), 0, 0)
    for (count, sender_count), frontier in states.items():
        if not frontier:
            continue
        gas, cost, chosen = frontier[0]
        key = (-count, -sender_count, gas, cost, chosen)
        if best_key is None or key < best_key:
            best_key = key
            best = (chosen, gas, cost)
    return best


def plan_bundle_sender_fair(document: Any) -> dict[str, Any]:
    """按 sender 公平约束的批量打包规划。

    文档恰好含 ``requests``、``sponsorshipPolicy``、``bundlePolicy`` 与
    ``fairnessPolicy`` 四个键；``fairnessPolicy`` 恰好含 ``maxPerSender``
    （规范 quantity 且大于 0），限制同一 sender 的入选数量。approved 请求在
    sender 配额与 bundlePolicy 两条限额下求最优子集：先最大化入选数量，再最
    大化不同 sender 数，之后依次取总 ``totalGas`` 较小、总
    ``estimatedCostWei`` 较小、``selected`` 下标序列字典序较小的唯一方案。
    未入选的 approved 请求只记一个原因：其 sender 入选数已达配额记
    ``E_SENDER_QUOTA``；否则并入后先超 gas 记 ``E_BUNDLE_GAS``，不先超 gas
    但超成本记 ``E_BUNDLE_BUDGET``。只返回字典，不抛业务异常。
    """
    prepared = _prepare_sender_fair(document)
    if isinstance(prepared, dict):
        return prepared
    requests, sponsorship_policy, max_total_gas, max_cost_wei, max_per_sender = (
        prepared
    )

    decisions = _evaluate_all(requests, sponsorship_policy)

    skipped: list[dict[str, Any]] = []
    # candidates 按原下标递增压入，平行的 indices 记录原下标；sender 取校验后
    # 的规范地址（小写），同一地址大小写不同视为同一 sender。
    candidates: list[tuple[Any, int, int]] = []
    indices: list[int] = []
    for index, (request, decision) in enumerate(zip(requests, decisions)):
        if not decision["approved"]:
            skipped.append({"index": index, "reason": decision["reason"]})
            continue
        # 请求已通过校验，此处取规范 sender 必然成功。
        sender = validate(request)["normalized"]["userOperation"]["sender"]
        candidates.append(
            (
                sender,
                int(decision["totalGas"]),
                int(decision["estimatedCostWei"]),
            )
        )
        indices.append(index)

    chosen, chosen_gas, _ = _choose_sender_fair(
        candidates, max_per_sender, max_total_gas, max_cost_wei
    )

    chosen_set = set(chosen)
    selected = [indices[item] for item in chosen]
    sender_counts: dict[Any, int] = {}
    for item in chosen:
        sender = candidates[item][0]
        sender_counts[sender] = sender_counts.get(sender, 0) + 1

    for item, (sender, item_gas, _) in enumerate(candidates):
        if item in chosen_set:
            continue
        index = indices[item]
        if sender_counts.get(sender, 0) >= max_per_sender:
            skipped.append({"index": index, "reason": REASON_SENDER_QUOTA})
        elif chosen_gas + item_gas > max_total_gas:
            skipped.append({"index": index, "reason": REASON_BUNDLE_GAS})
        else:
            # chosen 已达最大可行数量，该项 sender 未满且 gas 不超，并入必然
            # 使总成本超限。
            skipped.append({"index": index, "reason": REASON_BUNDLE_BUDGET})

    return _plan_result(selected, skipped, decisions)


def _nonce_key_frontiers(
    items: list[tuple[int, int, int]],
    max_total_gas: int,
    max_cost_wei: int,
) -> list[list[tuple[int, int, tuple[int, ...]]]]:
    """单个 (sender, nonce) 键的子集前沿：``dp[k]`` 为恰好选 k 项时的前沿。

    同一键至多入选一项，故只使用 k=0 与 k=1；返回完整列表以与 sender 公平的
    组合循环同构。``items`` 为按候选序号递增的 ``(候选序号, gas, cost)``
    列表。已超两条限额的点直接丢弃。
    """
    dp: list[list[tuple[int, int, tuple[int, ...]]]] = [[(0, 0, ())], []]
    for position, item_gas, item_cost in items:
        if item_gas > max_total_gas or item_cost > max_cost_wei:
            continue
        # 同键只保留一个：单项之间按 (gas, cost, 候选序号) 取最优前沿。
        dp[1] = _prune_frontier(dp[1] + [(item_gas, item_cost, (position,))])
    return dp


def _choose_nonce_unique(
    candidates: list[tuple[Any, Any, int, int]],
    max_total_gas: int,
    max_cost_wei: int,
) -> tuple[tuple[int, ...], int, int]:
    """在 nonce 唯一约束与两条限额下求最优入选子集。

    ``candidates`` 为按原下标递增的 ``(sender, nonce, gas, cost)`` 列表。
    目标依次：入选数量最大、总 gas 较小、总 cost 较小、候选序号序列字典序较
    小。返回 ``(入选候选序号元组, 总gas, 总成本)``。
    """
    # 按 (sender, nonce) 分组，组内保持候选序号递增；组顺序按首次出现。
    groups: dict[tuple[Any, Any], list[tuple[int, int, int]]] = {}
    for position, (sender, nonce, item_gas, item_cost) in enumerate(candidates):
        groups.setdefault((sender, nonce), []).append(
            (position, item_gas, item_cost)
        )

    # 状态 入选数 -> (gas, cost, 候选序号元组) 的 Pareto 前沿。每组至多取一项，
    # 故状态只按入选数索引。被支配点不可能反超：后续组的可选项对两点相同，
    # gas 与 cost 都不更差者恒不更差；同 (gas, cost) 时序号元组字典序在有序归
    # 并下保持（归并保持字典序）。
    states: dict[int, list[tuple[int, int, tuple[int, ...]]]] = {0: [(0, 0, ())]}
    for items in groups.values():
        frontiers = _nonce_key_frontiers(items, max_total_gas, max_cost_wei)
        merged_states: dict[int, list[tuple[int, int, tuple[int, ...]]]] = {}
        for count, points in states.items():
            for taken, frontier in enumerate(frontiers):
                if not frontier:
                    continue
                additions = merged_states.setdefault(count + taken, [])
                for gas, cost, chosen in points:
                    for add_gas, add_cost, add_chosen in frontier:
                        new_gas = gas + add_gas
                        new_cost = cost + add_cost
                        if new_gas > max_total_gas or new_cost > max_cost_wei:
                            continue
                        additions.append(
                            (new_gas, new_cost, tuple(sorted(chosen + add_chosen)))
                        )
        states = {
            count: _prune_frontier(points)
            for count, points in merged_states.items()
        }

    # 前沿按 (gas, cost, 序号元组) 升序，首元素即该入选数的最优方案。
    best: tuple[tuple[int, ...], int, int] = ((), 0, 0)
    best_key: tuple[Any, ...] | None = None
    for count, frontier in states.items():
        if not frontier:
            continue
        gas, cost, chosen = frontier[0]
        key = (-count, gas, cost, chosen)
        if best_key is None or key < best_key:
            best_key = key
            best = (chosen, gas, cost)
    return best


def plan_bundle_nonce_unique(document: Any) -> dict[str, Any]:
    """避免同账户 nonce 冲突的批量打包规划。

    文档结构、校验次序、错误码与结果结构与 ``plan_bundle`` 相同；区别在于
    approved 请求按规范化的 sender（小写地址）与 nonce（规范 quantity）判冲
    突：同一 sender 的同一 nonce 至多入选一项。在该约束与两条 bundle 限额下
    全局择优：先最大化入选数量，数量并列时依次取总 ``totalGas`` 较小、总
    ``estimatedCostWei`` 较小、``selected`` 下标序列字典序较小的唯一方案。
    未入选的 approved 请求只记一个原因：最终组已有同 sender 同 nonce 记
    ``E_NONCE_CONFLICT``；否则单独并入后先超 gas 记 ``E_BUNDLE_GAS``，不先
    超 gas 但超成本记 ``E_BUNDLE_BUDGET``。只返回字典，不抛业务异常。
    """
    prepared = _prepare(document)
    if isinstance(prepared, dict):
        return prepared
    requests, sponsorship_policy, max_total_gas, max_cost_wei = prepared

    decisions = _evaluate_all(requests, sponsorship_policy)

    skipped: list[dict[str, Any]] = []
    # candidates 按原下标递增压入，平行的 indices 记录原下标；sender 取校验后
    # 的规范地址（小写），nonce 取规范 quantity，故大小写或前导零差异不产生
    # 新键。
    candidates: list[tuple[Any, Any, int, int]] = []
    indices: list[int] = []
    for index, (request, decision) in enumerate(zip(requests, decisions)):
        if not decision["approved"]:
            skipped.append({"index": index, "reason": decision["reason"]})
            continue
        # 请求已通过校验，此处取规范 sender/nonce 必然成功。
        user_op = validate(request)["normalized"]["userOperation"]
        candidates.append(
            (
                user_op["sender"],
                int(user_op["nonce"], 16),
                int(decision["totalGas"]),
                int(decision["estimatedCostWei"]),
            )
        )
        indices.append(index)

    chosen, chosen_gas, _ = _choose_nonce_unique(
        candidates, max_total_gas, max_cost_wei
    )

    chosen_set = set(chosen)
    selected = [indices[item] for item in chosen]
    chosen_keys: set[tuple[Any, Any]] = {
        (candidates[item][0], candidates[item][1]) for item in chosen
    }

    for item, (sender, nonce, item_gas, _) in enumerate(candidates):
        if item in chosen_set:
            continue
        index = indices[item]
        if (sender, nonce) in chosen_keys:
            skipped.append({"index": index, "reason": REASON_NONCE_CONFLICT})
        elif chosen_gas + item_gas > max_total_gas:
            skipped.append({"index": index, "reason": REASON_BUNDLE_GAS})
        else:
            # chosen 已达约束下的最大可行数量：该项不冲突且 gas 不超，并入必
            # 然使总成本超限。
            skipped.append({"index": index, "reason": REASON_BUNDLE_BUDGET})

    return _plan_result(selected, skipped, decisions)


def _chain_group_frontiers(
    items: list[tuple[int, int, int, int]],
    max_total_gas: int,
    max_cost_wei: int,
) -> list[list[tuple[int, int, tuple[int, ...]]]]:
    """单个 (sender, key) 组的子集前沿：``dp[k]`` 为恰好选 k 项时的前沿。

    ``items`` 为按候选序号递增的 ``(候选序号, sequence, gas, cost)`` 列表。
    可行子集为一条链：按候选序号递增取出时 sequence 严格递增且相邻恰好相差
    1。``ending[j][k]`` 保存以 ``items[j]`` 结尾、长度为 k 的链的 Pareto 前
    沿；链只能从 sequence 恰好小 1 且候选序号更小的项延伸。已超两条限额的中
    间点不可能进入可行方案，直接丢弃。同一 (结尾, 长度) 内被支配的点不可能
    反超：后续可延伸的项对两点相同，gas 与 cost 都不更差者恒不更差；同
    (gas, cost) 时序号元组字典序较小者胜（后续并入的序号都更大，字典序关系
    不变）。
    """
    size = len(items)
    ending: list[list[list[tuple[int, int, tuple[int, ...]]]]] = [
        [[] for _ in range(size + 1)] for _ in range(size)
    ]
    dp: list[list[tuple[int, int, tuple[int, ...]]]] = [
        [] for _ in range(size + 1)
    ]
    dp[0] = [(0, 0, ())]
    for j, (position, sequence, item_gas, item_cost) in enumerate(items):
        if item_gas > max_total_gas or item_cost > max_cost_wei:
            continue  # 单项已超限额，任何可行方案都不会含它
        additions: list[list[tuple[int, int, tuple[int, ...]]]] = [
            [] for _ in range(size + 1)
        ]
        additions[1].append((item_gas, item_cost, (position,)))
        # 候选序号递增遍历，i < j 即候选序号更小；只接 sequence 恰好小 1 的链。
        for i in range(j):
            if items[i][1] != sequence - 1:
                continue
            for k in range(1, size):
                for gas, cost, chosen in ending[i][k]:
                    new_gas = gas + item_gas
                    new_cost = cost + item_cost
                    if new_gas > max_total_gas or new_cost > max_cost_wei:
                        continue
                    additions[k + 1].append(
                        (new_gas, new_cost, chosen + (position,))
                    )
        for k in range(1, size + 1):
            if not additions[k]:
                continue
            ending[j][k] = _prune_frontier(additions[k])
            dp[k] = _prune_frontier(dp[k] + ending[j][k])
    return dp


def _choose_nonce_chain(
    candidates: list[tuple[Any, int, int, int, int]],
    max_total_gas: int,
    max_cost_wei: int,
) -> tuple[tuple[int, ...], int, int]:
    """在 nonce 链约束与两条限额下求最优入选子集。

    ``candidates`` 为按原下标递增的 ``(sender, key, sequence, gas, cost)``
    列表。目标依次：入选数量最大、总 gas 较小、总 cost 较小、候选序号序列字
    典序较小。返回 ``(入选候选序号元组, 总gas, 总成本)``。
    """
    # 按 (sender, key) 分组，组内保持候选序号递增；组顺序按首次出现。
    groups: dict[tuple[Any, int], list[tuple[int, int, int, int]]] = {}
    for position, (sender, key, sequence, item_gas, item_cost) in enumerate(
        candidates
    ):
        groups.setdefault((sender, key), []).append(
            (position, sequence, item_gas, item_cost)
        )

    # 状态 入选数 -> (gas, cost, 候选序号元组) 的 Pareto 前沿，与
    # _choose_nonce_unique 的跨组归并同构：被支配点不可能反超，同 (gas, cost)
    # 时序号元组字典序在有序归并下保持。
    states: dict[int, list[tuple[int, int, tuple[int, ...]]]] = {0: [(0, 0, ())]}
    for items in groups.values():
        frontiers = _chain_group_frontiers(items, max_total_gas, max_cost_wei)
        merged_states: dict[int, list[tuple[int, int, tuple[int, ...]]]] = {}
        for count, points in states.items():
            for taken, frontier in enumerate(frontiers):
                if not frontier:
                    continue
                additions = merged_states.setdefault(count + taken, [])
                for gas, cost, chosen in points:
                    for add_gas, add_cost, add_chosen in frontier:
                        new_gas = gas + add_gas
                        new_cost = cost + add_cost
                        if new_gas > max_total_gas or new_cost > max_cost_wei:
                            continue
                        additions.append(
                            (new_gas, new_cost, tuple(sorted(chosen + add_chosen)))
                        )
        states = {
            count: _prune_frontier(points)
            for count, points in merged_states.items()
        }

    # 前沿按 (gas, cost, 序号元组) 升序，首元素即该入选数的最优方案。
    best: tuple[tuple[int, ...], int, int] = ((), 0, 0)
    best_key: tuple[Any, ...] | None = None
    for count, frontier in states.items():
        if not frontier:
            continue
        gas, cost, chosen = frontier[0]
        key = (-count, gas, cost, chosen)
        if best_key is None or key < best_key:
            best_key = key
            best = (chosen, gas, cost)
    return best


def plan_bundle_nonce_chain(document: Any) -> dict[str, Any]:
    """按账户 nonce 链约束的批量打包规划。

    文档结构、校验次序、错误码与结果结构与 ``plan_bundle`` 相同；区别在于
    approved 请求按规范化的 sender（小写地址）与 nonce 分组：nonce 整数值除
    以 2 的 64 次方，商为 key、余数为 sequence。同一 (sender, key) 组的入选
    项按 requests 下标递增检查时，sequence 必须严格递增且相邻恰好相差 1。在
    该链约束与两条 bundle 限额下全局择优：先最大化入选数量，数量并列时依次
    取总 ``totalGas`` 较小、总 ``estimatedCostWei`` 较小、``selected`` 下标
    序列字典序较小的唯一方案。未入选的 approved 请求只记一个原因：最终组已
    有同 sender、key、sequence 记 ``E_NONCE_CONFLICT``；并入后破坏连续链记
    ``E_NONCE_GAP``；否则单独并入后先超 gas 记 ``E_BUNDLE_GAS``，不先超 gas
    但超成本记 ``E_BUNDLE_BUDGET``。只返回字典，不抛业务异常。
    """
    prepared = _prepare(document)
    if isinstance(prepared, dict):
        return prepared
    requests, sponsorship_policy, max_total_gas, max_cost_wei = prepared

    decisions = _evaluate_all(requests, sponsorship_policy)

    skipped: list[dict[str, Any]] = []
    # candidates 按原下标递增压入，平行的 indices 记录原下标；sender 取校验后
    # 的规范地址（小写），nonce 取规范 quantity 的整数值再拆分 key/sequence，
    # 故大小写或前导零差异不产生新键。
    candidates: list[tuple[Any, int, int, int, int]] = []
    indices: list[int] = []
    for index, (request, decision) in enumerate(zip(requests, decisions)):
        if not decision["approved"]:
            skipped.append({"index": index, "reason": decision["reason"]})
            continue
        # 请求已通过校验，此处取规范 sender/nonce 必然成功。
        user_op = validate(request)["normalized"]["userOperation"]
        nonce = int(user_op["nonce"], 16)
        candidates.append(
            (
                user_op["sender"],
                nonce // _NONCE_SEQUENCE_MOD,
                nonce % _NONCE_SEQUENCE_MOD,
                int(decision["totalGas"]),
                int(decision["estimatedCostWei"]),
            )
        )
        indices.append(index)

    chosen, chosen_gas, _ = _choose_nonce_chain(
        candidates, max_total_gas, max_cost_wei
    )

    chosen_set = set(chosen)
    selected = [indices[item] for item in chosen]
    # 每个 (sender, key) 组最终入选的 sequence -> 候选序号，用于判断落选项
    # 是重复 sequence 还是破坏连续链。
    chosen_sequences: dict[tuple[Any, int], dict[int, int]] = {}
    for item in chosen:
        sender, key, sequence = candidates[item][:3]
        chosen_sequences.setdefault((sender, key), {})[sequence] = item

    for item, (sender, key, sequence, item_gas, _) in enumerate(candidates):
        if item in chosen_set:
            continue
        index = indices[item]
        sequences = chosen_sequences.get((sender, key))
        if sequences is None:
            # 组内无入选项：单独成链必然合法，只可能受两条限额阻挡。
            extends = True
        elif sequence in sequences:
            extends = False
            skipped.append({"index": index, "reason": REASON_NONCE_CONFLICT})
            continue
        else:
            lowest = min(sequences)
            highest = max(sequences)
            # 入选链的 sequence 连续，并入后仍为连续链当且仅当该项接在链的
            # 某一端，且候选序号（与原下标同序）落在对应端点之外。
            extends = (
                sequence == lowest - 1 and item < sequences[lowest]
            ) or (
                sequence == highest + 1 and item > sequences[highest]
            )
        if not extends:
            skipped.append({"index": index, "reason": REASON_NONCE_GAP})
        elif chosen_gas + item_gas > max_total_gas:
            skipped.append({"index": index, "reason": REASON_BUNDLE_GAS})
        else:
            # chosen 已达约束下的最大可行数量：该项并入不破坏链且 gas 不超，
            # 并入必然使总成本超限。
            skipped.append({"index": index, "reason": REASON_BUNDLE_BUDGET})

    return _plan_result(selected, skipped, decisions)


def _sender_chain_frontiers(
    key_groups: list[list[tuple[int, int, int, int]]],
    limit: int,
    max_total_gas: int,
    max_cost_wei: int,
) -> list[list[tuple[int, int, tuple[int, ...]]]]:
    """单个 sender 跨多个 (sender, key) 组的子集前沿。

    返回 ``frontiers[t]``：该 sender 在所有 key 组合计恰好选 ``t`` 项时的
    (gas, cost, 候选序号元组) Pareto 前沿，``t`` 不超过配额 ``limit``。每个
    key 组内部沿用 ``_chain_group_frontiers`` 的连续链约束，不同 key 组互不
    影响但共享配额。逐组归并时被支配点不可能反超：后续 key 组的可选项对两点
    相同，gas 与 cost 都不更差者恒不更差；同 (gas, cost) 时序号元组字典序在
    有序归并下保持。
    """
    states: dict[int, list[tuple[int, int, tuple[int, ...]]]] = {0: [(0, 0, ())]}
    for items in key_groups:
        chain_frontiers = _chain_group_frontiers(items, max_total_gas, max_cost_wei)
        merged_states: dict[int, list[tuple[int, int, tuple[int, ...]]]] = {}
        for count, points in states.items():
            for taken, frontier in enumerate(chain_frontiers):
                if not frontier or count + taken > limit:
                    continue
                additions = merged_states.setdefault(count + taken, [])
                for gas, cost, chosen in points:
                    for add_gas, add_cost, add_chosen in frontier:
                        new_gas = gas + add_gas
                        new_cost = cost + add_cost
                        if new_gas > max_total_gas or new_cost > max_cost_wei:
                            continue
                        additions.append(
                            (new_gas, new_cost, tuple(sorted(chosen + add_chosen)))
                        )
        states = {
            count: _prune_frontier(points)
            for count, points in merged_states.items()
        }

    frontiers: list[list[tuple[int, int, tuple[int, ...]]]] = [
        [] for _ in range(limit + 1)
    ]
    for count, points in states.items():
        frontiers[count] = points
    return frontiers


def _choose_sender_nonce_chain(
    candidates: list[tuple[Any, int, int, int, int]],
    max_per_sender: int,
    max_total_gas: int,
    max_cost_wei: int,
) -> tuple[tuple[int, ...], int, int]:
    """在 sender 配额、nonce 链约束与两条限额下求最优入选子集。

    ``candidates`` 为按原下标递增的 ``(sender, key, sequence, gas, cost)``
    列表。每个 (sender, key) 组各自成链，同一 sender 的多个 key 组合计入选数
    不超过 ``max_per_sender``。目标依次：入选数量最大、不同 sender 数最大、
    总 gas 较小、总 cost 较小、候选序号序列字典序较小。返回
    ``(入选候选序号元组, 总gas, 总成本)``。
    """
    # 先按 sender 分组（sender 顺序按首次出现），再按 key 分组（key 顺序同样
    # 按首次出现）；组内保持候选序号递增。
    sender_groups: dict[Any, dict[int, list[tuple[int, int, int, int]]]] = {}
    sender_items: dict[Any, list[tuple[int, int, int, int]]] = {}
    for position, (sender, key, sequence, item_gas, item_cost) in enumerate(
        candidates
    ):
        sender_groups.setdefault(sender, {})
        groups = sender_groups[sender]
        groups.setdefault(key, []).append(
            (position, sequence, item_gas, item_cost)
        )
        sender_items.setdefault(sender, []).append(
            (position, sequence, item_gas, item_cost)
        )

    # 状态 (入选数, 不同 sender 数) -> (gas, cost, 候选序号元组) 的 Pareto
    # 前沿，结构与 _choose_sender_fair 相同；区别仅在单 sender 的可行子集由
    # 链约束与跨 key 配额共同界定。
    states: dict[tuple[int, int], list[tuple[int, int, tuple[int, ...]]]] = {
        (0, 0): [(0, 0, ())]
    }
    for sender, groups in sender_groups.items():
        limit = min(max_per_sender, len(sender_items[sender]))
        frontiers = _sender_chain_frontiers(
            list(groups.values()), limit, max_total_gas, max_cost_wei
        )
        merged_states: dict[
            tuple[int, int], list[tuple[int, int, tuple[int, ...]]]
        ] = {}
        for (count, sender_count), points in states.items():
            for taken, frontier in enumerate(frontiers):
                if not frontier:
                    continue
                key = (count + taken, sender_count + (1 if taken > 0 else 0))
                additions = merged_states.setdefault(key, [])
                for gas, cost, chosen in points:
                    for add_gas, add_cost, add_chosen in frontier:
                        new_gas = gas + add_gas
                        new_cost = cost + add_cost
                        if new_gas > max_total_gas or new_cost > max_cost_wei:
                            continue
                        additions.append(
                            (new_gas, new_cost, tuple(sorted(chosen + add_chosen)))
                        )
        states = {
            key: _prune_frontier(points)
            for key, points in merged_states.items()
        }

    # 前沿按 (gas, cost, 序号元组) 升序，首元素即该状态的最优点。
    best_key: tuple[Any, ...] | None = None
    best: tuple[tuple[int, ...], int, int] = ((), 0, 0)
    for (count, sender_count), frontier in states.items():
        if not frontier:
            continue
        gas, cost, chosen = frontier[0]
        key = (-count, -sender_count, gas, cost, chosen)
        if best_key is None or key < best_key:
            best_key = key
            best = (chosen, gas, cost)
    return best


def plan_bundle_sender_nonce_chain(document: Any) -> dict[str, Any]:
    """合并 sender 配额与 nonce 链约束的批量打包规划。

    文档恰好含 ``requests``、``sponsorshipPolicy``、``bundlePolicy`` 与
    ``fairnessPolicy`` 四个键（校验次序、错误码与 path 同
    ``plan_bundle_sender_fair``）。approved 请求按规范化的 sender（小写地址）
    与 nonce 分组：nonce 整数值除以 2 的 64 次方，商为 key、余数为
    sequence。同一 (sender, key) 组的入选项按 requests 下标递增检查时，
    sequence 必须严格递增且相邻恰好相差 1；不同 key 组各自成链，并与同一
    sender 的其他 key 组共享 ``fairnessPolicy.maxPerSender`` 配额。在该链约
    束、sender 配额与两条 bundle 限额下全局择优：先最大化入选数量，再最大化
    不同 sender 数，之后依次取总 ``totalGas`` 较小、总
    ``estimatedCostWei`` 较小、``selected`` 下标序列字典序较小的唯一方案。
    未入选的 approved 请求只记一个原因，按序判定：最终组已有同 sender、key、
    sequence 记 ``E_NONCE_CONFLICT``；单独并入破坏连续链记
    ``E_NONCE_GAP``；否则其 sender 入选数已达配额记 ``E_SENDER_QUOTA``；否
    则并入后先超 gas 记 ``E_BUNDLE_GAS``，不先超 gas 但超成本记
    ``E_BUNDLE_BUDGET``。只返回字典，不抛业务异常。
    """
    prepared = _prepare_sender_fair(document)
    if isinstance(prepared, dict):
        return prepared
    requests, sponsorship_policy, max_total_gas, max_cost_wei, max_per_sender = (
        prepared
    )

    decisions = _evaluate_all(requests, sponsorship_policy)

    skipped: list[dict[str, Any]] = []
    # candidates 按原下标递增压入，平行的 indices 记录原下标；sender 取校验后
    # 的规范地址（小写），nonce 取规范 quantity 的整数值再拆分 key/sequence。
    candidates: list[tuple[Any, int, int, int, int]] = []
    indices: list[int] = []
    for index, (request, decision) in enumerate(zip(requests, decisions)):
        if not decision["approved"]:
            skipped.append({"index": index, "reason": decision["reason"]})
            continue
        # 请求已通过校验，此处取规范 sender/nonce 必然成功。
        user_op = validate(request)["normalized"]["userOperation"]
        nonce = int(user_op["nonce"], 16)
        candidates.append(
            (
                user_op["sender"],
                nonce // _NONCE_SEQUENCE_MOD,
                nonce % _NONCE_SEQUENCE_MOD,
                int(decision["totalGas"]),
                int(decision["estimatedCostWei"]),
            )
        )
        indices.append(index)

    chosen, chosen_gas, _ = _choose_sender_nonce_chain(
        candidates, max_per_sender, max_total_gas, max_cost_wei
    )

    chosen_set = set(chosen)
    selected = [indices[item] for item in chosen]
    # 每个 (sender, key) 组最终入选的 sequence -> 候选序号，用于判断落选项是
    # 重复 sequence 还是破坏连续链。
    chosen_sequences: dict[tuple[Any, int], dict[int, int]] = {}
    sender_counts: dict[Any, int] = {}
    for item in chosen:
        sender, key, sequence = candidates[item][:3]
        chosen_sequences.setdefault((sender, key), {})[sequence] = item
        sender_counts[sender] = sender_counts.get(sender, 0) + 1

    for item, (sender, key, sequence, item_gas, _) in enumerate(candidates):
        if item in chosen_set:
            continue
        index = indices[item]
        sequences = chosen_sequences.get((sender, key))
        if sequences is not None and sequence in sequences:
            skipped.append({"index": index, "reason": REASON_NONCE_CONFLICT})
            continue
        if sequences is None:
            # 组内无入选项：单独成链必然合法。
            extends = True
        else:
            lowest = min(sequences)
            highest = max(sequences)
            # 入选链的 sequence 连续，并入后仍为连续链当且仅当该项接在链的
            # 某一端，且候选序号（与原下标同序）落在对应端点之外。
            extends = (
                sequence == lowest - 1 and item < sequences[lowest]
            ) or (
                sequence == highest + 1 and item > sequences[highest]
            )
        if not extends:
            skipped.append({"index": index, "reason": REASON_NONCE_GAP})
        elif sender_counts.get(sender, 0) >= max_per_sender:
            skipped.append({"index": index, "reason": REASON_SENDER_QUOTA})
        elif chosen_gas + item_gas > max_total_gas:
            skipped.append({"index": index, "reason": REASON_BUNDLE_GAS})
        else:
            # chosen 已达约束下的最大可行数量：该项不冲突、不破坏链、sender
            # 未满且 gas 不超，并入必然使总成本超限。
            skipped.append({"index": index, "reason": REASON_BUNDLE_BUDGET})

    return _plan_result(selected, skipped, decisions)
