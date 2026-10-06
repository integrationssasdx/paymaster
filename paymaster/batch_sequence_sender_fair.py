"""多批次 sender 公平打包规划：在多批次规划上叠加每 bundle 的 sender 上限。

公开入口 ``plan_batch_sequence_sender_fair(document)``：

- ``document`` 恰好含八个键：``requests``、``sponsorshipPolicy``、
  ``bundlePolicy``、``senderBudgetPolicy``、``senderUsage``、
  ``senderNonceState``、``batchPolicy``（前七项的语义、校验顺序、错误码与
  JSON Pointer 路径同 ``paymaster.batch_sequence.plan_batch_sequence``）与
  新增的 ``fairnessPolicy``。``fairnessPolicy`` 最后校验，恰好含
  ``maxPerSenderPerBatch``，为无前导零、大于 0 且不超过 2 的 64 次方减 1
  的规范 quantity，限制每个 bundle（批次）内同一规范 sender 的入选数量；
  缺键、未知键、非法类型或值依次返回
  ``E_FAIRNESS_POLICY_MISSING_FIELD``、``E_FAIRNESS_POLICY_UNKNOWN_FIELD``、
  ``E_FAIRNESS_POLICY_INVALID_FIELD``，path 指向 ``/fairnessPolicy`` 或其
  字段。
- 沿用多批次规划的全部约束：计入 ``senderUsage`` 的两条 sender 聚合限额、
  以 ``senderNonceState`` 为锚点且跨批不重置的 nonce 连续链、bundle 两条
  限额、``batchPolicy`` 的批次数与每批项数上限；另叠加每 bundle 内同一
  sender 的入选数上限。
- 最优目标依次：入选数量最大、批次数最小、不同 sender 数最大、总
  ``totalGas`` 较小、总 ``estimatedCostWei`` 较小、``selected`` 下标序列
  字典序最小。批次划分取贪心最长前缀（满足全部上限的最小批次数划分中唯一
  确定者）。
- 成功结果顶层键固定为 ``ok``、``plan``、``nextSenderUsage`` 与
  ``nextSenderNonceState``，``plan`` 结构与 skipped 原因同
  ``plan_batch_sequence``（未代付沿用 ``E_GAS_LIMIT``/``E_BUDGET``，已批
  准未入选记 ``E_NOT_SELECTED``）。

函数不改入参，不落盘、不访问外部系统，只返回字典且不抛业务异常。命令行
``python -m paymaster.batch_sequence_sender_fair``：从标准输入读一个 JSON
文档，只向标准输出写一个 JSON 文档；成功退出 0，失败退出 1；标准输入不是
合法 JSON 时输出 ``E_INVALID_JSON`` 结果（path 为空）。
"""

from __future__ import annotations

import json
import sys
from typing import Any

from paymaster.batch_state import _next_sender_nonce_state, _next_sender_usage
from paymaster.batch_sequence import _validate_batch_policy
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

# fairnessPolicy 校验失败的错误码。
E_FAIRNESS_POLICY_MISSING_FIELD = "E_FAIRNESS_POLICY_MISSING_FIELD"
E_FAIRNESS_POLICY_UNKNOWN_FIELD = "E_FAIRNESS_POLICY_UNKNOWN_FIELD"
E_FAIRNESS_POLICY_INVALID_FIELD = "E_FAIRNESS_POLICY_INVALID_FIELD"

# fairnessPolicy 的必需键，顺序即检查顺序（缺键与字段值均按此顺序报错）。
_FAIRNESS_POLICY_FIELDS = ("maxPerSenderPerBatch",)

# fairnessPolicy 字段取值上界（2 的 64 次方减 1）。
_MAX_FAIRNESS_POLICY_VALUE = (1 << 64) - 1

# nonce 链分组：nonce 整数值除以 2 的 64 次方，商为 key、余数为 sequence。
_NONCE_SEQUENCE_MOD = 1 << 64

# 根对象的必需键，顺序即缺键检查顺序；fairnessPolicy 最后校验。
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


