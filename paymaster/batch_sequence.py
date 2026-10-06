"""多批次规划：在单批状态结转基线上把 UserOperation 按序分入多个 bundle。

公开入口 ``plan_batch_sequence(document)``：

- ``document`` 在
  ``paymaster.packing.plan_bundle_sender_budget_with_nonce_state`` 的六个字
  段（``requests``、``sponsorshipPolicy``、``bundlePolicy``、
  ``senderBudgetPolicy``、``senderUsage``、``senderNonceState``，语义与校验
  顺序不变）之外增加第七个字段 ``batchPolicy``，且恰好含这七个键。
- ``batchPolicy`` 最后校验，恰好含 ``maxBatchCount`` 与
  ``maxOperationsPerBatch``，均为无前导零、不超过 ``2**64 - 1`` 的正
  quantity（``0x`` 前缀规范十六进制）。缺键、未知键、非法值分别返回
  ``E_BATCH_POLICY_MISSING_FIELD``、``E_BATCH_POLICY_UNKNOWN_FIELD``、
  ``E_BATCH_POLICY_INVALID_FIELD``，根 path 为 ``/batchPolicy``，字段 path
  为其下 JSON Pointer。
- 校验通过后把 approved 请求按原下标组成不超过 ``maxBatchCount`` 个非空批
  次：每批不超过 ``maxOperationsPerBatch`` 项且独立满足 ``bundlePolicy``
  两条限额；sender 聚合限额计入 ``senderUsage`` 且跨全部批次累计；nonce 以
  ``senderNonceState`` 为锚点，按 sender、nonceKey 跨批次连续递增不重置。
- 最优目标依次为：最大化入选数、最小化批次数、最大化不同 sender 数、减小
  全部入选项总 gas 与总成本，最后取入选下标的扁平序列字典序最小者。
- 成功结果顶层固定为 ``ok``、``plan``、``nextSenderUsage``、
  ``nextSenderNonceState``；``plan`` 沿用单批汇总字段（``selected``、
  ``skipped``、``operationCount``、``totalGas``、``estimatedCostWei``），另
  含 ``batches``。``skipped`` 对未入选项只给一个原因：未代付沿用
  ``E_GAS_LIMIT``/``E_BUDGET``，已批准未入选记 ``E_NOT_SELECTED``。
- ``nextSenderUsage`` 与 ``nextSenderNonceState`` 按
  :func:`paymaster.batch_state.advance_batch_state` 的规则结转全部入选项。

函数不改入参、不落盘、不访问外部系统，只返回字典且不抛业务异常。

命令行 ``python -m paymaster.batch_sequence``：从标准输入读 JSON、只向标准
输出写一个 JSON 文档；成功退出 0、失败退出 1；标准输入不是合法 JSON 时输出
``E_INVALID_JSON`` 结果（path 为空）。
"""

from __future__ import annotations

import json
import sys
from typing import Any

from paymaster.batch_state import _next_sender_nonce_state, _next_sender_usage
from paymaster.packing import (
    _evaluate_all,
    _prepare_sender_budget_with_nonce_state,
)
from paymaster.validation import (
    E_INVALID_JSON,
    E_MISSING_FIELD,
    E_UNKNOWN_FIELD,
    _MAX_QUANTITY_HEX_DIGITS,
    _QUANTITY_RE,
    _pointer,
    validate,
)

# batchPolicy 错误码：缺键、未知键、非法值（根非对象也用 INVALID_FIELD）。
E_BATCH_POLICY_MISSING_FIELD = "E_BATCH_POLICY_MISSING_FIELD"
E_BATCH_POLICY_UNKNOWN_FIELD = "E_BATCH_POLICY_UNKNOWN_FIELD"
E_BATCH_POLICY_INVALID_FIELD = "E_BATCH_POLICY_INVALID_FIELD"

# 已批准但未入选的统一原因。
REASON_NOT_SELECTED = "E_NOT_SELECTED"

# 多批次文档的根对象必需键，顺序即缺键检查顺序；前六项与单批基线相同。
_BATCH_SEQUENCE_ROOT_FIELDS = (
    "requests",
    "sponsorshipPolicy",
    "bundlePolicy",
    "senderBudgetPolicy",
    "senderUsage",
    "senderNonceState",
    "batchPolicy",
)

