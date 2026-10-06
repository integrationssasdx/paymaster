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
from paymaster.packing import plan_bundle, plan_bundle_max_count, plan_bundle_sender_fair, plan_bundle_nonce_unique, plan_bundle_nonce_chain, plan_bundle_sender_nonce_chain, plan_bundle_sender_budget, plan_bundle_sender_budget_with_usage, plan_bundle_sender_budget_with_nonce_state
from paymaster.batch_state import advance_batch_state
from paymaster.batch_sequence import plan_batch_sequence

result = validate(request)  # request 为已解析的 JSON 值，只返回字典，不抛业务异常
decision = evaluate_sponsorship(request, policy)  # 先走 validate，再评估代付
plan = plan_bundle(document)  # 逐项评估代付，再按 bundlePolicy 限额入选
plan = plan_bundle_max_count(document)  # 在两条限额下最大化入选数量
plan = plan_bundle_sender_fair(document)  # 按 sender 公平约束的批量打包
plan = plan_bundle_nonce_unique(document)  # 避免同账户 nonce 冲突的批量打包
plan = plan_bundle_nonce_chain(document)  # 按账户 nonce 链约束的批量打包
plan = plan_bundle_sender_nonce_chain(document)  # 合并 sender 配额与 nonce 链的批量打包
plan = plan_bundle_sender_budget(document)  # 按 sender 聚合 Gas 与费用预算的批量打包
plan = plan_bundle_sender_budget_with_usage(document)  # 叠加跨批次 sender 累计用量的 sender 预算打包
plan = plan_bundle_sender_budget_with_nonce_state(document)  # 叠加跨批次 sender 累计用量与 nonce 状态的 sender 预算打包
result = advance_batch_state(document)  # 与上述规划同校验同方案，额外结转 nextSenderUsage 与 nextSenderNonceState
result = plan_batch_sequence(document)  # 增加 batchPolicy，把入选项按序分入多个 bundle 并结转状态
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
python -m paymaster.pack_nonce_chain < bundle.json  # 按账户 nonce 链约束打包
python -m paymaster.pack_sender_nonce_chain < bundle.json  # 合并 sender 配额与 nonce 链打包
python -m paymaster.pack_sender_budget < bundle.json  # 按 sender 聚合 Gas 与费用预算打包
python -m paymaster.pack_sender_budget_with_usage < bundle.json  # 叠加跨批次 sender 累计用量的 sender 预算打包
python -m paymaster.pack_sender_budget_with_nonce_state < bundle.json  # 叠加跨批次 sender 累计用量与 nonce 状态的 sender 预算打包
python -m paymaster.batch_state < bundle.json  # 同校验同方案，并结转纯内存批次状态
python -m paymaster.batch_sequence < sequence.json  # 多批次规划，并结转纯内存批次状态
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

校验通过后：`totalGas` 为 `callGasLimit`、`verificationGasLimit`、`preVerificationGas` 与可选 `paymasterVerificationGasLimit`、`paymasterPostOpGasLimit` 的整数和；`effectiveGasPriceWei` 为 `min(maxFeePerGas, baseFeePerGas + maxPriorityFeePerGas)`，按 context 已校验的基础费率估算；`estimatedCostWei` 为 `totalGas` 乘 `effectiveGasPriceWei`。`totalGas` 大于 `maxTotalGas` 时 `approved` 为 false、`reason` 为 `E_GAS_LIMIT`；否则 `estimatedCostWei` 大于 `budgetWei` 时 `approved` 为 false、`reason` 为 `E_BUDGET`；其余 `approved` 为 true、`reason` 为 `OK`。

结果格式：

- 结构失败：`{"ok": false, "error": {"code", "path", "message"}}`。
- 其他结果：`{"ok": true, "decision": {"approved", "reason", "totalGas", "estimatedCostWei", "effectiveGasPriceWei"}}`，`totalGas`、`estimatedCostWei` 与 `effectiveGasPriceWei` 均为十进制字符串。

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

