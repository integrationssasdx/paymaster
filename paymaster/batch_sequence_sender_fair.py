"""sender 分批公平的多批次打包规划：在 plan_batch_sequence 的全部约束上
叠加每个 bundle（批次）内同一规范 sender 的入选数上限。

公开入口 ``plan_batch_sequence_sender_fair(document)``：

- ``document`` 恰好含八个键：``requests``、``sponsorshipPolicy``、
  ``bundlePolicy``、``senderBudgetPolicy``、``senderUsage``、
  ``senderNonceState``、``batchPolicy`` 与 ``fairnessPolicy``。前七项的语
  义、校验顺序、错误码与 JSON Pointer 路径同
  ``paymaster.batch_sequence.plan_batch_sequence``；``fairnessPolicy`` 最
  后校验，恰好含 ``maxPerSenderPerBatch``，为无前导零、大于 0 且不超过 2
  的 64 次方减 1 的规范 quantity，限制每个 bundle 内同一规范 sender 的入
  选数量。缺键、未知键、非法类型或值依次返回
  ``E_FAIRNESS_POLICY_MISSING_FIELD``、
  ``E_FAIRNESS_POLICY_UNKNOWN_FIELD``、
  ``E_FAIRNESS_POLICY_INVALID_FIELD``，path 为 ``/fairnessPolicy`` 或其字
  段。
- 入选项仍受计入 ``senderUsage`` 的两条 sender 聚合限额约束；nonce 以
  ``senderNonceState`` 为锚点按 (sender, nonceKey) 连续递增，跨批不重置。
  入选项按原下标顺序划分为不超过 ``maxBatchCount`` 个非空批次，每批不超过
  ``maxOperationsPerBatch`` 项、满足 ``bundlePolicy`` 两条限额，且同一
  sender 在该批内不超过 ``maxPerSenderPerBatch`` 项。
- 最优目标依次：入选数量最大、批次数最小、不同 sender 数最大、总
  ``totalGas`` 较小、总 ``estimatedCostWei`` 较小、``selected`` 下标序列
  字典序最小。批次划分取贪心最长前缀（批次数最小的划分中唯一确定者）。
- 成功结果顶层键固定为 ``ok``、``plan``、``nextSenderUsage`` 与
  ``nextSenderNonceState``，``plan`` 结构与 ``plan_batch_sequence`` 完全
  相同；``skipped`` 对未入选项只记一个原因（未代付沿用
  ``E_GAS_LIMIT``/``E_BUDGET``，已批准未入选记 ``E_NOT_SELECTED``）。

函数不改入参，只返回字典且不抛业务异常。命令行
``python -m paymaster.batch_sequence_sender_fair``：从标准输入读一个 JSON
文档，只向标准输出写一个 JSON 文档；成功退出 0，失败退出 1；标准输入不是
合法 JSON 时输出 ``E_INVALID_JSON`` 结果（path 为空）。不访问节点、数据库
或文件。
"""

from __future__ import annotations

import json
import sys
from typing import Any

from paymaster.batch_state import _next_sender_nonce_state, _next_sender_usage
from paymaster.packing import (
    _BUNDLE_POLICY_FIELDS,
    _SENDER_BUDGET_POLICY_FIELDS,
    _error,
    _evaluate_all,
    _validate_policy,
    _validate_sender_nonce_state,
    _validate_sender_usage,
)
from paymaster.sponsorship import _POLICY_FIELDS
from paymaster.validation import (
    E_INVALID_FIELD,
    E_INVALID_JSON,
    E_MISSING_FIELD,
    E_UNKNOWN_FIELD,
    _QUANTITY_RE,
    _pointer,
    validate,
)

# 已批准但未入选的唯一原因。
REASON_NOT_SELECTED = "E_NOT_SELECTED"

# batchPolicy 校验失败的错误码。
E_BATCH_POLICY_MISSING_FIELD = "E_BATCH_POLICY_MISSING_FIELD"
E_BATCH_POLICY_UNKNOWN_FIELD = "E_BATCH_POLICY_UNKNOWN_FIELD"
E_BATCH_POLICY_INVALID_FIELD = "E_BATCH_POLICY_INVALID_FIELD"

# fairnessPolicy 校验失败的错误码。
E_FAIRNESS_POLICY_MISSING_FIELD = "E_FAIRNESS_POLICY_MISSING_FIELD"
E_FAIRNESS_POLICY_UNKNOWN_FIELD = "E_FAIRNESS_POLICY_UNKNOWN_FIELD"
E_FAIRNESS_POLICY_INVALID_FIELD = "E_FAIRNESS_POLICY_INVALID_FIELD"

