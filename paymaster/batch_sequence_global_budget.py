"""全局预算的多批次打包规划：在 plan_batch_sequence 的全部约束上叠加整个
请求序列的 totalGas 与 estimatedCostWei 总量上限。

公开入口 ``plan_batch_sequence_global_budget(document)``：

- ``document`` 恰好含八个键：``requests``、``sponsorshipPolicy``、
  ``bundlePolicy``、``senderBudgetPolicy``、``senderUsage``、
  ``senderNonceState``、``batchPolicy`` 与 ``sequenceBudgetPolicy``。前七项
  的语义、校验顺序、错误码与 JSON Pointer 路径同
  ``paymaster.batch_sequence.plan_batch_sequence``；
  ``sequenceBudgetPolicy`` 最后校验，恰好含 ``maxTotalGas`` 与
  ``maxCostWei``，均为无前导零、大于 0 且不超过 2 的 64 次方减 1 的规范
  quantity，限制全部入选项的 ``totalGas`` 总量与 ``estimatedCostWei`` 总
  量。缺键、未知键、非法类型或值依次返回
  ``E_SEQUENCE_POLICY_MISSING_FIELD``、
  ``E_SEQUENCE_POLICY_UNKNOWN_FIELD``、
  ``E_SEQUENCE_POLICY_INVALID_FIELD``，path 为 ``/sequenceBudgetPolicy``
  或其字段。
- 入选项仍受计入 ``senderUsage`` 的两条 sender 聚合限额约束；nonce 以
  ``senderNonceState`` 为锚点按 (sender, nonceKey) 连续递增，跨批不重置。
  入选项按原下标顺序划分为不超过 ``maxBatchCount`` 个非空批次，每批不超过
  ``maxOperationsPerBatch`` 项且满足 ``bundlePolicy`` 两条限额；全部入选项
  的 ``totalGas`` 总量与 ``estimatedCostWei`` 总量分别不超过
  ``sequenceBudgetPolicy`` 的 ``maxTotalGas`` 与 ``maxCostWei``。
- 最优目标依次：入选数量最大、批次数最小、不同 sender 数最大、总
  ``totalGas`` 较小、总 ``estimatedCostWei`` 较小、``selected`` 下标序列
  字典序最小。批次划分取贪心最长前缀（批次数最小的划分中唯一确定者）。
- 成功结果顶层键固定为 ``ok``、``plan``、``nextSenderUsage`` 与
  ``nextSenderNonceState``，``plan`` 结构与 ``plan_batch_sequence`` 完全
  相同（``selected``、``skipped``、``operationCount``、``totalGas``、
  ``estimatedCostWei``、``batches``，每批含 ``selected`` 与同样的汇总字
  段）；``skipped`` 对未入选项只记一个原因（未代付沿用
  ``E_GAS_LIMIT``/``E_BUDGET``，已批准未入选记 ``E_NOT_SELECTED``）。空
  请求列表成功，``batches`` 为空数组。

函数不改入参，只返回字典且不抛业务异常。命令行
``python -m paymaster.batch_sequence_global_budget``：从标准输入读一个
JSON 文档，只向标准输出写一个 JSON 文档；成功退出 0，失败退出 1；标准输
入不是合法 JSON 时输出 ``E_INVALID_JSON`` 结果（path 为空）。不访问节
点、数据库或文件。
"""

from __future__ import annotations

import json
import sys
from typing import Any

from paymaster.batch_sequence import (
    E_BATCH_POLICY_INVALID_FIELD,
    E_BATCH_POLICY_MISSING_FIELD,
    E_BATCH_POLICY_UNKNOWN_FIELD,
    REASON_NOT_SELECTED,
    _keep_better,
    _partition_batches,
    _prune_states,
    _validate_batch_policy,
)
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

# sequenceBudgetPolicy 校验失败的错误码。
E_SEQUENCE_POLICY_MISSING_FIELD = "E_SEQUENCE_POLICY_MISSING_FIELD"
E_SEQUENCE_POLICY_UNKNOWN_FIELD = "E_SEQUENCE_POLICY_UNKNOWN_FIELD"
E_SEQUENCE_POLICY_INVALID_FIELD = "E_SEQUENCE_POLICY_INVALID_FIELD"