`plan_bundle_nonce_chain(document)` 的文档结构、校验次序、错误码与结果结构同样沿用 `plan_bundle`（恰好 `requests`、`sponsorshipPolicy`、`bundlePolicy` 三个键）。approved 请求按规范化结果分组：`sender` 取规范小写地址，`nonce` 取规范 quantity 的整数值，除以 2 的 64 次方的商为 key、余数为 sequence。同一 (sender, key) 组的入选项按 requests 下标递增检查时，sequence 必须严格递增且相邻恰好相差 1（同一 sequence 至多一项，且下标顺序与 sequence 顺序一致）；不同组之间互不影响。在该链约束与两条 bundle 限额下求最优子集：先最大化入选数量，之后依次取总 `totalGas` 较小、总 `estimatedCostWei` 较小、`selected` 下标序列字典序较小的唯一方案。每个未入选的 approved 请求只记一个原因：最终组已含同 sender、key、sequence 记 `E_NONCE_CONFLICT`；单独并入后破坏连续链（按下标递增检查 sequence 不再严格递增且相邻相差 1）记 `E_NONCE_GAP`；否则单独并入后先使总 gas 超限记 `E_BUNDLE_GAS`，不先超 gas 但使总成本超限记 `E_BUNDLE_BUDGET`；能并入最终组且不破坏链、不超两条限额的项必须入选。`requests` 为空或最终无入选也是成功结果。命令行为 `python -m paymaster.pack_nonce_chain`，标准输入输出与退出码约定同上。

`plan_bundle_sender_nonce_chain(document)` 的文档结构、校验次序、错误码与结果结构与 `plan_bundle_sender_fair` 相同（恰好 `requests`、`sponsorshipPolicy`、`bundlePolicy`、`fairnessPolicy` 四个键），并叠加 nonce 链约束。approved 请求按规范化结果分组：`sender` 取规范小写地址，`nonce` 取规范 quantity 的整数值，除以 2 的 64 次方的商为 key、余数为 sequence。同一 (sender, key) 组的入选项按 requests 下标递增检查时 sequence 必须严格递增且相邻恰好相差 1；同一 sender 跨所有 key 的入选总数不超过 `fairnessPolicy.maxPerSender`；不同 key 相互独立，只共享 sender 配额与两条 bundle 限额。在上述约束与两条 bundle 限额下求最优子集：先最大化入选数量，再最大化不同 sender 数，之后依次取总 `totalGas` 较小、总 `estimatedCostWei` 较小、`selected` 下标序列字典序较小的唯一方案。每个未入选的 approved 请求按次序只记一个原因：最终组已含同 sender、key、sequence 记 `E_NONCE_CONFLICT`；单独并入后破坏连续链记 `E_NONCE_GAP`；否则其 sender 入选数已达 `maxPerSender` 记 `E_SENDER_QUOTA`；否则单独并入后先使总 gas 超限记 `E_BUNDLE_GAS`，不先超 gas 但使总成本超限记 `E_BUNDLE_BUDGET`；满足 sender 配额、链约束与两条 bundle 限额的项必须入选。`requests` 为空或最终无入选也是成功结果。命令行为 `python -m paymaster.pack_sender_nonce_chain`，标准输入输出与退出码约定同上。

`plan_bundle_sender_budget(document)` 的 `document` 恰好含 `requests`、`sponsorshipPolicy`、`bundlePolicy`、`senderBudgetPolicy` 四个键；`senderBudgetPolicy` 恰好含 `maxTotalGasPerSender` 与 `maxCostWeiPerSender`，均为规范 quantity 且大于 0，分别限制同一 sender（地址按规范小写比较）所有入选项的总 `totalGas` 与总 `estimatedCostWei`。校验次序、错误码与结果结构沿用上述约定，策略按 `sponsorshipPolicy`、`bundlePolicy`、`senderBudgetPolicy` 的顺序校验，新策略的错误 path 指向 `/senderBudgetPolicy` 下的字段。approved 请求在两条 sender 聚合限额与两条 bundle 限额下求最优子集：先最大化入选数量，再最大化不同 sender 数，之后依次取总 `totalGas` 较小、总 `estimatedCostWei` 较小、`selected` 下标序列字典序较小的唯一方案。最终方案确定后，每个未入选的 approved 请求单独并入该组并只记一个原因：先使其 sender 聚合总 gas 超限记 `E_SENDER_GAS`；否则先使其 sender 聚合总成本超限记 `E_SENDER_COST`；否则先使 bundle 总 gas 超限记 `E_BUNDLE_GAS`；否则记 `E_BUNDLE_BUDGET`。`approved` 为 false 的请求不参与选择，按原 index 在 `skipped` 中保留 `E_GAS_LIMIT` 或 `E_BUDGET`。`requests` 为空或最终无入选也是成功结果。命令行为 `python -m paymaster.pack_sender_budget`，标准输入输出与退出码约定同上。