# batchPolicy 的必需键，顺序即检查顺序（缺键与字段值均按此顺序报错）。
_BATCH_POLICY_FIELDS = ("maxBatchCount", "maxOperationsPerBatch")

# fairnessPolicy 的必需键（只有一个）。
_FAIRNESS_POLICY_FIELDS = ("maxPerSenderPerBatch",)

# batchPolicy / fairnessPolicy 字段取值上界（2 的 64 次方减 1）。
_MAX_POLICY_VALUE = (1 << 64) - 1

# nonce 链分组：nonce 整数值除以 2 的 64 次方，商为 key、余数为 sequence。
_NONCE_SEQUENCE_MOD = 1 << 64

# 根对象的必需键，顺序即缺键检查顺序；batchPolicy、fairnessPolicy 最后校验。
_ROOT_FIELDS = (
    "requests",
    "sponsorshipPolicy",
    "bundlePolicy",
    "senderBudgetPolicy",
    "senderUsage",
    "senderNonceState",
    "batchPolicy",
    "fairnessPolicy",
)


def _invalid_json_result(detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": E_INVALID_JSON,
            "path": "",
            "message": f"request is not a valid JSON document: {detail}",
        },
    }


def _validate_quantity_policy(
    policy: Any,
    fields: tuple[str, ...],
    segment: str,
    missing_code: str,
    unknown_code: str,
    invalid_code: str,
) -> tuple[dict[str, int] | None, dict[str, Any] | None]:
    """校验恰好含一组规范 quantity 字段（大于 0、不超过 2**64-1）的策略对象。

    返回 (规范化字段字典, None) 或 (None, 错误字典)。检查顺序：根类型、必需
    键、未知键、字段值（按 ``fields`` 顺序）。错误 path 以 ``/<segment>`` 为
    前缀，错误码分别由调用方给出。
    """
    prefix = _pointer(segment)
    if not isinstance(policy, dict):
        return None, _error(invalid_code, prefix, f"{segment} must be an object")

    for name in fields:
        if name not in policy:
            return None, _error(
                missing_code,
                prefix + _pointer(name),
                f"missing required field {name!r}",
            )

    for key in policy:
        if key not in fields:
            return None, _error(
                unknown_code,
                prefix + _pointer(key),
                f"unknown field {key!r}",
            )

    normalized: dict[str, int] = {}
    for name in fields:
        value = policy[name]
        if not isinstance(value, str) or not _QUANTITY_RE.match(value):
            return None, _error(
                invalid_code,
                prefix + _pointer(name),
                f"invalid quantity value for field {name!r}",
            )
        number = int(value, 16)
        if number == 0:
            return None, _error(
                invalid_code,
                prefix + _pointer(name),
                f"field {name!r} must be greater than 0",
            )
        if number > _MAX_POLICY_VALUE:
            return None, _error(
                invalid_code,
                prefix + _pointer(name),
                f"field {name!r} must not exceed 2**64 - 1",
            )
        normalized[name] = number
    return normalized, None


def _validate_batch_policy(
    policy: Any,
) -> tuple[dict[str, int] | None, dict[str, Any] | None]:
    """校验 batchPolicy 对象，规则与
    ``paymaster.batch_sequence._validate_batch_policy`` 相同。"""
    return _validate_quantity_policy(
        policy,
        _BATCH_POLICY_FIELDS,
        "batchPolicy",
        E_BATCH_POLICY_MISSING_FIELD,
        E_BATCH_POLICY_UNKNOWN_FIELD,
        E_BATCH_POLICY_INVALID_FIELD,
    )


def _validate_fairness_policy(
    policy: Any,
) -> tuple[dict[str, int] | None, dict[str, Any] | None]:
    """校验 fairnessPolicy 对象，返回 (规范化字段字典, None) 或
    (None, 错误字典)。

    检查顺序：根类型、必需键、未知键、字段值。``maxPerSenderPerBatch`` 须
    为无前导零、大于 0 且不超过 2 的 64 次方减 1 的规范 quantity。错误 path
    以 ``/fairnessPolicy`` 为前缀。
    """
    return _validate_quantity_policy(
        policy,
        _FAIRNESS_POLICY_FIELDS,
        "fairnessPolicy",
        E_FAIRNESS_POLICY_MISSING_FIELD,
        E_FAIRNESS_POLICY_UNKNOWN_FIELD,
        E_FAIRNESS_POLICY_INVALID_FIELD,
    )


