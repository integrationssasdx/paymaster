# Paymaster

ERC-4337 账户抽象服务：UserOperation 校验、Gas 代付与打包策略。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

已实现 ERC-4337 v0.7 UserOperation 静态校验，并在此基线上提供 Gas 代付决策与批量打包规划。不验签、不模拟链上执行、不访问外部系统、不落盘。

## 用法

库接口：

```python
from paymaster.validation import validate
from paymaster.sponsorship import evaluate_sponsorship
from paymaster.packing import plan_bundle

result = validate(request)  # request 为已解析的 JSON 值，只返回字典，不抛业务异常
decision = evaluate_sponsorship(request, policy)  # 先走 validate，再评估代付
plan = plan_bundle(document)  # 对批量代付文档逐项校验并按顺序规划打包
```

命令行（从标准输入读 JSON，只向标准输出写一个 JSON 文档；结果 `ok` 为 true 退出 0，否则退出 1）：

```sh
python -m paymaster.validate < request.json
python -m paymaster.sponsor < sponsorship.json  # {"request": ..., "policy": ...}
python -m paymaster.pack < bundle.json  # {"requests": [...], "sponsorshipPolicy": ..., "bundlePolicy": ...}
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

### 代付决策

`evaluate_sponsorship(request, policy)` 先用上述规则校验 `request`，失败则原样返回既有错误、不判断代付；随后校验 `policy`：

- `policy` 恰好含 `budgetWei` 与 `maxTotalGas` 两个键，均为规范 quantity 且大于 0。检查顺序：根类型、必需键、未知键、字段值（`budgetWei` 先于 `maxTotalGas`；缺键先于未知键）。
- policy 错误码：`E_POLICY_INVALID_FIELD`（根非对象或值非法）、`E_POLICY_MISSING_FIELD`（缺键）、`E_POLICY_UNKNOWN_FIELD`（未知键）。根 path 为空，字段 path 指向键。

校验通过后：`totalGas` 为 `callGasLimit`、`verificationGasLimit`、`preVerificationGas` 与可选 `paymasterVerificationGasLimit`、`paymasterPostOpGasLimit` 的整数和；`estimatedCostWei` 为 `totalGas` 乘 `maxFeePerGas`。`totalGas` 大于 `maxTotalGas` 时 `approved` 为 false、`reason` 为 `E_GAS_LIMIT`；否则 `estimatedCostWei` 大于 `budgetWei` 时 `approved` 为 false、`reason` 为 `E_BUDGET`；其余 `approved` 为 true、`reason` 为 `OK`。

结果格式：

- 结构失败：`{"ok": false, "error": {"code", "path", "message"}}`。
- 其他结果：`{"ok": true, "decision": {"approved", "reason", "totalGas", "estimatedCostWei"}}`，`totalGas` 与 `estimatedCostWei` 为十进制字符串。

### 批量打包

`plan_bundle(document)` 的根恰好含 `requests`、`sponsorshipPolicy`、`bundlePolicy` 三个键。

- `requests`：现有单笔代付请求组成的数组，逐项先走上述静态校验，再用 `sponsorshipPolicy` 套用单笔代付规则。
- `sponsorshipPolicy`：同单笔 `policy`，恰好含 `budgetWei`、`maxTotalGas`，均为规范 quantity 且大于 0。
- `bundlePolicy`：恰好含 `maxTotalGas`、`maxCostWei`，均为规范 quantity 且大于 0。

结构检查次序：根类型、未知键、缺键、`requests`（逐项，按下标顺序）、策略（先 `sponsorshipPolicy` 后 `bundlePolicy`）；策略内部为根类型、缺键、未知键、字段值。

- 请求结构错误原样保留 `code`、`message`，`path` 前加 `/requests/<下标>`（如 `/requests/0/userOperation/signature`）。
- 策略结构错误使用 `E_POLICY_INVALID_FIELD`、`E_POLICY_MISSING_FIELD`、`E_POLICY_UNKNOWN_FIELD`，根 `path` 为空，字段 `path` 指向该键。
- `requests` 不是数组时为 `E_INVALID_FIELD`，`path` 为 `/requests`。

结构通过后严格按 `requests` 顺序贪心决定：

- `approved` 为 false 的请求跳过并沿用单笔 `reason`（`E_GAS_LIMIT`/`E_BUDGET`）。
- `approved` 项：先把其 `totalGas` 计入累计值，若超过 `bundlePolicy.maxTotalGas` 则不入选、记 `E_BUNDLE_GAS`；否则再把 `estimatedCostWei` 计入，若超过 `bundlePolicy.maxCostWei` 则不入选、记 `E_BUNDLE_BUDGET`（gas 先于成本）；边界相等时入选。一旦某项超限被跳过，后续请求继续按序尝试，不回填、不重排。

结果格式：

- 结构失败：`{"ok": false, "error": {"code", "path", "message"}}`。
- 其他结果（含空选择与全部跳过）：`{"ok": true, "plan": {"selected", "skipped", "operationCount", "totalGas", "estimatedCostWei"}}`；`selected` 为入选项下标的数组，`skipped` 每项含 `index` 与 `reason`，`operationCount`、`totalGas`、`estimatedCostWei` 为十进制字符串。

`python -m paymaster.pack` 从标准输入读取该文档：JSON 解析失败用 `E_INVALID_JSON`（`path` 为空）；任意结构失败退出 1；其余（`ok` 为 true，含空选择）退出 0，标准输出为且仅为一个 JSON 文档。

## 测试

```sh
python -m unittest discover -s tests -v
```

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现（代付与打包）。
