"""纯内存批次状态结转：在现有规划入口的结果上派生下一批的累计用量与
nonce 状态。

公开入口 ``advance_batch_state(document)``：

- ``document`` 的六个字段（``requests``、``sponsorshipPolicy``、
  ``bundlePolicy``、``senderBudgetPolicy``、``senderUsage``、
  ``senderNonceState``）、校验顺序、错误码、JSON Pointer 路径与结果约定完
  全沿用 ``paymaster.packing.plan_bundle_sender_budget_with_nonce_state``；
  任一请求、策略、senderUsage、senderNonceState 不合法时，只返回与该入口
  相同的错误结果，不额外抛错。
- 校验通过后按现有规则做代付评估与最优打包，``plan`` 中的 ``selected``、
  ``skipped``、``operationCount``、``totalGas``、``estimatedCostWei`` 保
  持原结构与确定值。
- 成功结果顶层键固定为 ``ok``、``plan``、``nextSenderUsage`` 与
  ``nextSenderNonceState``：

  - ``nextSenderUsage`` 保留输入的每个 sender（即使该 sender 本批无入选或
    只有被拒请求）；有入选则按 sender 累加本批入选项的 ``totalGas`` 与
    ``estimatedCostWei``；输入中没有但本批入选的 sender 同结构新增，其他
    不新增。所有数值为无前导零的非负十进制字符串。
  - ``nextSenderNonceState`` 保留输入的每个 sender 与每个 nonceKey 项；有
    入选时把 ``lastSequence`` 更新为该 sender 与 nonceKey 下入选 sequence
    的最大值（有状态从旧 lastSequence 起连续接续，故该值覆盖旧值）；输入中
    没有的 (sender, nonceKey) 同结构新增。各 sender 的 nonceKey 项按数值升
    序，sender 键按规范小写地址升序，同一地址只出现一次。

- 函数不改入参，保持请求校验、代付、最优打包、skipped 原因与空请求行为；
  只返回字典且不抛业务异常。

命令行 ``python -m paymaster.batch_state``：从标准输入读一个 JSON 文档，
只向标准输出写一个 JSON 文档；成功退出 0，失败退出 1；标准输入不是合法
JSON 时输出 ``E_INVALID_JSON`` 结果（path 为空）。不访问节点、数据库或
文件。
"""

from __future__ import annotations

import json
import sys
from typing import Any

from paymaster.packing import (
    _evaluate_all,
    plan_bundle_sender_budget_with_nonce_state,
)
from paymaster.validation import E_INVALID_JSON, validate

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


def _next_sender_usage(
    usage_document: dict[str, Any],
    selected_indexes: list[int],
    requests: list[Any],
    decisions: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """由输入 senderUsage 与本批入选请求生成 nextSenderUsage。

    保留输入的每个 sender 及其两条记录（入参已通过校验，记录必恰好含
    totalGas/estimatedCostWei，且均为规范十进制字符串）；有入选的 sender
    累加本批入选项；未输入但入选的 sender 从 0 起新增。sender 键按规范小
    写地址升序输出。
    """
    # 先按规范小写地址收集输入中每个 sender 的既有值。
    next_usage: dict[str, dict[str, str]] = {}
    for key, entry in usage_document.items():
        next_usage[key.lower()] = {
            "totalGas": entry["totalGas"],
            "estimatedCostWei": entry["estimatedCostWei"],
        }

    # 再按 sender 累加本批入选请求的 totalGas 与 estimatedCostWei。
    for index in selected_indexes:
        # 请求已通过 validate 校验，此处取规范 sender 必然成功。
        sender = validate(requests[index])["normalized"]["userOperation"]["sender"]
        entry = next_usage.setdefault(
            sender, {"totalGas": "0", "estimatedCostWei": "0"}
        )
        entry["totalGas"] = str(
            int(entry["totalGas"]) + int(decisions[index]["totalGas"])
        )
        entry["estimatedCostWei"] = str(
            int(entry["estimatedCostWei"]) + int(decisions[index]["estimatedCostWei"])
        )

    return {sender: next_usage[sender] for sender in sorted(next_usage)}


def _next_sender_nonce_state(
    nonce_state_document: dict[str, Any],
    selected_indexes: list[int],
    requests: list[Any],
) -> dict[str, list[dict[str, str]]]:
    """由输入 senderNonceState 与本批入选请求生成 nextSenderNonceState。

    保留输入的每个 sender 与每个 nonceKey 项；有入选的 (sender, nonceKey)
    把 lastSequence 更新为入选 sequence 的最大值；输入中没有的
    (sender, nonceKey) 同结构新增。各 sender 的 nonceKey 项按数值升序，
    sender 键按规范小写地址升序。
    """
    # sender -> {nonceKey: 旧 lastSequence 的十进制字符串}；入参已通过校验，
    # 键均为规范小写地址，同一 sender 无重复 nonceKey。
    next_state: dict[str, dict[int, str]] = {}
    for sender, entries in nonce_state_document.items():
        keys = next_state.setdefault(sender, {})
        for entry in entries:
            keys[int(entry["nonceKey"])] = entry["lastSequence"]

    # 按 (sender, nonceKey) 求本批入选 sequence 的最大值。
    for index in selected_indexes:
        # 请求已通过 validate 校验，此处取规范 sender/nonce 必然成功。
        user_op = validate(requests[index])["normalized"]["userOperation"]
        nonce = int(user_op["nonce"], 16)
        sender = user_op["sender"]
        nonce_key = nonce // _NONCE_SEQUENCE_MOD
        sequence = nonce % _NONCE_SEQUENCE_MOD
        keys = next_state.setdefault(sender, {})
        if nonce_key not in keys or sequence > int(keys[nonce_key]):
            keys[nonce_key] = str(sequence)

    result: dict[str, list[dict[str, str]]] = {}
    for sender in sorted(next_state):
        keys = next_state[sender]
        result[sender] = [
            {"nonceKey": str(nonce_key), "lastSequence": keys[nonce_key]}
            for nonce_key in sorted(keys)
        ]
    return result


def advance_batch_state(document: Any) -> dict[str, Any]:
    """做一次与现有规划入口完全一致的批量打包，并结转纯内存批次状态。

    文档六个字段的校验顺序、错误码、JSON Pointer 路径与 ``plan`` 的确定值
    与 :func:`paymaster.packing.plan_bundle_sender_budget_with_nonce_state`
    完全相同；任何不合法输入只返回与该入口相同的错误结果。成功时返回
    ``{"ok": True, "plan": ..., "nextSenderUsage": ...,
    "nextSenderNonceState": ...}``，不修改入参，只返回字典且不抛业务异常。
    """
    result = plan_bundle_sender_budget_with_nonce_state(document)
    if not result.get("ok"):
        return result

    plan = result["plan"]
    # 成功时 document 必为通过校验的对象，requests 与各状态字段结构合法。
    requests = document["requests"]
    decisions = _evaluate_all(requests, document["sponsorshipPolicy"])

    next_usage = _next_sender_usage(
        document["senderUsage"], plan["selected"], requests, decisions
    )
    next_state = _next_sender_nonce_state(
        document["senderNonceState"], plan["selected"], requests
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
        result = advance_batch_state(document)

    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