# batchPolicy 的必需键，顺序即检查顺序（缺键与字段值均按此顺序报错）。
_BATCH_POLICY_FIELDS = ("maxBatchCount", "maxOperationsPerBatch")

# batchPolicy 数量上界：不超过 2 的 64 次方减 1 的正整数。
_MAX_BATCH_QUANTITY = (1 << 64) - 1

# nonce 链分组：nonce 整数值除以 2 的 64 次方，商为 key、余数为 sequence。
_NONCE_SEQUENCE_MOD = 1 << 64


def _invalid_json_result(detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": E_INVALID_JSON,
            "path": "",
            "message": f"request is not a valid JSON document: {detail}",
        },
    }


def _error(code: str, path: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "path": path, "message": message}}


def _validate_batch_policy(
    policy: Any,
) -> tuple[dict[str, int] | None, dict[str, Any] | None]:
    """校验 batchPolicy，返回 (规范化字段字典, None) 或 (None, 错误字典)。

    检查顺序：根类型、缺键、未知键、字段值（按 ``_BATCH_POLICY_FIELDS`` 顺
    序）。字段值须为规范 quantity（``0x`` 前缀、无多余前导零、不超过 32 字
    节），且为不超过 ``2**64 - 1`` 的正整数。
    """
    prefix = _pointer("batchPolicy")
    if not isinstance(policy, dict):
        return None, _error(
            E_BATCH_POLICY_INVALID_FIELD, prefix, "batchPolicy must be an object"
        )

    for name in _BATCH_POLICY_FIELDS:
        if name not in policy:
            return None, _error(
                E_BATCH_POLICY_MISSING_FIELD,
                prefix + _pointer(name),
                f"missing required field {name!r}",
            )

    for key in policy:
        if key not in _BATCH_POLICY_FIELDS:
            return None, _error(
                E_BATCH_POLICY_UNKNOWN_FIELD,
                prefix + _pointer(key),
                f"unknown field {key!r}",
            )

    normalized: dict[str, int] = {}
    for name in _BATCH_POLICY_FIELDS:
        value = policy[name]
        if (
            not isinstance(value, str)
            or not _QUANTITY_RE.match(value)
            or len(value) - 2 > _MAX_QUANTITY_HEX_DIGITS
        ):
            return None, _error(
                E_BATCH_POLICY_INVALID_FIELD,
                prefix + _pointer(name),
                f"invalid quantity value for field {name!r}",
            )
        number = int(value, 16)
        if not 1 <= number <= _MAX_BATCH_QUANTITY:
            return None, _error(
                E_BATCH_POLICY_INVALID_FIELD,
                prefix + _pointer(name),
                f"field {name!r} must be a positive integer not greater than 2**64-1",
            )
        normalized[name] = number
    return normalized, None


def _prepare(
    document: Any,
) -> tuple[
    list[Any],
    Any,
    int,
    int,
    int,
    int,
    dict[str, tuple[int, int]],
    dict[str, dict[int, int]],
    int,
    int,
] | dict[str, Any]:
    """校验七字段文档。

    根类型、根未知键、根缺键按七字段全集检查；前六字段的逐项校验与
    ``_prepare_sender_budget_with_nonce_state`` 完全同序同码同路径（以只含
    六字段的副本调用，不修改入参）；``batchPolicy`` 在
    ``senderNonceState`` 之后最后校验。成功时返回 (requests,
    sponsorshipPolicy, maxTotalGas, maxCostWei, maxGasPerSender,
    maxCostPerSender, usage, nonceState, maxBatchCount,
    maxOperationsPerBatch)。
    """
    if not isinstance(document, dict):
        return _error(E_INVALID_JSON, "", "document root must be a JSON object")

    for key in document:
        if key not in _BATCH_SEQUENCE_ROOT_FIELDS:
            return _error(E_UNKNOWN_FIELD, _pointer(key), f"unknown field {key!r}")
    for key in _BATCH_SEQUENCE_ROOT_FIELDS:
        if key not in document:
            return _error(
                E_MISSING_FIELD, _pointer(key), f"missing required field {key!r}"
            )

    six_field_document = {
        name: document[name] for name in _BATCH_SEQUENCE_ROOT_FIELDS[:-1]
    }
    prepared = _prepare_sender_budget_with_nonce_state(six_field_document)
    if isinstance(prepared, dict):
        return prepared

    batch_policy, err = _validate_batch_policy(document["batchPolicy"])
    if err is not None:
        return err
    assert batch_policy is not None

    return (
        *prepared,
        batch_policy["maxBatchCount"],
        batch_policy["maxOperationsPerBatch"],
    )