# sequenceBudgetPolicy 的必需键，顺序即检查顺序（缺键与字段值均按此顺序报错）。
_SEQUENCE_POLICY_FIELDS = ("maxTotalGas", "maxCostWei")

# sequenceBudgetPolicy 字段取值上界（2 的 64 次方减 1）。
_MAX_SEQUENCE_POLICY_VALUE = (1 << 64) - 1

# nonce 链分组：nonce 整数值除以 2 的 64 次方，商为 key、余数为 sequence。
_NONCE_SEQUENCE_MOD = 1 << 64

# 根对象的必需键，顺序即缺键检查顺序；sequenceBudgetPolicy 最后校验。
_ROOT_FIELDS = (
    "requests",
    "sponsorshipPolicy",
    "bundlePolicy",
    "senderBudgetPolicy",
    "senderUsage",
    "senderNonceState",
    "batchPolicy",
    "sequenceBudgetPolicy",
)

__all__ = [
    "E_BATCH_POLICY_MISSING_FIELD",
    "E_BATCH_POLICY_UNKNOWN_FIELD",
    "E_BATCH_POLICY_INVALID_FIELD",
    "E_SEQUENCE_POLICY_MISSING_FIELD",
    "E_SEQUENCE_POLICY_UNKNOWN_FIELD",
    "E_SEQUENCE_POLICY_INVALID_FIELD",
    "REASON_NOT_SELECTED",
    "plan_batch_sequence_global_budget",
]


def _invalid_json_result(detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": E_INVALID_JSON,
            "path": "",
            "message": f"request is not a valid JSON document: {detail}",
        },
    }


def _validate_sequence_policy(
    policy: Any,
) -> tuple[dict[str, int] | None, dict[str, Any] | None]:
    """校验 sequenceBudgetPolicy 对象，返回 (规范化字段字典, None) 或
    (None, 错误字典)。

    检查顺序：根类型、必需键、未知键、字段值（按
    ``_SEQUENCE_POLICY_FIELDS`` 顺序）。字段须为无前导零、大于 0 且不超过
    2 的 64 次方减 1 的规范 quantity。错误 path 以
    ``/sequenceBudgetPolicy`` 为前缀。
    """
    prefix = _pointer("sequenceBudgetPolicy")
    if not isinstance(policy, dict):
        return None, _error(
            E_SEQUENCE_POLICY_INVALID_FIELD,
            prefix,
            "sequenceBudgetPolicy must be an object",
        )

    for name in _SEQUENCE_POLICY_FIELDS:
        if name not in policy:
            return None, _error(
                E_SEQUENCE_POLICY_MISSING_FIELD,
                prefix + _pointer(name),
                f"missing required field {name!r}",
            )

    for key in policy:
        if key not in _SEQUENCE_POLICY_FIELDS:
            return None, _error(
                E_SEQUENCE_POLICY_UNKNOWN_FIELD,
                prefix + _pointer(key),
                f"unknown field {key!r}",
            )

    normalized: dict[str, int] = {}
    for name in _SEQUENCE_POLICY_FIELDS:
        value = policy[name]
        if not isinstance(value, str) or not _QUANTITY_RE.match(value):
            return None, _error(
                E_SEQUENCE_POLICY_INVALID_FIELD,
                prefix + _pointer(name),
                f"invalid quantity value for field {name!r}",
            )
        number = int(value, 16)
        if number == 0:
            return None, _error(
                E_SEQUENCE_POLICY_INVALID_FIELD,
                prefix + _pointer(name),
                f"field {name!r} must be greater than 0",
            )
        if number > _MAX_SEQUENCE_POLICY_VALUE:
            return None, _error(
                E_SEQUENCE_POLICY_INVALID_FIELD,
                prefix + _pointer(name),
                f"field {name!r} must not exceed 2**64 - 1",
            )
        normalized[name] = number
    return normalized, None


