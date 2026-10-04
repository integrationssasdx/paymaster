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
from paymaster.packing import plan_bundle, plan_bundle_max_count, plan_bundle_sender_fair, plan_bundle_nonce_unique, plan_bundle_nonce_chain

result = validate(request)  # request 为已解析的 JSON 值，只返回字典，不抛业务异常
decision = evaluate_sponsorship(request, policy)  # 先走 validate，再评估代付
plan = plan_bundle(document)  # 逐项评估代付，再按 bundlePolicy 限额入选
plan = plan_bundle_max_count(document)  # 在两条限额下最大化入选数量
plan = plan_bundle_sender_fair(document)  # 按 sender 公平约束的批量打包
plan = plan_bundle_nonce_unique(document)  # 避免同账户 nonce 冲突的批量打包
plan = plan_bundle_nonce_chain(document)  # 按账户 nonce 连续链约束的批量打包
```

命令行（从标准输入读 JSON，只向标准输出写一个 JSON 文档；结果 `ok` 为 true 退出 0，否则退出 1）：

```sh
python -m paymaster.validate < request.json
python -m paymaster.sponsor < sponsorship.json  # {"request": ..., "policy": ...}
python -m paymaster.pack < bundle.json  # {"requests": [...], "sponsorshipPolicy": ..., "bundlePolicy": ...}
python -m paymaster.pack_cost_first < bundle.json  # 成本优先打包
python -m paymaster.pack_max_count < bundle.json  # 入选数量最大化打包
python -m paymaster.pack_sender_fair < bundle.json  # 按 sender 公平约束打包
python -m paymaster.pack_nonce_unique < bundle.json  # 避免同账户 nonce 冲突打包
python -m paymaster.pack_nonce_chain < bundle.json  # 按账户 nonce 连续链约束打包
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

`plan_bundle(document)` 的 `document` 恰好含 `requests`、`sponsorshipPolicy`、`bundlePolicy` 三个键。`requests` 为上述请求的数组；`sponsorshipPolicy` 规则同单笔代付策略；`bundlePolicy` 恰好含 `maxTotalGas` 与 `maxCostWei`，均为规范 quantity 且大于 0。

校验次序：根类型、未知键、缺键、requests（逐项）、策略（`sponsorshipPolicy` 先于 `bundlePolicy`；策略内部为根类型、缺键、未知键、字段值）。请求错误沿用 `validate` 的 code 与 message，path 前加 `/requests/<下标>`；策略错误码同单笔（`E_POLICY_*`），path 指向 `/sponsorshipPolicy` 或 `/bundlePolicy` 下的字段。

校验通过后按 requests 顺序逐项决定：单笔 `approved` 为 false 的请求进入 `skipped` 并沿用其 reason；approved 项在累计 `totalGas` 与 `estimatedCostWei` 分别不超过 `bundlePolicy.maxTotalGas` 与 `bundlePolicy.maxCostWei` 时入选，否则进入 `skipped`——gas 先查，超限记 `E_BUNDLE_GAS`；成本后查，超限记 `E_BUNDLE_BUDGET`。空选择也是成功结果。

结果格式：

- 结构失败：`{"ok": false, "error": {"code", "path", "message"}}`。
- 其他结果：`{"ok": true, "plan": {"selected", "skipped", "operationCount", "totalGas", "estimatedCostWei"}}`，`selected` 为下标数组，`skipped` 元素含 `index` 与 `reason`，`operationCount`、`totalGas`、`estimatedCostWei` 为十进制字符串。

`plan_bundle_cost_first(document)` 使用相同的文档结构、校验次序、错误码与结果结构，但 approved 项按 `estimatedCostWei` 升序、`totalGas` 升序、原下标升序排列后逐项贪心入选，被拒项不阻断后续较小请求。

`plan_bundle_max_count(document)` 同样沿用文档结构、校验次序、错误码与结果结构，但在 approved 请求上求满足两条 bundle 限额的**入选数量最大**子集（二维 0/1 背包）。数量并列时依次取总 `totalGas` 较小、总 `estimatedCostWei` 较小、`selected` 下标序列字典序较小的唯一方案。最终方案确定后，每个未入选的 approved 请求单独并入该组：先使总 gas 超限记 `E_BUNDLE_GAS`，否则（使总成本超限）记 `E_BUNDLE_BUDGET`；不存在两条限额都不超却未入选的请求。`approved` 为 false 的请求不参与选择，按原 index 在 `skipped` 中保留 `E_GAS_LIMIT` 或 `E_BUDGET`。`requests` 为空或最终无入选也是成功结果。命令行为 `python -m paymaster.pack_max_count`，标准输入输出与退出码约定同上。

