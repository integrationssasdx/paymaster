"""Static validation for ERC-4337 v0.7 UserOperation requests.

Public entry point: :func:`validate`. It performs purely static checks:
no node access, no database, no file system, no signature verification,
no gas estimation and no on-chain simulation.

A request has the shape::

    {
      "userOperation": { ... ERC-4337 v0.7 JSON-RPC fields ... },
      "context": {"version", "chainId", "entryPoint", "baseFeePerGas"}
    }

On success the result is ``{"ok": True, "normalized": {...}}`` where
``normalized`` is a canonicalized copy of the request (addresses and byte
strings lower-cased, quantities kept in canonical hex form). On failure it
is ``{"ok": False, "error": {"code", "message", "path"}}`` with a stable
error code and a JSON Pointer (RFC 6901) path.
"""

from __future__ import annotations

from typing import Any

# Fields allowed on userOperation, in canonical output order.
_USER_OP_FIELDS = (
    "sender",
    "nonce",
    "factory",
    "factoryData",
    "callData",
    "callGasLimit",
    "verificationGasLimit",
    "preVerificationGas",
    "maxFeePerGas",
    "maxPriorityFeePerGas",
    "paymaster",
    "paymasterVerificationGasLimit",
    "paymasterPostOpGasLimit",
    "paymasterData",
    "signature",
)

_CONTEXT_FIELDS = ("version", "chainId", "entryPoint", "baseFeePerGas")

# Error codes, in documented priority order.
E_UNKNOWN_FIELD = "E_UNKNOWN_FIELD"
E_MISSING_FIELD = "E_MISSING_FIELD"
E_INVALID_FIELD = "E_INVALID_FIELD"
E_UNSUPPORTED_VERSION = "E_UNSUPPORTED_VERSION"
E_INVALID_JSON = "E_INVALID_JSON"
E_FIELD_COMBINATION = "E_FIELD_COMBINATION"

SUPPORTED_VERSION = "0.7"

_HEX_DIGITS = set("0123456789abcdefABCDEF")


class _ValidationFailure(Exception):
    """Internal control-flow signal carrying the public error payload."""

    def __init__(self, code: str, message: str, path: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path

    def to_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {"code": self.code, "message": self.message, "path": self.path},
        }


def _fail(code: str, message: str, path: str) -> None:
    raise _ValidationFailure(code, message, path)


def _is_hex_string(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("0x")
        and len(value) >= 2
        and all(ch in _HEX_DIGITS for ch in value[2:])
    )


def _check_address(value: Any, path: str, label: str) -> str:
    """Return the normalized address or raise on invalid value."""
    if not isinstance(value, str):
        _fail(E_INVALID_FIELD, f"{label} must be a 20-byte hex string", path)
    if not _is_hex_string(value):
        _fail(E_INVALID_FIELD, f"{label} must be a 0x-prefixed hex string", path)
    if len(value) != 42:
        _fail(E_INVALID_FIELD, f"{label} must be exactly 20 bytes", path)
    normalized = value.lower()
    if int(normalized, 16) == 0:
        _fail(E_INVALID_FIELD, f"{label} must not be the zero address", path)
    return normalized


def _check_bytes(value: Any, path: str, label: str) -> str:
    """Return the normalized byte string; empty (``0x``) is allowed."""
    if not isinstance(value, str):
        _fail(E_INVALID_FIELD, f"{label} must be a hex string", path)
    if not _is_hex_string(value):
        _fail(
            E_INVALID_FIELD,
            f"{label} must be 0x-prefixed hex with an even number of digits",
            path,
        )
    if (len(value) - 2) % 2 != 0:
        _fail(E_INVALID_FIELD, f"{label} must have an even number of hex digits", path)
    return value.lower()


