"""命令行入口：``python -m paymaster.pack``。

从标准输入读取一个批量打包 JSON 文档（含 ``requests``、
``sponsorshipPolicy``、``bundlePolicy``），静态校验并规划打包后向标准输出
写出且仅写出一个 JSON 结果文档；结构成功（``ok`` 为 true，含空选择）退出 0，
结构失败退出 1。不访问节点、数据库或文件，不验签、不模拟链上执行。
"""

from __future__ import annotations

import json
import sys

from paymaster.packing import plan_bundle
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
        result = plan_bundle(document)

    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
