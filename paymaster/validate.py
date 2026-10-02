"""Command-line entry point: ``python -m paymaster.validate``.

Reads exactly one JSON document from standard input, validates it via
:func:`paymaster.validation.validate`, and writes exactly one JSON document
to standard output. No node, database or file system access.

Exit status is 0 on successful validation and 1 on any failure.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from paymaster.validation import E_INVALID_JSON, validate


def main() -> int:
    raw = sys.stdin.read()
    try:
        request: Any = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        result = {
            "ok": False,
            "error": {
                "code": E_INVALID_JSON,
                "message": "standard input must contain one valid JSON document",
                "path": "",
            },
        }
    else:
        result = validate(request)

    json.dump(result, sys.stdout, separators=(",", ":"), ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
