"""纯内存批次状态结转：``advance_batch_state(document)`` 与命令行入口
``python -m paymaster.batch_state``。

在 ``paymaster.packing.plan_bundle_sender_budget_with_nonce_state`` 的校验、
代付与最优打包基线上，把本批入选结果结转为下一批的 ``senderUsage`` 与
``senderNonceState`` 基线。文档结构、校验次序、错误码、JSON Pointer 路径与
失败结果同规划入口完全一致：任一请求、策略、``senderUsage`` 或
``senderNonceState`` 不合法时，返回与规划入口相同的错误字典。

成功结果恰好含四个顶层键：

- ``ok``：true；
- ``plan``：规划入口的 ``plan``（``selected``、``skipped``、
  ``operationCount``、``totalGas``、``estimatedCostWei``），结构与取值不变；
- ``nextSenderUsage``：对象，键为规范小写 sender 地址（升序，同一地址只出
  现一次），值恰好含 ``totalGas`` 与 ``estimatedCostWei``。输入
  ``senderUsage`` 的每个 sender 都保留：无入选不变，有入选按 sender 累加本
  批入选项的 ``totalGas`` 与 ``estimatedCostWei``；未在输入中但有入选的
  sender 新增同结构记录，其他 sender 不新增。数值均为无前导零的非负十进制
  字符串。
- ``nextSenderNonceState``：对象，键为规范小写 sender 地址（升序），值为
  数组，数组项恰好含 ``nonceKey`` 与 ``lastSequence``（无前导零的非负十
  进制字符串），同一 sender 的项按 ``nonceKey`` 数值升序。输入
  ``senderNonceState`` 的每个 sender 与 nonceKey 项都保留；某 sender 有入
  选时，把对应 nonceKey 的 ``lastSequence`` 更新为该 sender 与该 nonceKey
  下入选 sequence 的最大值，缺项（sender 或 nonceKey）新增。

函数不修改入参，只返回字典，不抛业务异常。命令行从标准输入读 JSON，向标准
输出写出且仅写出一个 JSON 结果文档；结果 ``ok`` 为 true 退出 0，否则退出
1；JSON 解析失败返回 ``E_INVALID_JSON`` 结果。不访问节点、数据库或文件。
"""

from __future__ import annotations

import json
import sys
from typing import Any

from paymaster.packing import (
    _NONCE_SEQUENCE_MOD,
    plan_bundle_sender_budget_with_nonce_state,
)
from paymaster.sponsorship import evaluate_sponsorship
from paymaster.validation import E_INVALID_JSON, validate


def advance_batch_state(document: Any) -> dict[str, Any]:
    """按本批入选结果结转 sender 累计用量与 nonce 状态。

    文档结构、校验次序、错误码与失败结果同
    ``plan_bundle_sender_budget_with_nonce_state``；成功时在 ``plan`` 之外附
    加 ``nextSenderUsage`` 与 ``nextSenderNonceState``。不修改入参，只返回字
    典，不抛业务异常。
    """
    result = plan_bundle_sender_budget_with_nonce_state(document)
    if not result.get("ok"):
        return result
    plan = result["plan"]

    # 本批入选项按规范 sender 聚合 gas/cost，按 (sender, key) 记录最大
    # sequence。请求与策略均已通过校验，此处取规范化结果与代付决策必然成功。
    requests = document["requests"]
    sponsorship_policy = document["sponsorshipPolicy"]
    batch_gas: dict[str, int] = {}
    batch_cost: dict[str, int] = {}
    batch_sequences: dict[str, dict[int, int]] = {}
    for index in plan["selected"]:
        request = requests[index]
        user_op = validate(request)["normalized"]["userOperation"]
        sender = user_op["sender"]
        nonce = int(user_op["nonce"], 16)
        key = nonce // _NONCE_SEQUENCE_MOD
        sequence = nonce % _NONCE_SEQUENCE_MOD
        decision = evaluate_sponsorship(request, sponsorship_policy)["decision"]
        batch_gas[sender] = batch_gas.get(sender, 0) + int(decision["totalGas"])
        batch_cost[sender] = batch_cost.get(sender, 0) + int(
            decision["estimatedCostWei"]
        )
        sequences = batch_sequences.setdefault(sender, {})
        if sequence > sequences.get(key, -1):
            sequences[key] = sequence

    # nextSenderUsage：保留输入的每个 sender（键规范化为小写），有入选的累加
    # 本批用量；未输入但有入选的 sender 新增，其他不新增。
    usage_totals: dict[str, tuple[int, int]] = {}
    for address, entry in document["senderUsage"].items():
        usage_totals[address.lower()] = (
            int(entry["totalGas"]),
            int(entry["estimatedCostWei"]),
        )
    for sender, added_gas in batch_gas.items():
        gas, cost = usage_totals.get(sender, (0, 0))
        usage_totals[sender] = (gas + added_gas, cost + batch_cost[sender])
    next_sender_usage = {
        sender: {
            "totalGas": str(gas),
            "estimatedCostWei": str(cost),
        }
        for sender, (gas, cost) in sorted(usage_totals.items())
    }

    # nextSenderNonceState：保留输入的每个 sender 与 nonceKey 项，有入选的把
    # lastSequence 更新为该 (sender, nonceKey) 下入选 sequence 的最大值，缺
    # 项新增；同一 sender 的项按 nonceKey 数值升序。
    nonce_totals: dict[str, dict[int, int]] = {}
    for address, entries in document["senderNonceState"].items():
        nonce_totals[address] = {
            int(entry["nonceKey"]): int(entry["lastSequence"])
            for entry in entries
        }
    for sender, sequences in batch_sequences.items():
        states = nonce_totals.setdefault(sender, {})
        for key, sequence in sequences.items():
            if sequence > states.get(key, -1):
                states[key] = sequence
    next_sender_nonce_state = {
        sender: [
            {"nonceKey": str(key), "lastSequence": str(states[key])}
            for key in sorted(states)
        ]
        for sender, states in sorted(nonce_totals.items())
    }

    return {
        "ok": True,
        "plan": plan,
        "nextSenderUsage": next_sender_usage,
        "nextSenderNonceState": next_sender_nonce_state,
    }


def _invalid_json_result(detail: str) -> dict:
    return {
        "ok": False,
        "error": {
            "code": E_INVALID_JSON,
            "path": "",
            "message": f"request is not a valid JSON document: {detail}",
        },
    }


def main() -> int:
    raw = sys.stdin.read()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        result = _invalid_json_result(str(exc))
    else:
        result = advance_batch_state(document)

    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