`plan_bundle_sender_fair(document)` 的 `document` 恰好含 `requests`、`sponsorshipPolicy`、`bundlePolicy`、`fairnessPolicy` 四个键；`fairnessPolicy` 恰好含 `maxPerSender`，为大于 0 的规范 quantity，限制同一 sender 的入选数量（地址按规范小写比较）。校验次序、错误码与结果结构沿用上述约定，三个策略按 `sponsorshipPolicy`、`bundlePolicy`、`fairnessPolicy` 的顺序校验。approved 请求在 sender 配额与两条 bundle 限额下求最优子集：先最大化入选数量，再最大化不同 sender 数，之后依次取总 `totalGas` 较小、总 `estimatedCostWei` 较小、`selected` 下标序列字典序较小的唯一方案。每个未入选的 approved 请求只记一个原因：其 sender 入选数已达 `maxPerSender` 记 `E_SENDER_QUOTA`；否则单独并入后先使总 gas 超限记 `E_BUNDLE_GAS`，不先超 gas 但使总成本超限记 `E_BUNDLE_BUDGET`。`requests` 为空、最终无入选或同一 sender 只部分入选都是成功结果。命令行为 `python -m paymaster.pack_sender_fair`，标准输入输出与退出码约定同上。

`plan_bundle_nonce_unique(document)` 的文档结构、校验次序、错误码与结果结构与 `plan_bundle` 相同（恰好 `requests`、`sponsorshipPolicy`、`bundlePolicy` 三个键，无新增字段或策略）。approved 请求按规范化结果判冲突：`sender` 取规范小写地址，`nonce` 取规范 quantity（大小写或前导零差异视为同一键）；同一 sender 的同一 nonce 至多入选一项，同一 sender 不同 nonce 或不同 sender 同 nonce 均不受限制。在该约束与两条 bundle 限额下求最优子集：先最大化入选数量，之后依次取总 `totalGas` 较小、总 `estimatedCostWei` 较小、`selected` 下标序列字典序较小的唯一方案。每个未入选的 approved 请求只记一个原因：最终组已含同 sender 同 nonce 记 `E_NONCE_CONFLICT`；否则单独并入后先使总 gas 超限记 `E_BUNDLE_GAS`，不先超 gas 但使总成本超限记 `E_BUNDLE_BUDGET`；能在无冲突前提下满足两条限额的项必须入选。`requests` 为空或最终无入选也是成功结果。命令行为 `python -m paymaster.pack_nonce_unique`，标准输入输出与退出码约定同上。

`plan_bundle_nonce_chain(document)` 的文档结构、校验次序、错误码与结果结构与 `plan_bundle` 相同（不新增字段或策略，也不改变旧的 `plan_bundle_nonce_unique` 去重与既有策略排序）。approved 请求的 `nonce` 按 64 位拆分：`key = nonce // 2**64`（商）、`sequence = nonce % 2**64`（余数），连同规范 `sender`（小写地址）分组。按 requests 下标递增检查同一 (sender, key) 的入选项：其 sequence 必须严格递增且相邻相差 1、同一 sequence 至多一项——即入选序列恰为 `0, 1, ..., L-1` 的连续链（允许跨 key 与跨 sender 穿插，下标递增对全组与每个分组都成立）。在该链约束与两条 bundle 限额下求最优子集：先最大化入选数量，之后依次取总 `totalGas` 较小、总 `estimatedCostWei` 较小、`selected` 下标序列字典序较小的唯一方案。每个未入选的 approved 请求只记一个原因：其 sequence 已被最终组占用（该组入选 sequence 恰为 `0..L-1`，即 `sequence < L`）记 `E_NONCE_CONFLICT`；否则并入最终组会破坏连续链记 `E_NONCE_GAP`（本版本唯一新增错误码）；其余单独并入后先使总 gas 超限记 `E_BUNDLE_GAS`，不先超 gas 但使总成本超限记 `E_BUNDLE_BUDGET`。`requests` 为空或最终无入选也是成功结果。命令行为 `python -m paymaster.pack_nonce_chain`，标准输入输出与退出码约定同上。

## 测试

```sh
python -m unittest discover -s tests -v
```

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现（代付与打包）。
