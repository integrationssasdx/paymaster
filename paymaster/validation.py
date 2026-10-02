"""ERC-4337 v0.7 UserOperation 静态校验。

公开入口 ``validate(request)``：

- 入参为已解析的 JSON 值（期望是含 ``userOperation`` 与 ``context`` 的对象）。
- 返回值永远是字典，不抛业务异常：
  - 成功：``{"ok": True, "normalized": {...}}``，``normalized`` 为规范字段副本
    （地址与 bytes 转小写，quantity 保持规范十六进制）。
  - 失败：``{"ok": False, "error": {"code", "path", "message"}}``，``code``
    为稳定错误码，``path`` 为指向出错字段的 JSON Pointer（RFC 6901）。

错误码：

- ``E_INVALID_JSON``        请求根不是 JSON 对象（CLI 中 JSON 解析失败也用此码）
- ``E_UNKNOWN_FIELD``       出现未知键
- ``E_MISSING_FIELD``       缺少必需字段
- ``E_INVALID_FIELD``       字段值非法（类型、格式、取值范围、费用关系）
- ``E_UNSUPPORTED_VERSION`` context.version 不是支持的 "0.7"
- ``E_FIELD_COMBINATION``   字段配对错误（factory/factoryData、paymaster 字段组）

本模块不访问节点、数据库或文件，不验签、不估算成本、不模拟链上执行。
"""

from __future__ import annotations

import re
from typing import Any

SUPPORTED_VERSION = "0.7"

E_UNKNOWN_FIELD = "E_UNKNOWN_FIELD"
E_MISSING_FIELD = "E_MISSING_FIELD"
E_INVALID_FIELD = "E_INVALID_FIELD"
E_UNSUPPORTED_VERSION = "E_UNSUPPORTED_VERSION"
E_INVALID_JSON = "E_INVALID_JSON"
E_FIELD_COMBINATION = "E_FIELD_COMBINATION"

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BYTES_RE = re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")
_QUANTITY_RE = re.compile(r"^0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)$")
_MAX_QUANTITY_HEX_DIGITS = 64  # 32 字节
_ZERO_ADDRESS = "0x" + "0" * 40

_KIND_ADDRESS = "address"
_KIND_BYTES = "bytes"
_KIND_QUANTITY = "quantity"
_KIND_VERSION = "version"

# ERC-4337 v0.7 JSON-RPC UserOperation 字段：(类型, 是否必需)，顺序即规范输出顺序。
_USER_OPERATION_FIELDS: dict[str, tuple[str, bool]] = {
    "sender": (_KIND_ADDRESS, True),
    "nonce": (_KIND_QUANTITY, True),
    "factory": (_KIND_ADDRESS, False),
    "factoryData": (_KIND_BYTES, False),
    "callData": (_KIND_BYTES, True),
    "callGasLimit": (_KIND_QUANTITY, True),
    "verificationGasLimit": (_KIND_QUANTITY, True),
    "preVerificationGas": (_KIND_QUANTITY, True),
    "maxFeePerGas": (_KIND_QUANTITY, True),
    "maxPriorityFeePerGas": (_KIND_QUANTITY, True),
    "paymaster": (_KIND_ADDRESS, False),
    "paymasterVerificationGasLimit": (_KIND_QUANTITY, False),
    "paymasterPostOpGasLimit": (_KIND_QUANTITY, False),
    "paymasterData": (_KIND_BYTES, False),
    "signature": (_KIND_BYTES, True),
}

_CONTEXT_FIELDS: dict[str, tuple[str, bool]] = {
    "version": (_KIND_VERSION, True),
    "chainId": (_KIND_QUANTITY, True),
    "entryPoint": (_KIND_ADDRESS, True),
    "baseFeePerGas": (_KIND_QUANTITY, True),
}

# 必须大于 0 的 quantity 字段（gas limit 与 chainId）。
_POSITIVE_QUANTITY_FIELDS = frozenset({
    "callGasLimit",
    "verificationGasLimit",
    "preVerificationGas",
    "paymasterVerificationGasLimit",
    "paymasterPostOpGasLimit",
    "chainId",
})

# 使用 paymaster 时必须同有同无的字段组（顺序用于确定报错路径）。
_PAYMASTER_FIELD_GROUP = (
    "paymaster",
    "paymasterVerificationGasLimit",
    "paymasterPostOpGasLimit",
    "paymasterData",
)


def _error(code: str, path: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "path": path, "message": message}}


def _pointer(*segments: str) -> str:
    # RFC 6901：~ 转义为 ~0，/ 转义为 ~1。字段名均为固定标识符，转义仅作兜底。
    return "".join("/" + s.replace("~", "~0").replace("/", "~1") for s in segments)


