# Paymaster

ERC-4337 账户抽象服务：UserOperation 校验、Gas 代付与打包策略。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

已实现 ERC-4337 v0.7 UserOperation 静态校验，作为后续 Gas 代付与打包的公共入口。本次不验签、不估算成本、不模拟链上执行、不落盘。

## 用法

库接口：

```python
from paymaster.validation import validate

result = validate(request)  # request 为已解析的 JSON 值，只返回字典，不抛业务异常
```

命令行（从标准输入读 JSON，只向标准输出写一个 JSON 文档；成功退出 0，失败退出 1）：

```sh
python -m paymaster.validate < request.json
```

### 请求格式

仅含 `userOperation` 与 `context` 两个键：

- `userOperation`：ERC-4337 v0.7 JSON-RPC 字段。必需：`sender`、`nonce`、`callData`、`callGasLimit`、`verificationGasLimit`、`preVerificationGas`、`maxFeePerGas`、`maxPriorityFeePerGas`、`signature`；可选：`factory`/`factoryData`（必须同有同无）、`paymaster`/`paymasterVerificationGasLimit`/`paymasterPostOpGasLimit`/`paymasterData`（必须同有同无）。
- `context`：`version`（仅接受 `"0.7"`）、`chainId`、`entryPoint`、`baseFeePerGas`。

### 值规则

- 地址：`0x` 前缀 20 字节十六进制，非零地址。
- bytes：`0x` 前缀偶数长度十六进制，可为空（`0x`）。
- quantity：`0x` 前缀、无多余前导零（`0x0` 合法）、不超过 32 字节的十六进制。
- gas limit 与 `chainId` 必须大于 0；`maxPriorityFeePerGas` 不大于 `maxFeePerGas`。

### 结果格式

- 成功：`{"ok": true, "normalized": {...}}`，`normalized` 为规范字段副本（地址与 bytes 转小写，quantity 保持规范十六进制，缺失的可选字段不出现）。
- 失败：`{"ok": false, "error": {"code", "path", "message"}}`，`code` 为稳定错误码，`path` 为 JSON Pointer（RFC 6901）。

错误码：`E_UNKNOWN_FIELD`（未知键）、`E_MISSING_FIELD`（缺字段）、`E_INVALID_FIELD`（值非法）、`E_UNSUPPORTED_VERSION`（版本不支持）、`E_INVALID_JSON`（JSON 根错误）、`E_FIELD_COMBINATION`（字段配对错误）。

## 测试

```sh
python -m unittest discover -s tests -v
```

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现（代付与打包）。