def _validate_fairness_policy(
    policy: Any,
) -> tuple[dict[str, int] | None, dict[str, Any] | None]:
    """校验 fairnessPolicy 对象，返回 (规范化字段字典, None) 或 (None, 错误字典)。

    检查顺序：根类型、必需键、未知键、字段值（按
    ``_FAIRNESS_POLICY_FIELDS`` 顺序）。字段须为无前导零、大于 0 且不超过
    2 的 64 次方减 1 的规范 quantity。错误 path 以 ``/fairnessPolicy`` 为
    前缀。
    """
    prefix = _pointer("fairnessPolicy")
    if not isinstance(policy, dict):
        return None, _error(
            E_FAIRNESS_POLICY_INVALID_FIELD,
            prefix,
            "fairnessPolicy must be an object",
        )

    for name in _FAIRNESS_POLICY_FIELDS:
        if name not in policy:
            return None, _error(
                E_FAIRNESS_POLICY_MISSING_FIELD,
                prefix + _pointer(name),
                f"missing required field {name!r}",
            )

    for key in policy:
        if key not in _FAIRNESS_POLICY_FIELDS:
            return None, _error(
                E_FAIRNESS_POLICY_UNKNOWN_FIELD,
                prefix + _pointer(key),
                f"unknown field {key!r}",
            )

    normalized: dict[str, int] = {}
    for name in _FAIRNESS_POLICY_FIELDS:
        value = policy[name]
        if not isinstance(value, str) or not _QUANTITY_RE.match(value):
            return None, _error(
                E_FAIRNESS_POLICY_INVALID_FIELD,
                prefix + _pointer(name),
                f"invalid quantity value for field {name!r}",
            )
        number = int(value, 16)
        if number == 0:
            return None, _error(
                E_FAIRNESS_POLICY_INVALID_FIELD,
                prefix + _pointer(name),
                f"field {name!r} must be greater than 0",
            )
        if number > _MAX_FAIRNESS_POLICY_VALUE:
            return None, _error(
                E_FAIRNESS_POLICY_INVALID_FIELD,
                prefix + _pointer(name),
                f"field {name!r} must not exceed 2**64 - 1",
            )
        normalized[name] = number
    return normalized, None


