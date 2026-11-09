# 技术验证结论（任务 1）

验证环境：本地 macOS arm64 上的 kiro-cli 2.35.2，以及 `linux/arm64` 容器（`python:3.12-slim-trixie`，glibc 2.41）中通过官方安装脚本安装的 kiro-cli 2.35.2。容器测试使用 Secrets Manager 中 `prod/kiro/api-key` 的真实 key，以环境变量注入，未打印或落盘。

## 1.1 agent 配置格式与工具限制

- agent 配置放在 `~/.kiro/agents/<name>.json`，用 `--agent <name>` 选择。V3 引擎可以正常加载以下字段：`name`、`description`、`prompt`、`tools`、`resources`、`mcpServers`、`includeMcpJson`。
- V3 中的工具名为 `read`、`write`、`shell`、`grep`、`glob`。
- 工具列表为 `["read", "shell", "grep", "glob"]`（不含 `write`）时，模型可用的工具为 `list_directory, execute_bash, read_file, file_search, grep_search, code, disclose_context`，没有文件写入工具，shell 仍可用。**结论：`loop-verify` 可以去掉写入工具。**
- V3 会跳过“含 `allowedTools`/`toolsSettings` 但没有 `permissions` 块”的旧版配置。我们的配置不写这两个字段，权限由 `--trust-all-tools` 统一放开。

## 1.2 写入指定绝对路径

- 在 `--no-interactive --agent` 模式下，agent 能把指定内容准确写到指定的绝对路径（本地和容器内各验证一次）。
- **目标目录不可写时，kiro-cli 仍然返回退出码 0，`runFinished.status` 也是 `success`**，失败原因只出现在回复文本里。这证明阶段结果必须以“文件存在且校验通过”为准，不能看退出码（与设计 D2 一致）。
- 容器内 `/workspace` 必须在镜像构建时创建，并属于 `agent` 用户。

## 1.3 stream-json 事件

每行一个 JSON 对象，`type` 取值：

| type | 说明 |
|---|---|
| `runStarted` | `data.engine` 为 `v2` 或 `v3` |
| `sessionUpdate` | `data.update.sessionUpdate` 区分子类型：`tool_call` / `tool_call_update`（工具名、参数、状态）、`agent_message_chunk`（回复文本）、`session_info_update`（其中 `_meta.kiro.kind=turn_completion` 时带 `promptTurnSummaries[].usage` credit 用量和 `usedTools`） |
| `runFinished` | `data.status`、`data.stopReason`、`data.finalText` |
| `runError` | `data.stage`、`data.message`；认证失败时 message 含 `Access denied` / `bearer token included in the request is invalid` |

无效 key 时：退出码 1，stdout 最后一行是 `runError`，stderr 中有 `HTTP 403 ... AccessDeniedException`。

**新增可利用的信息**：`turn_completion` 事件带 credit 用量，可以按阶段累计后记入任务记录。

## 1.4 容器安装

- 安装方式：`curl -fsSL https://cli.kiro.dev/install -o install.sh && bash install.sh --version 2.35.2`。脚本会按版本 manifest 校验 sha256。安装到 `~/.local/bin`（`kiro-cli`、`kiro-cli-chat`、`kiro-cli-term`），共约 270 MB。
- **aarch64 的 glibc 版本需要 2.39 或更高**，否则安装脚本会退回 musl 构建。`python:3.12-slim-bookworm` 只有 glibc 2.36，因此**基础镜像改为 `python:3.12-slim-trixie`**。
- **Linux 上默认引擎是 V2**（macOS 本机是 V3）。为了行为一致，并让 agent 配置格式与上面的验证结果一致，命令行**显式加 `--v3`**。
- 数据目录：`~/.local/share/kiro-cli`（首次运行时解压 node 和 KAS），`~/.kiro`（sessions、logs、settings、agents）。
- 本机网络访问 Docker Hub 失败，因此基础镜像统一使用 ECR Public（`public.ecr.aws/docker/library/...`）。部署到 AWS 时也能少一次外部依赖。

