"""Paymaster: ERC-4337 账户抽象服务（UserOperation 校验、Gas 代付与打包）。

当前公开入口：`paymaster.validation.validate(request)` 静态校验
ERC-4337 v0.7 UserOperation 请求。后续在此基线上增加代付与打包。
"""

from paymaster.validation import validate

__all__ = ["validate"]
__version__ = "0.1.0"