def _check_quantity(
    value: Any,
    path: str,
    label: str,
    *,
    positive: bool = False,
) -> str:
    """Validate a hex quantity: no leading-zero padding, max 32 bytes.

    Returns the canonical form. ``0x0`` is the canonical zero; when
    ``positive`` is true zero is rejected.
    """
    if not isinstance(value, str):
        _fail(E_INVALID_FIELD, f"{label} must be a hex quantity string", path)
    if not _is_hex_string(value):
        _fail(E_INVALID_FIELD, f"{label} must be a 0x-prefixed hex string", path)

    digits = value[2:]
    if digits == "":
        _fail(E_INVALID_FIELD, f"{label} hex body must not be empty", path)
    # No superfluous leading zeros: only "0" may start with 0, and a
    # multi-digit value must not be zero-padded.
    if len(digits) > 1 and digits[0] == "0":
        _fail(
            E_INVALID_FIELD,
            f"{label} must not have leading-zero padding",
            path,
        )
    if len(digits) > 64:
        _fail(E_INVALID_FIELD, f"{label} must not exceed 32 bytes", path)
    if positive and int(digits, 16) == 0:
        _fail(E_INVALID_FIELD, f"{label} must be greater than zero", path)
    # Canonical: lowercase, collapse "-0" style oddities (none possible here).
    return "0x" + digits.lower().lstrip("0") if digits != "0" else "0x0"


def _check_unknown_keys(obj: Any, allowed: tuple[str, ...], base_path: str, label: str) -> None:
    if not isinstance(obj, dict):
        _fail(E_INVALID_FIELD, f"{label} must be a JSON object", base_path)
    allowed_set = set(allowed)
    # Deterministic order for the error path.
    for key in obj:
        if key not in allowed_set:
            _fail(
                E_UNKNOWN_FIELD,
                f"unknown field {key!r}",
                f"{base_path}/{_escape_token(key)}",
            )


