# kac 使用说明

`kac` 是本服务的本地命令行客户端，用来提交任务、查询状态和下载结果。它只需要 Cognito token，不需要 AWS 凭证。唯一的例外是 `kac configure --from-stack`，它要读取 CloudFormation 输出，所以需要 AWS 凭证。

## 安装

```bash
cd cli
uv tool install --python 3.12 '.[aws]'      # 或：pipx install '.[aws]'
```

通过 CloudShell 安装向导部署时，向导已经在 CloudShell 中装好并配置了 kac。

`[aws]` 会额外安装 boto3，只有 `configure --from-stack` 用得到它。

## 配置

```bash
kac configure --from-stack KiroAgentCoreStack
```

配置写入 `~/.config/kac/config.toml`。每一项都可以用环境变量覆盖，优先级高于配置文件：

| 配置项 | 环境变量 |
|---|---|
| `runtime_arn` | `KAC_RUNTIME_ARN` |
| `query_api_url` | `KAC_QUERY_API_URL` |
| `cognito_domain` | `KAC_COGNITO_DOMAIN` |
| `client_id` | `KAC_CLIENT_ID` |
| `scope` | `KAC_SCOPE`（默认 `kiro-agent/invoke`） |
| `region` | `KAC_REGION`（默认 `us-east-1`） |

`KAC_CONFIG_DIR` 可以改变配置目录。

## 登录

```bash
kac login               # 打开浏览器登录，本地 8765 端口接收回调
kac login --no-browser  # 只打印 URL，自己复制到浏览器打开
kac login --paste       # 远程终端：登录后把浏览器地址栏的回调地址粘贴回来
kac logout
```

在 AWS CloudShell 或 SSH 会话中，`kac login` 默认使用 `--paste` 模式。登录完成后浏览器会显示“无法访问此网站”，这是正常的，把地址栏里以 `http://localhost:8765/callback?code=` 开头的完整网址粘贴到终端即可。

**在本机创建测试用户**（不发邮件，直接设永久密码）：

```bash
POOL=$(aws cloudformation describe-stacks --region us-east-1 --stack-name KiroAgentCoreStack \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
aws cognito-idp admin-create-user --region us-east-1 --user-pool-id "$POOL" --username tester \
  --message-action SUPPRESS --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true
read -s -p "密码: " PW; echo
aws cognito-idp admin-set-user-password --region us-east-1 --user-pool-id "$POOL" \
  --username tester --password "$PW" --permanent; unset PW
```

token 保存在 `~/.config/kac/tokens.json`，文件权限为 0600。access token 过期后，kac 会自动用 refresh token 刷新，refresh token 有效期 30 天。

**CI 场景**：设置 `KAC_CLIENT_ID`（取 `MachineClientId`）和 `KAC_CLIENT_SECRET` 后，kac 走 client_credentials 流程，不读写本地 token 文件。

## 提交任务

```bash
kac submit "修复登录页的表单校验 bug，并提交 PR" \
  --context "仓库: https://github.com/org/web, 分支: main" \
  --ac "npm test 全部通过" \
  --ac "新增覆盖该 bug 的单元测试" \
  --model gpt-5.6-sol --effort high --max-iterations 3 --timeout 7200
```