def _validate_value(kind: str, value: Any) -> str | None:
    """校验单个字段值，返回规范化后的值；非法时返回 None。"""
    if not isinstance(value, str):
        return None
    if kind == _KIND_ADDRESS:
        if not _ADDRESS_RE.match(value) or value.lower() == _ZERO_ADDRESS:
            return None
        return value.lower()
    if kind == _KIND_BYTES:
        if not _BYTES_RE.match(value):
            return None
        return value.lower()
    if kind == _KIND_QUANTITY:
        if not _QUANTITY_RE.match(value):
            return None
        if len(value) - 2 > _MAX_QUANTITY_HEX_DIGITS:
            return None
        # 输入已保证无多余前导零，规范形式即小写。
        return value.lower()
    return None  # pragma: no cover - 未知类型属实现错误


def _validate_object(
    obj: Any,
    segment: str,
    fields: dict[str, tuple[str, bool]],
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """校验一个字段对象，返回 (规范化字段字典, None) 或 (None, 错误字典)。"""
    if not isinstance(obj, dict):
        return None, _error(
            E_INVALID_FIELD, _pointer(segment), f"field {segment!r} must be an object"
        )

    for key in obj:
        if key not in fields:
            return None, _error(
                E_UNKNOWN_FIELD, _pointer(segment, key), f"unknown field {key!r}"
            )

    for name, (_, required) in fields.items():
        if required and name not in obj:
            return None, _error(
                E_MISSING_FIELD,
                _pointer(segment, name),
                f"missing required field {name!r}",
            )

    normalized: dict[str, str] = {}
    for name, (kind, _) in fields.items():
        if name not in obj:
            continue
        value = obj[name]
        if kind == _KIND_VERSION:
            if not isinstance(value, str):
                return None, _error(
                    E_INVALID_FIELD,
                    _pointer(segment, name),
                    "version must be a string",
                )
            if value != SUPPORTED_VERSION:
                return None, _error(
                    E_UNSUPPORTED_VERSION,
                    _pointer(segment, name),
                    f"unsupported version {value!r}; "
                    f"only {SUPPORTED_VERSION!r} is supported",
                )
            normalized[name] = value
            continue
        norm = _validate_value(kind, value)
        if norm is None:
            return None, _error(
                E_INVALID_FIELD,
                _pointer(segment, name),
                f"invalid {kind} value for field {name!r}",
            )
        if name in _POSITIVE_QUANTITY_FIELDS and int(norm, 16) == 0:
            return None, _error(
                E_INVALID_FIELD,
                _pointer(segment, name),
                f"field {name!r} must be greater than 0",
            )
        normalized[name] = norm
    return normalized, None


def validate(request: Any) -> dict[str, Any]:
    """静态校验 ERC-4337 v0.7 UserOperation 请求。

    只返回字典，不抛业务异常。不访问节点、数据库或文件。
    """
    if not isinstance(request, dict):
        return _error(E_INVALID_JSON, "", "request root must be a JSON object")

    for key in request:
        if key not in ("userOperation", "context"):
            return _error(E_UNKNOWN_FIELD, _pointer(key), f"unknown field {key!r}")
    for key in ("userOperation", "context"):
        if key not in request:
            return _error(E_MISSING_FIELD, _pointer(key), f"missing required field {key!r}")

    context, err = _validate_object(request["context"], "context", _CONTEXT_FIELDS)
    if err is not None:
        return err

    user_op, err = _validate_object(
        request["userOperation"], "userOperation", _USER_OPERATION_FIELDS
    )
    if err is not None:
        return err
    assert context is not None and user_op is not None

    # 字段配对：factory 与 factoryData 必须同时有或同时无。
    has_factory = "factory" in user_op
    has_factory_data = "factoryData" in user_op
    if has_factory != has_factory_data:
        missing = "factoryData" if has_factory else "factory"
        return _error(
            E_FIELD_COMBINATION,
            _pointer("userOperation", missing),
            "factory and factoryData must be both present or both absent",
        )

    # 字段配对：paymaster 字段组必须同有同无。
    present = [f for f in _PAYMASTER_FIELD_GROUP if f in user_op]
    if present and len(present) != len(_PAYMASTER_FIELD_GROUP):
        missing = next(f for f in _PAYMASTER_FIELD_GROUP if f not in user_op)
        return _error(
            E_FIELD_COMBINATION,
            _pointer("userOperation", missing),
            "paymaster, paymasterVerificationGasLimit, paymasterPostOpGasLimit "
            "and paymasterData must be all present or all absent",
        )

    # 费用关系：maxPriorityFeePerGas 不大于 maxFeePerGas。
    if int(user_op["maxPriorityFeePerGas"], 16) > int(user_op["maxFeePerGas"], 16):
        return _error(
            E_INVALID_FIELD,
            _pointer("userOperation", "maxPriorityFeePerGas"),
            "maxPriorityFeePerGas must not exceed maxFeePerGas",
        )

    return {
        "ok": True,
        "normalized": {"userOperation": user_op, "context": context},
    }