def _prepare_batch_sequence_global_budget(
    document: Any,
) -> tuple[
    list[Any], Any, int, int, int, int,
    dict[str, tuple[int, int]], dict[str, dict[int, int]], int, int, int, int,
] | dict[str, Any]:
    """校验文档并返回 (requests, sponsorshipPolicy, maxTotalGas, maxCostWei,
    maxTotalGasPerSender, maxCostWeiPerSender, usage, nonceState,
    maxBatchCount, maxOperationsPerBatch, sequenceMaxTotalGas,
    sequenceMaxCostWei)。

    校验失败时返回错误字典（``ok`` 为 False）。校验次序：根类型、未知键、
    缺键、requests（逐项）、sponsorshipPolicy、bundlePolicy、
    senderBudgetPolicy、senderUsage、senderNonceState、batchPolicy、
    sequenceBudgetPolicy。
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

    sequence_policy, err = _validate_sequence_policy(document["sequenceBudgetPolicy"])
    if err is not None:
        return err

    assert sponsorship_policy is not None and bundle_policy is not None
    assert sender_budget_policy is not None and usage is not None
    assert nonce_state is not None and batch_policy is not None
    assert sequence_policy is not None

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
        sequence_policy["maxTotalGas"],
        sequence_policy["maxCostWei"],
    )


def _choose_batch_sequence_global_budget(
    candidates: list[tuple[Any, int, int, int, int]],
    usage: dict[Any, tuple[int, int]],
    nonce_state: dict[Any, dict[int, int]],
    max_gas_per_sender: int,
    max_cost_per_sender: int,
    max_total_gas: int,
    max_cost_wei: int,
    max_batches: int,
    max_ops: int,
    seq_max_total_gas: int,
    seq_max_cost_wei: int,
) -> tuple[tuple[int, ...], int]:
    """求全局预算多批次规划的最优入选子集。

    ``candidates`` 为按原下标递增的 ``(sender, key, sequence, gas, cost)``
    列表；``usage`` 为 sender -> (已累计 gas, 已累计成本)；``nonce_state``
    为 sender -> {key: lastSequence}。约束：每个 (sender, key) 组的入选项构
    成从 ``lastSequence + 1``（无状态时从 0）开始的连续链（跨批不重置）；
    计入既有用量后各 sender 的两条聚合限额；入选序列（保持原下标顺序）可划
    分为不超过 ``max_batches`` 个非空连续批次，每批不超过 ``max_ops`` 项且
    满足两条 bundle 限额；全部入选项的 gas 总量与成本总量分别不超过
    ``seq_max_total_gas`` 与 ``seq_max_cost_wei``。目标依次：入选数量最
    大、批次数最小、不同 sender 数最大、总 gas 较小、总 cost 较小、候选序
    号序列字典序较小。返回 ``(入选候选序号元组, 批次数)``。

    按候选序号顺序做子集 DP：状态为 (各组链尾, 各 sender 跨批累计, 已用批
    次数, 当前批填充)，与 ``plan_batch_sequence`` 相同；全局总量即各 sender
    跨批累计之和，故支配剪枝规则不变，只在转移时额外检查两条全局上限。
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

    # 状态 (tails, acc, batches, fill) -> (入选数, 入选候选序号元组)：
    #   tails 为 ((sender, key), 链尾 sequence) 的升序元组；
    #   acc 为 (sender, 跨批累计 gas, 跨批累计 cost) 按 sender 升序的元组；
    #   fill 为当前批 (项数, gas, cost)。
    dp: dict[tuple, tuple[int, tuple[int, ...]]] = {
        ((), (), 0, (0, 0, 0)): (0, ())
    }
    for position, (sender, key, sequence, item_gas, item_cost) in enumerate(
        candidates
    ):
        if (
            item_gas > max_total_gas
            or item_cost > max_cost_wei
            or item_gas > seq_max_total_gas
            or item_cost > seq_max_cost_wei
        ):
            continue  # 单项已超批限额或全局预算，任何批次都容不下
        start = starts[position]
        prior_gas, prior_cost = priors[position]
        additions: dict[tuple, tuple[int, tuple[int, ...]]] = {}
        for (tails, acc, batches, fill), (count, chosen) in dp.items():
            # nonce 锚定链：须恰好接上该 (sender, key) 的下一 sequence。
            tails_dict = dict(tails)
            last = tails_dict.get((sender, key))
            expected = start if last is None else last + 1
            if sequence != expected:
                continue
            # sender 聚合限额（含既有用量）。
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
            # 全局预算：全部入选项的 gas 总量与成本总量。
            if sum(gas for _, gas, _ in new_acc) > seq_max_total_gas:
                continue
            if sum(cost for _, _, cost in new_acc) > seq_max_cost_wei:
                continue
            new_record = (count + 1, chosen + (position,))
            fill_count, fill_gas, fill_cost = fill
            # 并入当前批。
            if (
                batches >= 1
                and fill_count + 1 <= max_ops
                and fill_gas + item_gas <= max_total_gas
                and fill_cost + item_cost <= max_cost_wei
            ):
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
                    ),
                    new_record,
                )
            # 另起新批（首批也由此进入）。
            if batches < max_batches:
                _keep_better(
                    additions,
                    (new_tails, new_acc, batches + 1, (1, item_gas, item_cost)),
                    new_record,
                )
        for state, record in additions.items():
            _keep_better(dp, state, record)
        dp = _prune_states(dp)

    # 目标序列：入选数降序、批次数升序、sender 数降序、总 gas 升序、总成本
    # 升序、入选候选序号元组字典序升序。
    best_key: tuple[Any, ...] | None = None
    best: tuple[tuple[int, ...], int] = ((), 0)
    for (_, acc, batches, _), (count, chosen) in dp.items():
        total_gas = sum(gas for _, gas, _ in acc)
        total_cost = sum(cost for _, _, cost in acc)
        key = (-count, batches, -len(acc), total_gas, total_cost, chosen)
        if best_key is None or key < best_key:
            best_key = key
            best = (chosen, batches)
    return best


