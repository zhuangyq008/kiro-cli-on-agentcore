# 部署

> **不熟悉 AWS？** 请按 [在 AWS CloudShell 中安装](cloudshell-install.md) 操作，全程只需要浏览器。本文介绍的是开发者在本机部署的方式，以及两种方式共用的部署参数和运维说明。

部署区域固定为 `us-east-1`。

## 1. 前置条件

| 工具 | 版本 | 用途 |
|---|---|---|
| AWS CLI | v2 | 部署和 secret 管理 |
| Node.js | 20+ | CDK |
| Docker | 能构建 `linux/arm64` 镜像 | 构建 Runtime 镜像。Apple Silicon 原生支持；x86 机器需要 QEMU，构建会慢很多 |
| Python | 3.12+，推荐用 uv 管理 | 测试和 kac |

部署所用的 AWS 身份需要能创建 CloudFormation、IAM、AgentCore、Cognito、DynamoDB、S3、Lambda、API Gateway、EventBridge 和 Scheduler 资源。

## 2. 前置配置

```bash
scripts/bootstrap.sh
```

脚本按顺序检查：工具、AWS 身份和区域、CDK bootstrap、两个 secret、CloudWatch Transaction Search。缺少 secret 时会询问是否创建。输入不回显，值也不会出现在命令行参数或 shell 历史中。值通过一个权限为 0600 的临时文件交给 AWS CLI，调用结束后立即删除。脚本不会修改已经存在的 secret。

