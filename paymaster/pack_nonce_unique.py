"""命令行入口：``python -m paymaster.pack_nonce_unique``。

从标准输入读取一个 JSON 文档 ``{"requests": [...], "sponsorshipPolicy": ...,
"bundlePolicy": ...}``，做避免同账户 nonce 冲突的批量打包规划后向标准输出
写出且仅写出一个 JSON 结果文档；结果 ``ok`` 为 true 退出 0，否则退出 1。不
访问节点、数据库或文件。
"""

from __future__ import annotations

import json
import sys

from paymaster.packing import plan_bundle_nonce_unique
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
        result = plan_bundle_nonce_unique(document)

    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