def plan_batch_sequence_global_budget(document: Any) -> dict[str, Any]:
    """全局预算的多批次打包规划并结转纯内存批次状态。

    文档恰好含 ``requests``、``sponsorshipPolicy``、``bundlePolicy``、
    ``senderBudgetPolicy``、``senderUsage``、``senderNonceState``、
    ``batchPolicy`` 与 ``sequenceBudgetPolicy`` 八个键；前七项的校验顺序、
    错误码与 JSON Pointer 路径同 ``plan_batch_sequence``，
    ``sequenceBudgetPolicy`` 最后校验。入选项按原下标顺序划分为不超过
    ``maxBatchCount`` 个非空批次，每批不超过 ``maxOperationsPerBatch`` 项且
    满足 ``bundlePolicy``；入选项受计入 ``senderUsage`` 的 sender 聚合限额
    约束，nonce 以 ``senderNonceState`` 为锚点连续递增、跨批不重置；全部入
    选项的 ``totalGas`` 总量与 ``estimatedCostWei`` 总量分别不超过
    ``sequenceBudgetPolicy`` 的两条上限。成功结果含 ``ok``、``plan``（顶层
    汇总字段同 ``plan_batch_sequence``，另含 ``batches``）、
    ``nextSenderUsage`` 与 ``nextSenderNonceState``；未入选的 approved 请求
    记 ``E_NOT_SELECTED``。只返回字典，不抛业务异常。
    """
    prepared = _prepare_batch_sequence_global_budget(document)
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
        seq_max_total_gas,
        seq_max_cost_wei,
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

    chosen, _ = _choose_batch_sequence_global_budget(
        candidates,
        usage,
        nonce_state,
        max_gas_per_sender,
        max_cost_per_sender,
        max_total_gas,
        max_cost_wei,
        max_batches,
        max_ops,
        seq_max_total_gas,
        seq_max_cost_wei,
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
        candidates, chosen, max_ops, max_total_gas, max_cost_wei
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
        result = plan_batch_sequence_global_budget(document)

    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