`plan_bundle_sender_budget_with_usage(document)` 在 sender 预算规划上叠加跨批次累计用量：`document` 恰好含 `requests`、`sponsorshipPolicy`、`bundlePolicy`、`senderBudgetPolicy`、`senderUsage` 五个键。`senderUsage` 为对象，键为按规范小写比较的非零地址，值恰好含 `totalGas` 与 `estimatedCostWei`，二者均为无前导零的非负十进制整数字符串（`"0"` 合法），表示该 sender 在本批之前的既有累计用量。requests 与三类策略的结构、错误码与检查顺序同 `plan_bundle_sender_budget`；`senderUsage` 最后校验，错误码为 `E_USAGE_INVALID_FIELD`，path 指向 `/senderUsage` 下的根、键或字段。approved 请求先累计到 `senderUsage`，再受两条 sender 聚合限额与两条 bundle 限额约束求最优子集，优先级与 `plan_bundle_sender_budget` 相同（先最大化入选数量，再最大化不同 sender 数，再依次减小本批总 `totalGas`、总 `estimatedCostWei`，最后取 `selected` 下标序列字典序较小者）。每个未入选的 approved 请求只记一个原因：单独并入后先突破其 sender 的 gas 聚合限额（含既有用量）记 `E_SENDER_GAS`；否则先突破其 sender 的成本聚合限额记 `E_SENDER_COST`；否则先使 bundle 总 gas 超限记 `E_BUNDLE_GAS`；否则记 `E_BUNDLE_BUDGET`。`approved` 为 false 的请求不参与选择也不占额度，按原 index 在 `skipped` 中保留其 reason。空请求、空选择与零累计值均为成功结果。命令行为 `python -m paymaster.pack_sender_budget_with_usage`，标准输入输出与退出码约定同上。

`plan_bundle_sender_budget_with_nonce_state(document)` 在带累计用量的 sender 预算规划上叠加连续批次的 nonce 状态：`document` 恰好含 `requests`、`sponsorshipPolicy`、`bundlePolicy`、`senderBudgetPolicy`、`senderUsage`、`senderNonceState` 六个键。`senderNonceState` 为对象，键为规范（小写）非零 sender 地址，值为数组，数组项恰好含 `nonceKey` 与 `lastSequence`，二者均为无前导零且小于 2 的 64 次方的非负十进制整数字符串，同一 sender 的数组项不得重复 `nonceKey`。requests、三类策略与 `senderUsage` 的结构、错误码与检查顺序同 `plan_bundle_sender_budget_with_usage`；`senderNonceState` 最后校验，错误码为 `E_NONCE_STATE_INVALID_FIELD`，path 指向 `/senderNonceState` 下的根、键、数组项或字段。approved 请求按规范 sender 与 nonce 分组（nonce 整数值除以 2 的 64 次方，商为 key、余数为 sequence）：有状态的组从 `lastSequence` 加 1 开始，无状态的组从 0 开始，同一 (sender, key) 组的入选项按 requests 下标递增时 sequence 必须严格递增且相邻恰好相差 1。方案同时满足计入 `senderUsage` 的两条 sender 聚合限额与两条 bundle 限额，求最优子集的优先级与 `plan_bundle_sender_budget_with_usage` 相同。每个未入选的 approved 请求只记一个原因：sequence 与最终组重复或不高于该组的 `lastSequence` 记 `E_NONCE_CONFLICT`；不能从正确起点连续接续（单独并入后不再是锚定连续链）记 `E_NONCE_GAP`；随后依次记 `E_SENDER_GAS`、`E_SENDER_COST`、`E_BUNDLE_GAS`、`E_BUNDLE_BUDGET`。`approved` 为 false 的请求不参与选择也不占额度，按原 index 在 `skipped` 中保留 `E_GAS_LIMIT` 或 `E_BUDGET`。空请求、空选择与空状态均为成功结果。命令行为 `python -m paymaster.pack_sender_budget_with_nonce_state`，标准输入输出与退出码约定同上。

### 纯内存批次状态结转