| 参数 | 说明 |
|---|---|
| `PROMPT` | 任务描述；传 `-` 时从 stdin 读取 |
| `--context` / `--context-file` | 附加上下文，例如仓库、分支、背景说明 |
| `--ac`（可重复）/ `--ac-file`（每行一条） | 验收标准。会原样保留，编号为 `U-1`、`U-2`……，是验证阶段的判定依据。不提供时由 agent 自行推导 |
| `--model` | 模型 ID，用 `kac models` 查看可选值。不指定时用服务端默认：按优先级列表取第一个 API key 可用的模型（默认 `claude-sonnet-5.5` → `gpt-5.6-terra` → `auto`） |
| `--effort` | low / medium / high / xhigh / max |
| `--max-iterations` | 最多几轮“执行 + 验证”，默认 3 |
| `--timeout` | 整个任务的超时秒数，覆盖所有阶段 |
| `--kiro-dir` | 同步这个 `.kiro` 目录里的 MCP、skills、steering 和 agent，见[同步 Kiro 配置](#同步-kiro-配置mcpskillssteeringagent)。也可以用环境变量 `KAC_KIRO_DIR` |
| `--agent` | 把 `--kiro-dir` 里的这个 agent 合并进每个阶段 |

命令输出 task_id。每次提交都使用一个新的 Session，不支持在已有 Session 中追问。

## 同步 Kiro 配置（MCP、skills、steering、agent）

提交任务时可以把本地的 kiro 配置一起带上，任务在云端使用和本机相同的 MCP server、skills、steering 和自定义 agent：

```bash
kac submit "排查 Lambda 冷启动慢的原因" --kiro-dir ~/.kiro
kac submit "评审这个 PR" --kiro-dir ./my-team/.kiro --agent code-reviewer
export KAC_KIRO_DIR=~/.kiro   # 每次都带上时，可以用环境变量代替 --kiro-dir
```

**同步哪些内容**：只打包下面这些，目录里的其他内容（会话、日志、`settings/cli.json` 等）都留在本地。

| 本地路径 | 云端效果 |
|---|---|
| `settings/mcp.json` | 4 个阶段都能调用这些 MCP server（`disabled: true` 的除外） |
| `skills/<name>/SKILL.md`（以及同目录下的 scripts、references 等） | 4 个阶段都可以按需加载这些 skill |
| `steering/*.md` | 作为规则加载到每个阶段的上下文 |
| `agents/*.json`、`agents/*.md` | 用 `--agent NAME` 选中的 agent 会合并进每个阶段（见下文）。`.md` 文件只有带 YAML front matter 时才算 agent，其他 `.md`（例如被 `"prompt": "file://./x.md"` 引用的 prompt 文件）作为普通文件同步 |

**`--agent` 的合并方式**：闭环的 4 个阶段必须使用服务自己的 agent，才能保证流程和交接文件格式，所以不会直接换成你的 agent，而是合并它的部分配置：

- `prompt`：追加在闭环规则之后，作为补充指令。闭环规则优先，不能被它覆盖。
- `mcpServers`：合并进来（`disabled: true` 的除外）。
- `tools`：只接受指向 bundle 中声明过的 MCP server 的选择器（`@server`、`@server/tool`），`@builtin`、`*`、内置工具名都会被忽略。内置工具由闭环决定，验证阶段始终没有写文件的内置工具。
- `resources`：只接受 `~/.kiro/...` 和工作区内的相对路径（`.kiro/...` 会自动指向同步过去的 `~/.kiro/...`）。绝对路径（例如 `/proc`、`~/.aws`）、包含 `..` 的路径和其他协议会被忽略并给出警告。
- `model`、`hooks`、`permissions`、`allowedTools`、`toolsSettings` 会被忽略。模型由 `--model` 和服务端默认值决定。

注意：MCP server 提供的工具在 4 个阶段都可以用，包括验证阶段。如果某个 MCP server 能修改文件或外部系统（例如 filesystem、git、Jira 写操作），验证阶段在技术上也能调用它，只是闭环规则要求验证阶段不修改任何东西。

**MCP server 的运行环境**：容器里有 `npx`、`node`（Node.js 24）、`uvx`、`uv`、`python3`、`bash`、`git`、`aws`。使用本机绝对路径（例如 `/Users/me/bin/tool`）或 `docker` 启动的 server 在云端起不来。kiro-cli 会静默跳过启动失败的 server，所以 kac 在提交前会给出警告，任务报告里也会列出来。

**MCP 里的密钥**：

- 可以直接写明文（例如 `"env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."}`）。明文随任务经 TLS 传到 microVM，不会保存到 DynamoDB 或 S3，服务端会把它加入脱敏列表，日志和产物里出现时替换为 `***REDACTED***`。
- 更推荐写成 `${VAR}`，在服务端的"任务环境变量 secret"（默认 `prod/kiro/git-credentials`，JSON 的每个键都是一个环境变量）里提供值。引用了没有设置的变量时，kiro-cli 会把 `${VAR}` 原样传给 server，报告里会提示。
- 不允许引用服务自己的凭证：MCP 配置的 command、url、args、env、headers 里出现 `KIRO_API_KEY` 或 `$AWS_...`（包括 `${env:...}`、`$VAR` 等写法）会被直接拒绝。这是纵深防御：MCP server 本身就是在 microVM 里运行的代码，能读到进程环境变量，所以只同步可信的配置。
- 除了 env、headers，`args` 里形如 `--token xxx`、`--api-key=xxx` 的值和 URL 中 `?token=` 一类的参数也会被加入脱敏列表。

**限制**：压缩后 5 MB 以内，解压后 20 MB 以内，最多 1000 个文件（含目录最多 2000 个条目），单个文件 5 MB 以内，agent 的 front matter 64 KB 以内、prompt 64K 字符以内，front matter 不能使用 YAML 别名。不支持符号链接，隐藏文件（以 `.` 开头）不同步；文件名不能含控制字符或特殊空白，单段不超过 255 个字符、整个路径不超过 1024 个字符。`agents/` 下以 `loop-` 开头的名字是保留名，会被跳过；agent 重名会直接报错。kac 在本地按和服务端相同的规则检查，有问题会在上传前报出来。

提交时 kac 会打印同步了哪些内容和警告。任务结束后，`output.md` 的 "Kiro configuration" 一节和 `loop/kiro_config.json` 会记录清单（文件数、sha256、MCP server、skill、agent 的名字）和警告，不包含文件内容。

## 查看可用模型

```bash
kac models         # 服务的 Kiro API key 可用、且服务端允许的模型；* 标记默认模型
kac models --all   # 同时列出不被允许的模型
```

输出示例：

```
  auto                     1x  Models chosen by task for optimal usage and consistent quality
* claude-sonnet-5.5      1.3x  Claude Sonnet 5.5 model with 1M context window
  gpt-5.6-sol            4.4x  OpenAI GPT 5.6 Sol with 1M context window
  gpt-5.6-terra          2.2x  OpenAI GPT 5.6 Terra with 1M context window
  gpt-5.6-luna           0.6x  OpenAI GPT 5.6 Luna with 1M context window

default: claude-sonnet-5.5  (marked *, used when --model is not given)
```

倍数是 Kiro credits 的费率，直接影响成本。模型名写错或不被允许时，`kac submit` 会立即报错并给出原因（退出码 11）。

结果在每个 microVM 内缓存 5 分钟。

**不通过 kac、直接调用 Runtime 时**：业务错误的 HTTP 状态码也是 200，错误放在响应体的 `error` 字段里，原本的状态码在 `error.http_status` 中（AgentCore 会把容器返回的非 2xx 响应替换成不带信息的 424）。判断是否成功要检查有没有 `error` 字段：

```json
{ "error": { "code": "VALIDATION_ERROR", "http_status": 400, "message": "model 'x' is not allowed; allowed patterns: auto, claude-*, gpt-*" } }
```

token 缺失或无效仍由 AgentCore 直接返回 401 / 403，请求不会到达容器。

## 查看与等待

```bash
kac status tsk_01J...     # 状态、当前阶段和轮次、各阶段结果与耗时
kac wait tsk_01J...       # 轮询到结束，阶段变化时打印一行
kac list --limit 20       # 最近的任务
```

`kac wait` 的退出码可以直接用在脚本里：

| 退出码 | 含义 |
|---|---|
| 0 | SUCCEEDED：所有验收标准通过独立验证 |
| 1 | FAILED：执行出错，见 `error_code` |
| 2 | TIMED_OUT |
| 3 | INCOMPLETE：达到最大轮数仍有标准未通过 |
| 10 | 认证错误（需要 `kac login`） |
| 11 | 请求被拒绝（参数错误、任务不存在等） |
| 12 | 服务端或网络错误 |

所有命令都支持 `--json` 输出。

## 下载结果

```bash
kac result tsk_01J... --out ./result
```

| 文件 | 内容 |
|---|---|
| `output.md` | 最终报告：结论、需求理解、每条验收标准的结果和证据、假设及修改历史、各轮验证结论、执行摘要 |
| `result.json` | 机器可读的结果 |
| `loop/` | 闭环过程文件：`requirements.*`、`plan.*`、`iterations/<n>/execution.*`、`iterations/<n>/verify.*`、`feedback.json`、`assumption_history.json` |
| `artifacts/` | agent 写到 `ARTIFACTS_DIR` 的交付物 |

产物和任务记录在 90 天后自动删除。

## 常见错误码

| error_code | 含义 |
|---|---|
| `VERIFICATION_NOT_PASSED` | INCOMPLETE 时，列出仍未通过的标准 |
| `PHASE_OUTPUT_INVALID` | 某阶段两次都没有产出合规的交接文件 |
| `MODEL_UNAVAILABLE` | 当前 API key 不能使用所选模型，或默认优先级列表中没有可用的模型；错误信息列出可用的模型 |
| `KIRO_AUTH_FAILED` | Kiro API key 无效 |
| `SECRET_UNAVAILABLE` | 读取 secret 失败 |
| `KIRO_CONFIG_INSTALL_FAILED` | 同步的 kiro 配置无法写入容器 |
| `SESSION_LOST` | microVM 被回收，由 Sweeper 标记 |
| `TASK_TIMEOUT` | 超时 |
