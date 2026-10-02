"""命令行入口：``python -m paymaster.validate``。

从标准输入读取一个 JSON 请求文档，静态校验后向标准输出写出且仅写出
一个 JSON 结果文档；校验成功退出 0，失败退出 1。
不访问节点、数据库或文件。
"""

from __future__ import annotations

import json
import sys

from paymaster.validation import E_INVALID_JSON, validate


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
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        result = _invalid_json_result(str(exc))
    else:
        result = validate(request)

    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