def _prepare_batch_sequence_sender_fair(
    document: Any,
) -> tuple[
    list[Any], Any, int, int, int, int,
    dict[str, tuple[int, int]], dict[str, dict[int, int]], int, int, int,
] | dict[str, Any]:
    """校验多批次 sender 公平文档并返回 (requests, sponsorshipPolicy,
    maxTotalGas, maxCostWei, maxTotalGasPerSender, maxCostWeiPerSender, usage,
    nonceState, maxBatchCount, maxOperationsPerBatch, maxPerSenderPerBatch)。

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
    候选序号元组字典序更小的记录（同状态时总 gas、总成本、sender 数、批次数
    与当前批填充均相同，无需再比较）。"""
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
    填充各维不超过 y、两状态跨批累计的 sender 集合相同且 x 各 sender 的累计
    用量不超过 y、两状态当前批的 sender 集合相同且 x 各 sender 的本批入选数
    不超过 y，并且 x 在目标序列（入选数、批次数、sender 数、gas、成本、下
    标序列）上严格占优于某一更靠前的维度，或全部相同时下标序列字典序不更
    大。此时 y 的任何后续选择 x 都能模仿（并入当前批或同步另起新批）且最终
    结果不更差，y 可安全剪除。
    """
    _, x_acc, x_batches, x_fill, x_batch_senders = x_state
    _, y_acc, y_batches, y_fill, y_batch_senders = y_state
    x_count, x_chosen = x_record
    y_count, y_chosen = y_record
    if x_count < y_count or x_batches > y_batches:
        return False
    if any(x > y for x, y in zip(x_fill, y_fill)):
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
    # 当前批各 sender 入选数：集合相同且逐项不超过，x 才能模仿 y 向当前批的
    # 一切并入（每 bundle sender 上限对 x 不更紧）。
    x_per_batch = dict(x_batch_senders)
    y_per_batch = dict(y_batch_senders)
    if set(x_per_batch) != set(y_per_batch):
        return False
    if any(x_per_batch[sender] > y_per_batch[sender] for sender in x_per_batch):
        return False
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
    max_per_sender_per_batch: int,
) -> tuple[tuple[int, ...], int]:
    """求多批次 sender 公平规划的最优入选子集。

    与 ``paymaster.batch_sequence._choose_batch_sequence`` 的约束和目标相
    同，另要求每个批次内同一 sender 的入选数不超过
    ``max_per_sender_per_batch``（跨批配额不共享、随另起新批重置）。返回
    ``(入选候选序号元组, 批次数)``。

    状态在多批次 DP 的 (tails, acc, batches, fill) 上增加
    ``batch_senders``：当前批 (sender, 本批入选数) 按 sender 升序的元组。
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

    # 状态 (tails, acc, batches, fill, batch_senders) -> (入选数, 入选候选序号
    # 元组)：
    #   tails 为 ((sender, key), 链尾 sequence) 的升序元组；
    #   acc 为 (sender, 跨批累计 gas, 跨批累计 cost) 按 sender 升序的元组；
    #   fill 为当前批 (项数, gas, cost)；
    #   batch_senders 为当前批 (sender, 入选数) 按 sender 升序的元组。
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
        for (tails, acc, batches, fill, batch_senders), (count, chosen) in dp.items():
            # nonce 锚定链：须恰好接上该 (sender, key) 的下一 sequence。
            tails_dict = dict(tails)
            last = tails_dict.get((sender, key))
            expected = start if last is None else last + 1
            if sequence != expected:
                continue
            # sender 聚合限额（含既有用量，跨批累计）。
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
            new_record = (count + 1, chosen + (position,))
            fill_count, fill_gas, fill_cost = fill
            per_batch = dict(batch_senders)
            # 并入当前批：另须满足该 sender 的每批入选上限。
            if (
                batches >= 1
                and fill_count + 1 <= max_ops
                and fill_gas + item_gas <= max_total_gas
                and fill_cost + item_cost <= max_cost_wei
                and per_batch.get(sender, 0) < max_per_sender_per_batch
            ):
                per_batch[sender] = per_batch.get(sender, 0) + 1
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
                        tuple(sorted(per_batch.items())),
                    ),
                    new_record,
                )
            # 另起新批（首批也由此进入）；新批中该 sender 计数为 1，上限至少
            # 为 1，故无需再查每批 sender 上限。
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
    max_per_sender_per_batch: int,
) -> list[list[int]]:
    """把入选候选（序号递增）按贪心最长前缀划分为连续批次。

    每项尽量并入当前批，装不下（项数、两条限额或该 sender 每批入选上限）时
    另起新批；该划分在非负 gas、成本与计数下达到最小批次数，且是最小批次
    数划分中唯一的前缀最长者。
    """
    batches: list[list[int]] = []
    current: list[int] = []
    fill_count = 0
    fill_gas = 0
    fill_cost = 0
    per_batch: dict[Any, int] = {}
    for position in chosen:
        sender, _, _, item_gas, item_cost = candidates[position]
        sender_count = per_batch.get(sender, 0)
        if current and (
            fill_count + 1 > max_ops
            or fill_gas + item_gas > max_total_gas
            or fill_cost + item_cost > max_cost_wei
            or sender_count >= max_per_sender_per_batch
        ):
            batches.append(current)
            current = []
            fill_count = fill_gas = fill_cost = 0
            per_batch = {}
        current.append(position)
        fill_count += 1
        fill_gas += item_gas
        fill_cost += item_cost
        per_batch[sender] = per_batch.get(sender, 0) + 1
    if current:
        batches.append(current)
    return batches


def plan_batch_sequence_sender_fair(document: Any) -> dict[str, Any]:
    """多批次 sender 公平打包规划并结转纯内存批次状态。

    文档恰好含 ``requests``、``sponsorshipPolicy``、``bundlePolicy``、
    ``senderBudgetPolicy``、``senderUsage``、``senderNonceState``、
    ``batchPolicy`` 与 ``fairnessPolicy`` 八个键；前七项的校验顺序、错误码
    与 JSON Pointer 路径同 ``plan_batch_sequence``，``fairnessPolicy`` 最后
    校验，其 ``maxPerSenderPerBatch`` 限制每个 bundle 内同一规范 sender 的
    入选数。入选项按原下标顺序划分为不超过 ``maxBatchCount`` 个非空批次，
    每批不超过 ``maxOperationsPerBatch`` 项、满足 ``bundlePolicy`` 两条限额
    且每批每个 sender 不超过 ``maxPerSenderPerBatch`` 项；跨批仍受计入
    ``senderUsage`` 的 sender 聚合限额与锚定 nonce 连续链约束。成功结果含
    ``ok``、``plan``（结构同 ``plan_batch_sequence``）、
    ``nextSenderUsage`` 与 ``nextSenderNonceState``；未入选的 approved 请求
    记 ``E_NOT_SELECTED``。只返回字典，不抛业务异常。
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
        max_per_sender_per_batch,
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
        max_per_sender_per_batch,
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
        max_per_sender_per_batch,
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