`advance_batch_state(document)` 在 `plan_bundle_sender_budget_with_nonce_state` 的基线上做纯内存（不落盘、不访问外部系统）的批次状态结转。`document` 沿用上述六个字段及其校验顺序、错误码、JSON Pointer 路径和结果约定；任一请求、策略、`senderUsage`、`senderNonceState` 不合法，只返回与该规划入口完全相同的错误结果（`{"ok": false, "error": {...}}`）。校验通过后按现有规则代付并最优打包，`plan` 中的 `selected`、`skipped`、`operationCount`、`totalGas`、`estimatedCostWei` 保持原结构与确定值。

成功结果顶层键固定为 `ok`、`plan`、`nextSenderUsage`、`nextSenderNonceState`：

- `nextSenderUsage` 保留输入的每个 sender（即使该 sender 本批无入选或只有被拒请求）；无入选的 sender 值不变，有入选的 sender 按 sender 累加本批入选项的 `totalGas` 与 `estimatedCostWei`；输入中没有但本批入选的 sender 以相同结构（`totalGas`、`estimatedCostWei`）新增，其他 sender 不新增。数值均为无前导零的非负十进制字符串。
- `nextSenderNonceState` 保留输入的每个 sender 与每个 `nonceKey` 项；有入选时把该 sender 与 `nonceKey` 项的 `lastSequence` 更新为该组入选 sequence 的最大值（入选项从旧 `lastSequence` 加 1 连续接续，故最大值即新的链尾），缺项（输入中没有的 sender 或 `nonceKey`）同结构新增。各 sender 的 `nonceKey` 项按数值升序排列；`nextSenderUsage` 与 `nextSenderNonceState` 的 sender 键按规范小写地址升序排列，同一地址只出现一次。

函数不改入参，保持请求校验、代付、最优打包、`skipped` 原因与空请求行为；只返回字典且不抛业务异常。命令行为 `python -m paymaster.batch_state`，从标准输入读 JSON、只向标准输出写一个 JSON 文档，成功退出 0、失败退出 1；标准输入不是合法 JSON 时输出 `E_INVALID_JSON` 结果（path 为空）。

### 多批次规划

`plan_batch_sequence(document)` 在单批状态结转基线上新增多批次规划：`document` 恰好含七个键——上述六个字段（语义、校验顺序、错误码与 JSON Pointer 路径不变）与 `batchPolicy`。`batchPolicy` 最后校验，恰好含 `maxBatchCount` 与 `maxOperationsPerBatch`，均为无前导零、大于 0 且不超过 2 的 64 次方减 1 的规范 quantity；缺键、未知键、非法值分别返回 `E_BATCH_POLICY_MISSING_FIELD`、`E_BATCH_POLICY_UNKNOWN_FIELD`、`E_BATCH_POLICY_INVALID_FIELD`，path 指向 `/batchPolicy` 或其字段。

入选项按原下标顺序划分为不超过 `maxBatchCount` 个非空批次，每批不超过 `maxOperationsPerBatch` 项且满足 `bundlePolicy` 两条限额；入选项受计入 `senderUsage` 的两条 sender 聚合限额约束，nonce 以 `senderNonceState` 为锚点按 (sender, nonceKey) 连续递增、跨批不重置。最优目标依次：入选数量最大、批次数最小、不同 sender 数最大、总 `totalGas` 较小、总 `estimatedCostWei` 较小、`selected` 下标序列字典序最小；批次划分取贪心最长前缀（最小批次数划分中唯一确定者）。

成功结果顶层键固定为 `ok`、`plan`、`nextSenderUsage`、`nextSenderNonceState`：`plan` 顶层沿用 `plan_bundle` 的汇总字段（`selected`、`skipped`、`operationCount`、`totalGas`、`estimatedCostWei`）并增加 `batches`（按序排列，每批含 `selected` 与同样的汇总字段）；`skipped` 对未入选项只记一个原因——未代付沿用 `E_GAS_LIMIT` 或 `E_BUDGET`，已批准未入选记 `E_NOT_SELECTED`。`nextSenderUsage` 与 `nextSenderNonceState` 按 `advance_batch_state` 的规则结转全部入选项。空请求与空选择均为成功结果（`batches` 为空数组）。函数不改入参、不落盘、不访问外部系统，只返回字典且不抛业务异常。命令行为 `python -m paymaster.batch_sequence`，标准输入输出、退出码与 `E_INVALID_JSON` 约定同上。


## 测试

```sh
python -m unittest discover -s tests -v
```

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现（代付与打包）。