## 1.5 只设置 `KIRO_API_KEY`

- 在全新的 HOME 下，只设置 `KIRO_API_KEY` 就能运行 `--list-models` 和完整的 V3 对话，不需要其他初始化。
- 用这个 key 查到的模型列表与本机一致，包含 `claude-sonnet-5.5`、`claude-opus-5.5`、`claude-haiku-5.5`、`auto`。
- **`claude-haiku-5.5` 不支持 effort**：传入 `--effort` 时 kiro-cli 只在 stderr 打印警告 `model 'claude-haiku-5.5' offers no effort levels`，然后忽略该参数继续执行。
- V3 会连接 `app.kiro.dev` 同步远程会话（stderr 中有 `acp.remote_sessions.enabled`），这属于 Kiro 服务自身的行为。

## 1.6 AgentCore 日志组

在真实部署中验证：AgentCore 在 Runtime 创建时就会自动创建 `/aws/bedrock-agentcore/runtimes/<runtime_id>-DEFAULT`。CDK 再创建同名日志组会报 `AlreadyExists`，导致首次部署回滚。最终方案是用 CDK 的 `LogRetention` custom resource 在 Runtime 创建后设置 30 天保留期。部署后已确认保留期为 30 天。

## 1.7 AgentCore Runtime V2（snapshot）冷启动

2026-11-09 在 us-east-1 实测。用与现网相同的镜像（ECR 压缩后 500 MB，解压约 1.7 GB）、执行角色和配置，另建一个 V2 Runtime，与现网 V1 对比。客户端在 us-east-1 的 EC2 上，每次都用新的 session 调用，再在同一 session 上调一次作为热调用对照。

| | V1 | V2 |
|---|---|---|
| 不合法请求（只有平台冷启动） | 13.6–17.5 s | 2.2–2.6 s |
| `models`（读 secret + 启动 kiro-cli），稳定后 | 14.5–18.9 s | 2.5–3.2 s |
| 其中容器内 handler 耗时 | 0.8–1.3 s | 0.7–1.0 s |
| 同一 session 的热调用 | 0.25–0.28 s | 0.25–0.31 s |
| Runtime 创建到 `READY` | 几秒 | 约 3.5 分钟 |

- **新快照的前几次恢复较慢**：快照刚建好时，最初 5 次 `models` 冷调用耗时 23.3、23.8、6.6、21.7、2.8 秒，平台部分始终约 2–3 秒，多出来的时间都花在 kiro-cli 的首次启动上（4.5–20.8 秒）。kiro-cli 的文件不在快照里，需要从镜像按需加载。之后连续 8 次都在 3 秒左右。
- **恢复后第一次读 secret**约 460 ms（V1 为 70–90 ms），是重新建立连接的开销。
- **span ID 在 microVM 之间重复**：同一个 span ID 出现在多个 session 中。原因是 OpenTelemetry 默认的 ID 生成器使用 Python 的 `random`，其状态在快照里。trace ID 由 AgentCore 从上游传入，不受影响；后台线程里没有父 span 的 span 需要在本地生成 trace ID，同一秒内开始的两个任务会撞 ID。解决办法见 `runtime/src/kiro_runtime/snapshot.py`。
- 空闲很久之后，新 session 会不会重新变慢，尚未验证。

## 对设计的调整

1. 基础镜像改为 `public.ecr.aws/docker/library/python:3.12-slim-trixie`。
2. kiro-cli 命令行显式加 `--v3`。
3. 镜像构建时创建 `/workspace` 并交给 `agent` 用户；首次运行解压 KAS 的开销，通过构建时预热消除（具体做法在任务 9.2 中确定）。
4. 认证失败识别：`runError.data.message` 或 stderr 中出现 `AccessDenied` / `bearer token`。
5. 任务记录新增 `credits_used`（各阶段 `turn_completion` 用量之和），`phase_history` 每项也记录本阶段的 credits。
6. effort：模型不支持时 kiro-cli 自动忽略。任务记录中保存请求的 effort，并在 stderr 出现上述警告时，把 `effort_applied` 记为 `false`。