def _choose_batch_sequence(
    candidates: list[tuple[Any, int, int, int, int]],
    usage: dict[Any, tuple[int, int]],
    nonce_state: dict[Any, dict[int, int]],
    max_gas_per_sender: int,
    max_cost_per_sender: int,
    max_total_gas: int,
    max_cost_wei: int,
    max_batch_count: int,
    max_operations_per_batch: int,
) -> tuple[tuple[tuple[int, ...], ...], int, int]:
    """在多批次约束下求最优入选方案。

    ``candidates`` 为按原下标递增的 ``(sender, key, sequence, gas, cost)``
    列表。方案为若干非空批次：入选项按候选序号（与原下标同序）切成有序的
    连续段，跳过的候选既不占段也不强制另起一批；每批不超过
    ``max_operations_per_batch`` 项、独立满足两条 bundle 限额，且允许在任
    意两个相邻入选项之间主动另起一批。sender 聚合限额（计入 ``usage``）跨
    全部批次累计；锚定 nonce 链按 (sender, key) 跨批次连续递增不重置。

    目标依次：入选数量最大、批次数最小、不同 sender 数最大、总 gas 较小、
    总 cost 较小、扁平候选序号序列字典序较小。返回 ``(批次元组（每批为候
    选序号元组）, 总gas, 总成本)``。

    DP 逐候选扩展，每个方案状态为
    ``(批次数, 末批gas, 末批cost, 末批长度, 不同sender集合, 总gas, 总成本,
    扁平候选序号元组, 批次元组)``。每步从旧状态派生「跳过」与「入选」，入
    选又分为「延续末批」与「另起一批」。剪枝：未来可加入的候选只取决于
    sender 累计用量与各 (sender, key) 的下一允许 sequence（资源键）；同资
    源键下若一状态的批次数、末批 gas/cost/长度、扁平序号都不优于另一状
    态，其当前解与所有未来扩展都不可能更优，可安全删除。入选数与不同
    sender 数、总 gas/cost 由资源键与扁平序号隐含，终选时再展开比较。
    """

    def anchor_of(sender: Any, key: int) -> int:
        sender_state = nonce_state.get(sender)
        if sender_state is not None and key in sender_state:
            return sender_state[key] + 1
        return 0

    def make_resource_key(
        sender_gas: dict[Any, int],
        sender_cost: dict[Any, int],
        next_seq: dict[tuple[Any, int], int],
    ) -> tuple[Any, ...]:
        return (
            tuple(sorted(sender_gas.items())),
            tuple(sorted(sender_cost.items())),
            tuple(sorted(next_seq.items())),
        )

    State = tuple[
        int,
        int,
        int,
        int,
        frozenset[Any],
        int,
        int,
        tuple[int, ...],
        tuple[tuple[int, ...], ...],
    ]
    empty: State = (0, 0, 0, 0, frozenset(), 0, 0, (), ())
    states: dict[tuple[Any, ...], list[State]] = {
        make_resource_key({}, {}, {}): [empty]
    }

    def prune(entries: list[State]) -> list[State]:
        """同资源键下按 (批次数, 末批gas, 末批cost, 末批长度, 扁平序号) 五
        维删除被支配状态。五维全部不更差者支配之（至少一维严格更优由去重
        保证）。
        """
        surviving: list[State] = []
        for state in entries:
            b, bg, bc, blen, _, _, _, flat, _ = state
            dominated = False
            for other in surviving:
                ob, obg, obc, olen, _, _, _, oflat, _ = other
                if (
                    ob <= b
                    and obg <= bg
                    and obc <= bc
                    and olen <= blen
                    and oflat <= flat
                ):
                    dominated = True
                    break
            if dominated:
                continue
            surviving = [
                other
                for other in surviving
                if not (
                    b <= other[0]
                    and bg <= other[1]
                    and bc <= other[2]
                    and blen <= other[3]
                    and flat <= other[7]
                )
            ]
            surviving.append(state)
        return surviving

    for position, (sender, key, sequence, item_gas, item_cost) in enumerate(
        candidates
    ):
        group = (sender, key)
        anchor = anchor_of(sender, key)
        used_gas, used_cost = usage.get(sender, (0, 0))
        next_states: dict[tuple[Any, ...], list[State]] = {}

        def add_state(resource: tuple[Any, ...], state: State) -> None:
            bucket = next_states.setdefault(resource, [])
            if state not in bucket:
                bucket.append(state)

        for resource, entries in states.items():
            gas_items, cost_items, next_items = resource
            sender_gas = dict(gas_items)
            sender_cost = dict(cost_items)
            next_seq = dict(next_items)

            for (
                batch_count,
                last_gas,
                last_cost,
                last_len,
                senders,
                total_gas,
                total_cost,
                flat,
                batches,
            ) in entries:
                # 分支一：跳过本候选，资源与状态不变。
                add_state(resource, (
                    batch_count,
                    last_gas,
                    last_cost,
                    last_len,
                    senders,
                    total_gas,
                    total_cost,
                    flat,
                    batches,
                ))

                # 入选前置：nonce 恰为该 (sender,key) 组的下一期望值，且不
                # 突破计入既有用量的两条 sender 聚合限额。
                if sequence != next_seq.get(group, anchor):
                    continue
                if used_gas + sender_gas.get(sender, 0) + item_gas > max_gas_per_sender:
                    continue
                if (
                    used_cost + sender_cost.get(sender, 0) + item_cost
                    > max_cost_per_sender
                ):
                    continue

                new_sender_gas = dict(sender_gas)
                new_sender_cost = dict(sender_cost)
                new_next_seq = dict(next_seq)
                new_sender_gas[sender] = new_sender_gas.get(sender, 0) + item_gas
                new_sender_cost[sender] = (
                    new_sender_cost.get(sender, 0) + item_cost
                )
                new_next_seq[group] = sequence + 1
                new_resource = make_resource_key(
                    new_sender_gas, new_sender_cost, new_next_seq
                )
                new_senders = senders | {sender}
                new_total_gas = total_gas + item_gas
                new_total_cost = total_cost + item_cost
                new_flat = flat + (position,)

                # 分支二a：延续末批（末批非空、未满且并入后两条限额不超）。
                if (
                    last_len > 0
                    and last_len < max_operations_per_batch
                    and last_gas + item_gas <= max_total_gas
                    and last_cost + item_cost <= max_cost_wei
                ):
                    add_state(new_resource, (
                        batch_count,
                        last_gas + item_gas,
                        last_cost + item_cost,
                        last_len + 1,
                        new_senders,
                        new_total_gas,
                        new_total_cost,
                        new_flat,
                        batches[:-1] + (batches[-1] + (position,),),
                    ))

                # 分支二b：另起一批（首批也走此分支）；受批次数上限约束，
                # 单项独立满足两条 bundle 限额。
                if (
                    batch_count < max_batch_count
                    and item_gas <= max_total_gas
                    and item_cost <= max_cost_wei
                ):
                    add_state(new_resource, (
                        batch_count + 1,
                        item_gas,
                        item_cost,
                        1,
                        new_senders,
                        new_total_gas,
                        new_total_cost,
                        new_flat,
                        batches + ((position,),),
                    ))

        states = {key: prune(entries) for key, entries in next_states.items()}

    # 终选：入选数最大、批次数最小、不同 sender 数最大、总 gas 小、总成本
    # 小、扁平序号字典序小。
    best: State | None = None
    best_key: tuple[Any, ...] | None = None
    for entries in states.values():
        for state in entries:
            (
                batch_count,
                _lg,
                _lc,
                _ll,
                senders,
                total_gas,
                total_cost,
                flat,
                _batches,
            ) = state
            key = (
                -len(flat),
                batch_count,
                -len(senders),
                total_gas,
                total_cost,
                flat,
            )
            if best_key is None or key < best_key:
                best_key = key
                best = state
    assert best is not None
    return best[8], best[5], best[6]