def _escape_token(token: str) -> str:
    """Escape a JSON Pointer reference token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def _require(obj: dict[str, Any], field: str, base_path: str) -> Any:
    if field not in obj:
        _fail(
            E_MISSING_FIELD,
            f"missing required field {field!r}",
            f"{base_path}/{field}",
        )
    return obj[field]


def _validate_context(context: Any) -> dict[str, Any]:
    ctx_path = "/context"
    _check_unknown_keys(context, _CONTEXT_FIELDS, ctx_path, "context")

    version = _require(context, "version", ctx_path)
    if not isinstance(version, str):
        _fail(E_INVALID_FIELD, "version must be a string", f"{ctx_path}/version")
    if version != SUPPORTED_VERSION:
        _fail(
            E_UNSUPPORTED_VERSION,
            f"unsupported version {version!r}; only {SUPPORTED_VERSION} is supported",
            f"{ctx_path}/version",
        )

    chain_id = _check_quantity(
        _require(context, "chainId", ctx_path),
        f"{ctx_path}/chainId",
        "chainId",
        positive=True,
    )
    entry_point = _check_address(
        _require(context, "entryPoint", ctx_path),
        f"{ctx_path}/entryPoint",
        "entryPoint",
    )
    base_fee = _check_quantity(
        _require(context, "baseFeePerGas", ctx_path),
        f"{ctx_path}/baseFeePerGas",
        "baseFeePerGas",
    )

    return {
        "version": version,
        "chainId": chain_id,
        "entryPoint": entry_point,
        "baseFeePerGas": base_fee,
    }


def _validate_user_operation(op: Any) -> dict[str, Any]:
    op_path = "/userOperation"
    _check_unknown_keys(op, _USER_OP_FIELDS, op_path, "userOperation")

    # Presence check first, in canonical field order, so a missing field
    # surfaces before value checks on the rest.
    values: dict[str, Any] = {}
    for field in _USER_OP_FIELDS:
        # factory/factoryData/paymaster* conditional presence is handled after
        # the mandatory-field pass; here only the always-required fields count.
        if field in ("factory", "factoryData", "paymaster", "paymasterData",
                     "paymasterVerificationGasLimit", "paymasterPostOpGasLimit"):
            values[field] = op.get(field, None)
            continue
        values[field] = _require(op, field, op_path)

    # Field-pair rules. Using a paymaster means all four paymaster fields
    # must be present together; not using one means none of them may appear.
    has_paymaster = "paymaster" in op
    paymaster_related = (
        "paymaster",
        "paymasterData",
        "paymasterVerificationGasLimit",
        "paymasterPostOpGasLimit",
    )
    present_paymaster_fields = [f for f in paymaster_related if f in op]
    if has_paymaster:
        for field in _USER_OP_FIELDS:
            if field in paymaster_related and field not in op:
                _fail(
                    E_MISSING_FIELD,
                    f"paymaster usage requires field {field!r}",
                    f"{op_path}/{field}",
                )
    elif present_paymaster_fields:
        first = present_paymaster_fields[0]
        _fail(
            E_FIELD_COMBINATION,
            "paymasterData and paymaster gas limits require paymaster and must not appear without it",
            f"{op_path}/{first}",
        )

    has_factory = "factory" in op
    if has_factory != ("factoryData" in op):
        missing_or_extra = "factoryData" if has_factory else "factory"
        if has_factory:
            _fail(
                E_MISSING_FIELD,
                "factory requires factoryData",
                f"{op_path}/{missing_or_extra}",
            )
        _fail(
            E_FIELD_COMBINATION,
            "factoryData requires factory",
            f"{op_path}/{missing_or_extra}",
        )

    # Value validation, in canonical field order.
    normalized: dict[str, Any] = {}

    normalized["sender"] = _check_address(
        values["sender"], f"{op_path}/sender", "sender"
    )
    normalized["nonce"] = _check_quantity(
        values["nonce"], f"{op_path}/nonce", "nonce"
    )

    if has_factory:
        normalized["factory"] = _check_address(
            values["factory"], f"{op_path}/factory", "factory"
        )
        normalized["factoryData"] = _check_bytes(
            values["factoryData"], f"{op_path}/factoryData", "factoryData"
        )

    normalized["callData"] = _check_bytes(
        values["callData"], f"{op_path}/callData", "callData"
    )

    for field in ("callGasLimit", "verificationGasLimit", "preVerificationGas"):
        normalized[field] = _check_quantity(
            values[field], f"{op_path}/{field}", field, positive=True
        )

    max_fee = _check_quantity(
        values["maxFeePerGas"], f"{op_path}/maxFeePerGas", "maxFeePerGas"
    )
    max_priority = _check_quantity(
        values["maxPriorityFeePerGas"],
        f"{op_path}/maxPriorityFeePerGas",
        "maxPriorityFeePerGas",
    )
    normalized["maxFeePerGas"] = max_fee
    normalized["maxPriorityFeePerGas"] = max_priority
    if int(max_priority, 16) > int(max_fee, 16):
        _fail(
            E_INVALID_FIELD,
            "maxPriorityFeePerGas must not exceed maxFeePerGas",
            f"{op_path}/maxPriorityFeePerGas",
        )

    if has_paymaster:
        normalized["paymaster"] = _check_address(
            values["paymaster"], f"{op_path}/paymaster", "paymaster"
        )
        normalized["paymasterVerificationGasLimit"] = _check_quantity(
            values["paymasterVerificationGasLimit"],
            f"{op_path}/paymasterVerificationGasLimit",
            "paymasterVerificationGasLimit",
            positive=True,
        )
        normalized["paymasterPostOpGasLimit"] = _check_quantity(
            values["paymasterPostOpGasLimit"],
            f"{op_path}/paymasterPostOpGasLimit",
            "paymasterPostOpGasLimit",
            positive=True
        )
        normalized["paymasterData"] = _check_bytes(
            values["paymasterData"], f"{op_path}/paymasterData", "paymasterData"
        )

    normalized["signature"] = _check_bytes(
        values["signature"], f"{op_path}/signature", "signature"
    )

    return normalized


def validate(request: Any) -> dict[str, Any]:
    """Validate a UserOperation request statically.

    Always returns a result dict; never raises business exceptions::

        {"ok": True, "normalized": {"userOperation": ..., "context": ...}}
        {"ok": False, "error": {"code", "message", "path"}}
    """
    root_path = ""
    if not isinstance(request, dict):
        return {
            "ok": False,
            "error": {
                "code": E_INVALID_FIELD,
                "message": "request must be a JSON object",
                "path": root_path,
            },
        }

    try:
        _check_unknown_keys(request, ("userOperation", "context"), root_path, "request")

        user_operation = _require(request, "userOperation", root_path)
        context = _require(request, "context", root_path)

        normalized_context = _validate_context(context)
        normalized_op = _validate_user_operation(user_operation)
    except _ValidationFailure as exc:
        return exc.to_result()

    return {
        "ok": True,
        "normalized": {
            "userOperation": normalized_op,
            "context": normalized_context,
        },
    }