def _prepare_batch_sequence_sender_fair(
    document: Any,
) -> tuple[
    list[Any], Any, int, int, int, int,
    dict[str, tuple[int, int]], dict[str, dict[int, int]], int, int, int,
] | dict[str, Any]:
    """校验文档并返回 (requests, sponsorshipPolicy, maxTotalGas, maxCostWei,
    maxTotalGasPerSender, maxCostWeiPerSender, usage, nonceState,
    maxBatchCount, maxOperationsPerBatch, maxPerSenderPerBatch)。

    校验失败时返回错误字典（``ok`` 为 False）。校验次序：根类型、未知键、
    缺键、requests（逐项）、sponsorshipPolicy、bundlePolicy、
    senderBudgetPolicy、senderUsage、senderNonceState、batchPolicy、
    fairnessPolicy。
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

    sender_budget_policy, err = _validate_policy(
        document["senderBudgetPolicy"],
        _SENDER_BUDGET_POLICY_FIELDS,
        "senderBudgetPolicy",
    )
    if err is not None:
        return err

    usage, err = _validate_sender_usage(document["senderUsage"])
    if err is not None:
        return err

    nonce_state, err = _validate_sender_nonce_state(document["senderNonceState"])
    if err is not None:
        return err

    batch_policy, err = _validate_batch_policy(document["batchPolicy"])
    if err is not None:
        return err

    fairness_policy, err = _validate_fairness_policy(document["fairnessPolicy"])
    if err is not None:
        return err

    assert sponsorship_policy is not None and bundle_policy is not None
    assert sender_budget_policy is not None and usage is not None
    assert nonce_state is not None and batch_policy is not None
    assert fairness_policy is not None

    return (
        requests,
        document["sponsorshipPolicy"],
        int(bundle_policy["maxTotalGas"], 16),
        int(bundle_policy["maxCostWei"], 16),
        int(sender_budget_policy["maxTotalGasPerSender"], 16),
        int(sender_budget_policy["maxCostWeiPerSender"], 16),
        usage,
        nonce_state,
        batch_policy["maxBatchCount"],
        batch_policy["maxOperationsPerBatch"],
        fairness_policy["maxPerSenderPerBatch"],
    )


def _keep_better(
    states: dict[tuple, tuple[int, tuple[int, ...]]],
    state: tuple,
    record: tuple[int, tuple[int, ...]],
) -> None:
    """把 (state, record) 并入状态表：同一状态只保留入选数更大、并列时入选
    候选序号元组字典序更小的记录（同状态时总 gas、总成本、sender 数、批次
    数与各批 sender 分布相同，无需再比较）。"""
    existing = states.get(state)
    if (
        existing is None
        or record[0] > existing[0]
        or (record[0] == existing[0] and record[1] < existing[1])
    ):
        states[state] = record


def _dominates(
    x_state: tuple,
    x_record: tuple[int, tuple[int, ...]],
    y_state: tuple,
    y_record: tuple[int, tuple[int, ...]],
) -> bool:
    """判断状态 x 是否支配状态 y（两者的 nonce 链尾 tails 相同）。

    x 支配 y 当且仅当：x 的入选数更多或相同、已用批次数更少或相同、当前批
    填充各维（项数、gas、成本、各 sender 入选数）不超过 y、两状态的跨批
    sender 集合相同且 x 各 sender 的累计用量不超过 y，并且 x 在目标序列
    （入选数、批次数、sender 数、gas、成本、下标序列）上严格占优于某一更
    靠前的维度，或全部相同时下标序列字典序不更大。此时 y 的任何后续选择 x
    都能模仿且最终结果不更差，y 可安全剪除。
    """
    _, x_acc, x_batches, x_fill, x_fill_senders = x_state
    _, y_acc, y_batches, y_fill, y_fill_senders = y_state
    x_count, x_chosen = x_record
    y_count, y_chosen = y_record
    if x_count < y_count or x_batches > y_batches:
        return False
    if any(x > y for x, y in zip(x_fill, y_fill)):
        return False
    x_fill_dict = dict(x_fill_senders)
    y_fill_dict = dict(y_fill_senders)
    # 当前批各 sender 入选数：缺失记 0；任一 sender 在 x 中更多则 x 不能模仿
    # y 后续对该 sender 的批内入选。
    if any(
        x_fill_dict.get(sender, 0) > y_fill_dict.get(sender, 0)
        for sender in set(x_fill_dict) | set(y_fill_dict)
    ):
        return False
    x_usage = {sender: (gas, cost) for sender, gas, cost in x_acc}
    y_usage = {sender: (gas, cost) for sender, gas, cost in y_acc}
    if set(x_usage) != set(y_usage):
        return False
    less_usage = False
    for sender, (x_gas, x_cost) in x_usage.items():
        y_gas, y_cost = y_usage[sender]
        if x_gas > y_gas or x_cost > y_cost:
            return False
        if x_gas < y_gas or x_cost < y_cost:
            less_usage = True
    if x_count > y_count or x_batches < y_batches or less_usage:
        return True
    return x_chosen <= y_chosen


def _prune_states(
    dp: dict[tuple, tuple[int, tuple[int, ...]]],
) -> dict[tuple, tuple[int, tuple[int, ...]]]:
    """按 nonce 链尾分组做支配剪枝，不改变最终最优解。"""
    groups: dict[tuple, list[tuple[tuple, tuple[int, tuple[int, ...]]]]] = {}
    for state, record in dp.items():
        groups.setdefault(state[0], []).append((state, record))
    pruned: dict[tuple, tuple[int, tuple[int, ...]]] = {}
    for members in groups.values():
        for position, (state, record) in enumerate(members):
            if any(
                _dominates(other_state, other_record, state, record)
                for other, (other_state, other_record) in enumerate(members)
                if other != position
            ):
                continue
            pruned[state] = record
    return pruned


def _choose_batch_sequence_sender_fair(
    candidates: list[tuple[Any, int, int, int, int]],
    usage: dict[Any, tuple[int, int]],
    nonce_state: dict[Any, dict[int, int]],
    max_gas_per_sender: int,
    max_cost_per_sender: int,
    max_total_gas: int,
    max_cost_wei: int,
    max_batches: int,
    max_ops: int,
    max_senders: int,
) -> tuple[tuple[int, ...], int]:
    """求 sender 分批公平多批次规划的最优入选子集。

    ``candidates`` 为按原下标递增的 ``(sender, key, sequence, gas, cost)``
    列表；``usage`` 为 sender -> (已累计 gas, 已累计成本)；``nonce_state``
    为 sender -> {key: lastSequence}。约束：每个 (sender, key) 组的入选项构
    成从 ``lastSequence + 1``（无状态时从 0）开始的连续链（跨批不重置）；
    计入既有用量后各 sender 的两条跨批聚合限额；入选序列（保持原下标顺序）
    可划分为不超过 ``max_batches`` 个非空连续批次，每批不超过 ``max_ops``
    项、满足两条 bundle 限额，且同一 sender 在该批内至多 ``max_senders``
    项。目标依次：入选数量最大、批次数最小、不同 sender 数最大、总 gas 较
    小、总 cost 较小、候选序号序列字典序较小。返回
    ``(入选候选序号元组, 批次数)``。

    按候选序号顺序做子集 DP：状态为 (各组链尾, 各 sender 跨批累计, 已用批次
    数, 当前批填充, 当前批各 sender 入选数)，每个候选只有"跳过"与"入选"
    （并入当前批或另起新批）两种转移；同 nonce 链尾的状态间做支配剪枝。
    """
    # 每个候选的锚点起点与既有用量，与候选序号对齐。
    starts: list[int] = []
    priors: list[tuple[int, int]] = []
    for sender, key, _, _, _ in candidates:
        sender_state = nonce_state.get(sender)
        start = 0
        if sender_state is not None and key in sender_state:
            start = sender_state[key] + 1
        starts.append(start)
        priors.append(usage.get(sender, (0, 0)))

    # 状态 (tails, acc, batches, fill, fillSenders) -> (入选数, 入选候选序号元组)：
    #   tails 为 ((sender, key), 链尾 sequence) 的升序元组；
    #   acc 为 (sender, 跨批累计 gas, 跨批累计 cost) 按 sender 升序的元组；
    #   fill 为当前批 (项数, gas, cost)；
    #   fillSenders 为 (sender, 本批入选数) 按 sender 升序的元组。
    dp: dict[tuple, tuple[int, tuple[int, ...]]] = {
        ((), (), 0, (0, 0, 0), ()): (0, ())
    }
    for position, (sender, key, sequence, item_gas, item_cost) in enumerate(
        candidates
    ):
        if item_gas > max_total_gas or item_cost > max_cost_wei:
            continue  # 单项已超批限额，任何批次都容不下
        start = starts[position]
        prior_gas, prior_cost = priors[position]
        additions: dict[tuple, tuple[int, tuple[int, ...]]] = {}
        for (tails, acc, batches, fill, fill_senders), (count, chosen) in dp.items():
            # nonce 锚定链：须恰好接上该 (sender, key) 的下一 sequence。
            tails_dict = dict(tails)
            last = tails_dict.get((sender, key))
            expected = start if last is None else last + 1
            if sequence != expected:
                continue
            # sender 跨批聚合限额（含既有用量）。
            acc_dict = {s: (g, c) for s, g, c in acc}
            used_gas, used_cost = acc_dict.get(sender, (0, 0))
            if used_gas + prior_gas + item_gas > max_gas_per_sender:
                continue
            if used_cost + prior_cost + item_cost > max_cost_per_sender:
                continue
            tails_dict[(sender, key)] = sequence
            new_tails = tuple(sorted(tails_dict.items()))
            acc_dict[sender] = (used_gas + item_gas, used_cost + item_cost)
            new_acc = tuple(
                sorted((s, g, c) for s, (g, c) in acc_dict.items())
            )
            # 当前批内该 sender 的入选数（另起新批时在下方重建）。
            fill_sender_dict = {s: n for s, n in fill_senders}
            sender_in_batch = fill_sender_dict.get(sender, 0)
            new_record = (count + 1, chosen + (position,))
            fill_count, fill_gas, fill_cost = fill
            # 并入当前批。
            if (
                batches >= 1
                and fill_count + 1 <= max_ops
                and fill_gas + item_gas <= max_total_gas
                and fill_cost + item_cost <= max_cost_wei
                and sender_in_batch + 1 <= max_senders
            ):
                fill_sender_dict[sender] = sender_in_batch + 1
                _keep_better(
                    additions,
                    (
                        new_tails,
                        new_acc,
                        batches,
                        (
                            fill_count + 1,
                            fill_gas + item_gas,
                            fill_cost + item_cost,
                        ),
                        tuple(sorted(fill_sender_dict.items())),
                    ),
                    new_record,
                )
            # 另起新批（首批也由此进入）。
            if batches < max_batches:
                _keep_better(
                    additions,
                    (
                        new_tails,
                        new_acc,
                        batches + 1,
                        (1, item_gas, item_cost),
                        ((sender, 1),),
                    ),
                    new_record,
                )
        for state, record in additions.items():
            _keep_better(dp, state, record)
        dp = _prune_states(dp)

    # 目标序列：入选数降序、批次数升序、sender 数降序、总 gas 升序、总成本
    # 升序、入选候选序号元组字典序升序。
    best_key: tuple[Any, ...] | None = None
    best: tuple[tuple[int, ...], int] = ((), 0)
    for (_, acc, batches, _, _), (count, chosen) in dp.items():
        total_gas = sum(gas for _, gas, _ in acc)
        total_cost = sum(cost for _, _, cost in acc)
        key = (-count, batches, -len(acc), total_gas, total_cost, chosen)
        if best_key is None or key < best_key:
            best_key = key
            best = (chosen, batches)
    return best


def _partition_batches(
    candidates: list[tuple[Any, int, int, int, int]],
    chosen: tuple[int, ...],
    max_ops: int,
    max_total_gas: int,
    max_cost_wei: int,
    max_senders: int,
) -> list[list[int]]:
    """把入选候选（序号递增）按贪心最长前缀划分为连续批次。

    每项尽量并入当前批，装不下（项数、两条限额或该 sender 批内入选数）时另
    起新批；该划分在非负 gas、成本与计数约束下达到最小批次数，且是最小批
    次数划分中唯一的前缀最长者。
    """
    batches: list[list[int]] = []
    current: list[int] = []
    fill_count = 0
    fill_gas = 0
    fill_cost = 0
    fill_senders: dict[Any, int] = {}
    for position in chosen:
        sender, _, _, item_gas, item_cost = candidates[position]
        sender_in_batch = fill_senders.get(sender, 0)
        if current and (
            fill_count + 1 > max_ops
            or fill_gas + item_gas > max_total_gas
            or fill_cost + item_cost > max_cost_wei
            or sender_in_batch + 1 > max_senders
        ):
            batches.append(current)
            current = []
            fill_count = fill_gas = fill_cost = 0
            fill_senders = {}
            sender_in_batch = 0
        current.append(position)
        fill_count += 1
        fill_gas += item_gas
        fill_cost += item_cost
        fill_senders[sender] = sender_in_batch + 1
    if current:
        batches.append(current)
    return batches


def plan_batch_sequence_sender_fair(document: Any) -> dict[str, Any]:
    """sender 分批公平的多批次打包规划并结转纯内存批次状态。

    文档恰好含 ``requests``、``sponsorshipPolicy``、``bundlePolicy``、
    ``senderBudgetPolicy``、``senderUsage``、``senderNonceState``、
    ``batchPolicy`` 与 ``fairnessPolicy`` 八个键；前七项的校验顺序、错误码
    与 JSON Pointer 路径同 ``plan_batch_sequence``，``fairnessPolicy`` 最后
    校验。入选项按原下标顺序划分为不超过 ``maxBatchCount`` 个非空批次，每批
    不超过 ``maxOperationsPerBatch`` 项、满足 ``bundlePolicy`` 且同一
    sender 每批不超过 ``maxPerSenderPerBatch`` 项；入选项受计入
    ``senderUsage`` 的 sender 聚合限额约束，nonce 以 ``senderNonceState``
    为锚点连续递增、跨批不重置。成功结果含 ``ok``、``plan``（顶层汇总字段
    同 ``plan_batch_sequence``，另含 ``batches``）、``nextSenderUsage`` 与
    ``nextSenderNonceState``；未入选的 approved 请求记 ``E_NOT_SELECTED``。
    只返回字典，不抛业务异常。
    """
    prepared = _prepare_batch_sequence_sender_fair(document)
    if isinstance(prepared, dict):
        return prepared
    (
        requests,
        sponsorship_policy,
        max_total_gas,
        max_cost_wei,
        max_gas_per_sender,
        max_cost_per_sender,
        usage,
        nonce_state,
        max_batches,
        max_ops,
        max_senders,
    ) = prepared

    decisions = _evaluate_all(requests, sponsorship_policy)

    skipped: list[dict[str, Any]] = []
    # candidates 按原下标递增压入，平行的 indices 记录原下标；sender 取校验后
    # 的规范地址（小写），nonce 取规范 quantity 的整数值再拆 key/sequence。
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

    chosen, _ = _choose_batch_sequence_sender_fair(
        candidates,
        usage,
        nonce_state,
        max_gas_per_sender,
        max_cost_per_sender,
        max_total_gas,
        max_cost_wei,
        max_batches,
        max_ops,
        max_senders,
    )

    chosen_set = set(chosen)
    for position in range(len(candidates)):
        if position not in chosen_set:
            skipped.append({"index": indices[position], "reason": REASON_NOT_SELECTED})
    skipped.sort(key=lambda item: item["index"])

    # chosen 按候选序号递增，对应原下标亦递增。
    selected = [indices[position] for position in chosen]

    batches: list[dict[str, Any]] = []
    for group in _partition_batches(
        candidates,
        chosen,
        max_ops,
        max_total_gas,
        max_cost_wei,
        max_senders,
    ):
        group_selected = [indices[position] for position in group]
        group_gas = sum(int(decisions[index]["totalGas"]) for index in group_selected)
        group_cost = sum(
            int(decisions[index]["estimatedCostWei"]) for index in group_selected
        )
        batches.append(
            {
                "selected": group_selected,
                "operationCount": str(len(group_selected)),
                "totalGas": str(group_gas),
                "estimatedCostWei": str(group_cost),
            }
        )

    total_gas = sum(int(decisions[index]["totalGas"]) for index in selected)
    total_cost = sum(int(decisions[index]["estimatedCostWei"]) for index in selected)
    plan = {
        "selected": selected,
        "skipped": skipped,
        "operationCount": str(len(selected)),
        "totalGas": str(total_gas),
        "estimatedCostWei": str(total_cost),
        "batches": batches,
    }

    return {
        "ok": True,
        "plan": plan,
        "nextSenderUsage": _next_sender_usage(
            document["senderUsage"], selected, requests, decisions
        ),
        "nextSenderNonceState": _next_sender_nonce_state(
            document["senderNonceState"], selected, requests
        ),
    }


def main() -> int:
    raw = sys.stdin.read()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        result = _invalid_json_result(str(exc))
    else:
        result = plan_batch_sequence_sender_fair(document)

    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