| Secret | 默认名称 | 内容 |
|---|---|---|
| Kiro API key | `prod/kiro/api-key` | 字符串，Kiro 门户生成的 API key（`ksk_` 开头）。参见 [Headless mode](https://kiro.dev/docs/cli/headless/) |
| Git 凭证 / 任务环境变量（可选） | `prod/kiro/git-credentials` | JSON 对象，每个键会作为一个环境变量注入 kiro-cli，例如 `{"GITHUB_TOKEN": "github_pat_...", "DOCS_API_TOKEN": "..."}`。同步过来的 MCP 配置里的 `${VAR}` 也从这里取值。键名不能是 `PATH`、`HOME`、`KIRO_API_KEY`、`ARTIFACTS_DIR`，也不能以 `AWS_`、`LD_`、`KIRO_`、`PYTHON` 开头 |

镜像里已为 `github.com` 配置 Git credential helper，使用时才读取 `GITHUB_TOKEN`，所以 token 不会写入文件或 remote URL。

不用 Git 凭证时，跳过该 secret，部署时传入 `-c gitCredentialsSecretName=NONE`。这时同步的 MCP 配置里的 `${VAR}` 没有地方取值，需要用的话要先创建这个 secret，再用默认名称重新部署。

使用其他 secret 名称时，脚本和 CDK 要使用同一个名称：

```bash
scripts/bootstrap.sh --api-key-secret my/kiro/key --git-secret my/kiro/git
npx cdk deploy -c kiroApiKeySecretName=my/kiro/key -c gitCredentialsSecretName=my/kiro/git
```

轮换 secret：`aws secretsmanager put-secret-value --secret-id prod/kiro/api-key --secret-string ...`。每个任务在新的 microVM 中启动时读取 secret，所以更新后新提交的任务立即生效。

## 3. 部署

```bash
cd infra
npm ci
npx cdk deploy
```

首次部署会在本机构建镜像（约 1.7 GB）并推送到 CDK 的 ECR 仓库，耗时取决于网络。

### 部署参数（`infra/cdk.json` 的 `context`，也可以用 `-c key=value` 覆盖）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `defaultModel` | `claude-sonnet-5.5, gpt-5.6-terra, auto` | 默认模型的**优先级列表**：任务未指定模型时，使用第一个 Kiro API key 可用的模型。每一项都必须被 `allowedModels` 允许 |
| `allowedModels` | `auto, claude-*, gpt-*` | 允许使用的模型，可写精确 ID 或 `*` / `?` 通配符；`*` 表示 API key 能用的全部模型 |
| `defaultEffort` / `allowedEfforts` | `high` / 全部 5 级 | `claude-haiku-5.5` 不支持 effort，指定了也会被忽略；GPT 5.6 系列支持全部 5 级 |
| `defaultMaxIterations` / `maxIterationsLimit` | 3 / 5 | 闭环轮数的默认值和上限 |
| `defaultTimeoutSeconds` / `maxTimeoutSeconds` | 7200 / 27000 | 上限要给 8 小时的 Session 留出收尾时间 |
| `kiroCliVersion` | `2.35.2` | 镜像中安装的 kiro-cli 版本 |
| `cognitoDomainPrefix` | `kiro-agentcore-<account>` | Cognito 登录域名前缀，必须全局唯一 |
| `machineClientNames` | `[]` | 需要额外的系统调用方 app client 时，在这里加名字 |

列表参数在 `cdk.json` 中写成数组，用 `-c` 覆盖时写成逗号分隔的字符串，例如 `-c defaultModel=gpt-5.6-terra,auto -c 'allowedModels=gpt-*,auto'`。配置冲突（例如默认模型不在允许列表内）时 `cdk deploy` 会直接报错。

### 选择模型（Claude、GPT 及其他）

不同的 Kiro API key 能用的模型不同：有的只有 Claude，有的只有 GPT，有的还包含 DeepSeek、GLM、Qwen 等。服务不写死模型 ID：

- 任务指定了 `--model` 时，PREFLIGHT 确认该模型对 API key 可用，否则以 `MODEL_UNAVAILABLE` 结束，错误信息中列出可用的模型。
- 任务未指定模型时，按 `defaultModel` 的顺序选第一个可用的模型，并写入任务记录。默认值下，有 Claude 的 key 用 `claude-sonnet-5.5`，只有 GPT 的 key 用 `gpt-5.6-terra`，都没有时用 `auto`。
- 部署后用 `kac models` 查看 API key 实际可用的模型、费率倍数、是否被允许，以及默认会用哪个。

常见配置：

| 场景 | 参数 |
|---|---|
| 只有 GPT，默认用 Sol | `-c defaultModel=gpt-5.6-sol,auto -c 'allowedModels=gpt-*,auto'` |
| 控制成本，只允许 Luna 和 Haiku | `-c defaultModel=gpt-5.6-luna -c allowedModels=gpt-5.6-luna,claude-haiku-5.5` |
| 允许 API key 的全部模型 | `-c 'allowedModels=*'` |

CloudShell 安装向导使用同名参数：`./install.sh --default-model gpt-5.6-sol,auto --allowed-models 'gpt-*,auto'`。升级时不带这两个参数，会沿用当前部署的设置（从 stack 输出 `DefaultModels` / `AllowedModels` 读取）。

**从旧版本升级**：旧版本没有记录模型设置（stack 输出里没有 `DefaultModels`）。升级后未指定的项会使用新的默认值，允许列表会从 4 个 Claude 模型放宽为 `auto, claude-*, gpt-*`。CloudShell 安装向导遇到这种情况会提示并要求确认；用 CDK 直接部署时，如需保留旧的限制，请显式传 `-c allowedModels=...`。

注意费率：`kac models` 显示的倍数直接决定 credits 消耗，例如 `gpt-5.6-sol` 为 4.4 倍、`gpt-5.6-terra` 为 2.2 倍、`gpt-5.6-luna` 为 0.6 倍。

### 部署输出

| 输出 | 用途 |
|---|---|
| `RuntimeArn` | kac 提交任务 |
| `QueryApiUrl` | kac 查询任务 |
| `CognitoDomain`、`CliClientId`、`OAuthScope` | kac 登录 |
| `MachineClientId` | CI 等系统调用方 |
| `DefaultModels`、`AllowedModels` | 当前的模型设置，CloudShell 安装向导升级时沿用 |
| `UserPoolId` | 创建用户 |
| `RuntimeLogGroupName` | Runtime 日志 |
| `ArtifactsBucketName`、`TasksTableName`、`EventBusName` | 运维排查 |

## 4. 创建用户

User Pool 关闭了自助注册，需要由管理员创建用户：

```bash
POOL=$(aws cloudformation describe-stacks --stack-name KiroAgentCoreStack --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
aws cognito-idp admin-create-user --region us-east-1 --user-pool-id "$POOL" \
  --username alice --user-attributes Name=email,Value=alice@example.com Name=email_verified,Value=true
```

用户首次登录时，Cognito 会要求修改临时密码。

系统调用方使用 `kac-machine` client，通过 client_credentials 流程获取 token。查询 client secret：

```bash
aws cognito-idp describe-user-pool-client --region us-east-1 --user-pool-id "$POOL" \
  --client-id <MachineClientId> --query UserPoolClient.ClientSecret --output text
```

把这个 secret 存进 CI 系统自己的 secret 管理，不要提交到仓库。

## 5. 验证

```bash
cd cli && uv tool install --python 3.12 '.[aws]'
kac configure --from-stack KiroAgentCoreStack
kac login
scripts/smoke_test.sh
```

冒烟测试包括三个场景，会消耗 Kiro credits：
1. 问答任务，答案写入产物，预期 SUCCEEDED。
2. 小型代码任务，预期 SUCCEEDED，并检查闭环过程文件是否齐全。
3. 60 秒超时的任务，预期 TIMED_OUT。

部署后还需要确认以下配置实际生效：

```bash
aws logs describe-log-groups --region us-east-1 --log-group-name-prefix /aws/bedrock-agentcore/runtimes/ \
  --query 'logGroups[].[logGroupName,retentionInDays]'          # 应为 30
aws s3api get-bucket-lifecycle-configuration --bucket <ArtifactsBucketName>   # 90 天
aws dynamodb describe-time-to-live --table-name <TasksTableName>            # expires_at ENABLED
```

## 6. 运维

- **日志**：Runtime 的日志组 `RuntimeLogGroupName`，每条日志都带 `task_id`、`session_id`、`phase`、`iteration`。查询示例（CloudWatch Logs Insights）：
  ```
  fields ts, message, phase, iteration, outcome | filter task_id = "tsk_..." | sort ts asc
  ```
- **Traces**：CloudWatch 控制台的 GenAI Observability 页面，需要先启用 Transaction Search。
- **指标**：命名空间 `KiroAgentCore`，包括 `TasksSucceeded`、`TasksIncomplete`、`TasksFailed`、`TasksTimedOut`、`TaskDurationMs`、`TaskIterations`、`TaskCredits`、`PhaseDurationMs`。
- **会话丢失**：microVM 被平台回收时任务无法自己收尾，Sweeper 每 5 分钟检查一次，把心跳超过 10 分钟的任务标记为 `FAILED / SESSION_LOST`。
- **完成事件**：总线 `kiro-agentcore`，source 为 `kiro.agentcore`，detail-type 为 `TaskCompleted`。本期没有建订阅规则，需要通知时自己加规则，转发到 SNS、Slack 或 webhook。

### 并发上限

并发任务数受账号的 AgentCore Runtime 配额限制，每个任务占用一个 Session。查询当前配额：

```bash
aws service-quotas list-service-quotas --service-code bedrock-agentcore --region us-east-1 \
  --query 'Quotas[].[QuotaName,Value]' --output table
```

需要更多并发时，在 Service Quotas 控制台申请提额。另外，所有任务共用一个 Kiro API key，Kiro 侧的限流也会影响并发。

## 7. 删除

```bash
cd infra && npx cdk destroy
```

使用 CloudShell 安装器部署的，还需要删除 `KiroAgentCoreInstaller` stack（可以在 CloudFormation 控制台操作）。

为了防止数据丢失，Cognito User Pool、DynamoDB 表和 S3 bucket 使用 `RETAIN_ON_UPDATE_OR_DELETE`。删除 stack 时它们会被保留，需要手动清理。首次部署失败回滚时，这些资源会自动删除。

Runtime 日志组由 AgentCore 创建，删除 stack 后也会保留。两个 secret 不由 CDK 管理，不受影响。