def plan_batch_sequence(document: Any) -> dict[str, Any]:
    """把 approved 请求按原下标规划为不超过策略上限的多个非空批次。

    七字段文档的校验、错误码与 JSON Pointer 路径前六项完全沿用
    ``plan_bundle_sender_budget_with_nonce_state``；``batchPolicy`` 最后校
    验，失败返回 ``E_BATCH_POLICY_*``。成功时返回
    ``{"ok": True, "plan": {...}, "nextSenderUsage": ...,
    "nextSenderNonceState": ...}``，不修改入参，只返回字典且不抛业务异常。
    """
    prepared = _prepare(document)
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
        max_batch_count,
        max_operations_per_batch,
    ) = prepared

    decisions = _evaluate_all(requests, sponsorship_policy)

    skipped: list[dict[str, Any]] = []
    # candidates 按原下标递增压入，平行的 indices 记录原下标；sender 取规范
    # 小写地址，nonce 拆 key/sequence。
    candidates: list[tuple[Any, int, int, int, int]] = []
    indices: list[int] = []
    for index, (request, decision) in enumerate(zip(requests, decisions)):
        if not decision["approved"]:
            skipped.append({"index": index, "reason": decision["reason"]})
            continue
        user_op = validate(request)["normalized"]["userOperation"]
        nonce = int(user_op["nonce"], 16)
        candidates.append((
            user_op["sender"],
            nonce // _NONCE_SEQUENCE_MOD,
            nonce % _NONCE_SEQUENCE_MOD,
            int(decision["totalGas"]),
            int(decision["estimatedCostWei"]),
        ))
        indices.append(index)

    batches_chosen, total_gas, total_cost_wei = _choose_batch_sequence(
        candidates,
        usage,
        nonce_state,
        max_gas_per_sender,
        max_cost_per_sender,
        max_total_gas,
        max_cost_wei,
        max_batch_count,
        max_operations_per_batch,
    )

    chosen_set = {position for batch in batches_chosen for position in batch}
    selected: list[int] = []
    batch_results: list[dict[str, Any]] = []
    for batch_order, batch_positions in enumerate(batches_chosen):
        batch_indexes = [indices[position] for position in batch_positions]
        selected.extend(batch_indexes)
        batch_gas = sum(candidates[p][3] for p in batch_positions)
        batch_cost = sum(candidates[p][4] for p in batch_positions)
        batch_results.append({
            "index": batch_order,
            "selected": batch_indexes,
            "operationCount": str(len(batch_indexes)),
            "totalGas": str(batch_gas),
            "estimatedCostWei": str(batch_cost),
        })
    selected.sort()

    # 已批准但未入选的请求统一记 E_NOT_SELECTED；与未代付项合并后按原下标
    # 升序输出。
    for position in range(len(candidates)):
        if position not in chosen_set:
            skipped.append(
                {"index": indices[position], "reason": REASON_NOT_SELECTED}
            )
    skipped.sort(key=lambda item: item["index"])

    plan = {
        "selected": selected,
        "skipped": skipped,
        "operationCount": str(len(selected)),
        "totalGas": str(total_gas),
        "estimatedCostWei": str(total_cost_wei),
        "batches": batch_results,
    }

    next_usage = _next_sender_usage(
        document["senderUsage"], selected, requests, decisions
    )
    next_state = _next_sender_nonce_state(
        document["senderNonceState"], selected, requests
    )

    return {
        "ok": True,
        "plan": plan,
        "nextSenderUsage": next_usage,
        "nextSenderNonceState": next_state,
    }


def main() -> int:
    raw = sys.stdin.read()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        result = _invalid_json_result(str(exc))
    else:
        result = plan_batch_sequence(document)

    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
