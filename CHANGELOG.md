# Changelog

版本号见仓库根目录的 `VERSION`。客户端（`kac --version`）和服务端（`kac models` 中的 service version）使用同一个版本号。

## 0.2.1

### 性能

- Runtime 改用 AgentCore Runtime V2（platform version `V2`）：部署时平台启动一次容器并打快照，之后每个任务的 microVM 都从快照恢复，不再拉取 1.7 GB 的镜像。实测每个任务的冷启动从 13.5–17.7 秒降到 2–3 秒，`kac submit` 返回 task_id 也相应变快。

### 需要注意

- `cdk deploy` 和 CloudShell 安装向导的部署步骤多出约 4 分钟（平台准备快照）。
- 每次部署后，最初几个任务的 kiro-cli 首次启动会慢 5–20 秒，之后恢复正常。部署后可以运行几次 `kac models` 预热（不消耗 credits，每次使用新的 session）。
- `RuntimeStarted` 指标改为在每个 microVM 收到第一个请求时上报（原来在进程启动时上报，切到 V2 后只会在打快照时报一次）。

### 修复

- OpenTelemetry 的 trace/span ID 改由操作系统随机源生成。默认生成器使用 Python 的 `random`，其状态会被打进快照，导致从同一快照恢复的 microVM 生成相同的 ID。每个 microVM 收到第一个请求时，也会给 `random` 重新播种。
- `kac models` 列模型的服务端超时从 30 秒放宽到 45 秒，以覆盖新快照下 kiro-cli 首次启动的耗时。

## 0.2.0

### 不兼容的变化

- **任务默认不再走闭环。** 不带 `--loop-mode` 的任务现在是 `none`：kiro-cli 直接运行一次，验收标准由 agent 自查，不做独立验证，报告中标为 "not independently verified"。需要独立验证的任务请加 `--loop-mode process`，或部署时设 `-c defaultLoopMode=process`（CloudShell：`./install.sh --loop-mode process`）。依赖 `kac wait` 退出码判断验收结果的 CI 需要相应调整。
- 直接调用 Runtime（不经过 kac）的程序：错误响应改为 HTTP 200，错误放在响应体的 `error` 字段里，原状态码在 `error.http_status`（AgentCore 会把非 2xx 响应替换为不带信息的 424）。这类程序需要检查 `error` 字段；kac 已适配。

### 新功能

- `--loop-mode none | process | subagent`：单次运行（默认）、每阶段一个 kiro-cli 的闭环、以 kiro sub-agent 执行阶段的闭环。
- 支持 GPT 等 Kiro 提供的模型；`kac models` 列出 API key 可用的模型、费率倍数和默认模型。允许的模型改为通配符列表（默认 `auto, claude-*, gpt-*`），默认模型改为优先级列表（`claude-sonnet-5.5, gpt-5.6-terra, auto`，取第一个 API key 可用的）。原来用 `-c defaultModel=<单个模型>` 部署的方式仍然有效。
- `kac submit --kiro-dir DIR [--agent NAME]`：同步本地的 MCP、skills、steering 和自定义 agent 到任务中；镜像加入 Node.js 24 和 uv，`npx` / `uvx` 方式的 MCP server 可以直接使用。
- `kac --version`；服务端版本记录在任务记录、`result.json` 和 `output.md` 中。
- CloudShell 安装向导：`--default-model`、`--allowed-models`、`--loop-mode`，升级时沿用当前设置并显示版本变化。

### 性能

- 闭环的阶段交接改为消息内联和 JSON 回复，每个阶段少 2–4 轮模型往返：简单任务 45 秒 → 26 秒，小型代码任务 66 秒 → 42 秒，credits 减少约 40%。
- 单次运行（`none`）：简单任务约 5–7 秒，小型代码任务约 13 秒。

### 修复

- kiro-cli 及其 MCP server 不再继承 wrapper 的 OpenTelemetry 环境变量（此前导致 `uvx` 启动的 Python MCP server 崩溃）。

## 0.1.0

- 首个版本：AgentCore Runtime 上的闭环 agent 服务、Query API、Sweeper、kac 命令行、CloudShell 安装向导。
