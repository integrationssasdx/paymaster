# Paymaster

ERC-4337 账户抽象服务：UserOperation 校验、Gas 代付与打包策略。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

- UserOperation 静态校验（ERC-4337 v0.7）：已实现。
- Gas 代付与打包策略：待增量实现。

## UserOperation 校验

纯静态校验：不访问节点、数据库或文件系统；不验签、不估算成本、不模拟链上执行。

Python API：

```python
from paymaster.validation import validate

result = validate(request)
# 成功：{"ok": True, "normalized": {"userOperation": ..., "context": ...}}
# 失败：{"ok": False, "error": {"code", "message", "path"}}
```

命令行（从标准输入读取一个 JSON 文档，向标准输出写一个 JSON 文档；成功退出 0，失败退出 1）：

```sh
python -m paymaster.validate < request.json
```

请求结构：`userOperation` 采用 ERC-4337 v0.7 JSON-RPC 字段；`context` 含
`version`（仅接受 `0.7`）、`chainId`、`entryPoint`、`baseFeePerGas`。
成功结果中的 `normalized` 为规范副本：地址与 bytes 转小写，quantity 保持规范十六进制。

错误码（按优先级）：`E_UNKNOWN_FIELD`、`E_MISSING_FIELD`、`E_INVALID_FIELD`、
`E_UNSUPPORTED_VERSION`、`E_INVALID_JSON`、`E_FIELD_COMBINATION`；错误 `path` 为
RFC 6901 JSON Pointer。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
