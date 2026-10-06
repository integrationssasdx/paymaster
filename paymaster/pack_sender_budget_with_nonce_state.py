"""命令行入口：``python -m paymaster.pack_sender_budget_with_nonce_state``。

从标准输入读取一个 JSON 文档 ``{"requests": [...], "sponsorshipPolicy": ...,
"bundlePolicy": ..., "senderBudgetPolicy": ..., "senderUsage": ...,
"senderNonceState": ...}``，在跨批次 sender 累计用量与 nonce 状态基线上做按
sender 聚合 Gas 与费用预算的批量打包规划后，向标准输出写出且仅写出一个 JSON
结果文档；结果 ``ok`` 为 true 退出 0，否则退出 1。不访问节点、数据库或文件。
"""

from __future__ import annotations

import json
import sys

from paymaster.packing import plan_bundle_sender_budget_with_nonce_state
from paymaster.validation import E_INVALID_JSON


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
        result = plan_bundle_sender_budget_with_nonce_state(document)

    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
