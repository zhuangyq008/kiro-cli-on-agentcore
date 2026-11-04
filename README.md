# kiro-cli on AWS AgentCore

把 kiro-cli 以 headless 模式运行在 Amazon Bedrock AgentCore Runtime 上，提供一个异步的远程 agent 服务。每个任务在独立的 microVM 中按闭环执行：

```
UNDERSTAND（理解需求）→ PLAN（规划）→ EXECUTE（执行）→ VERIFY（独立验证）→ 决策
                ↑               ↑               ↑                          │
                └───────────────┴───────────────┴──── 验证未通过时回退 ────┘
```

- 阶段顺序、回退和轮次由代码控制。每个阶段单独运行一次 kiro-cli，阶段之间通过 JSON 文件交接，并做校验。
- 验证阶段使用全新上下文，看不到执行阶段的自述，必须逐条给出证据。全部验收标准通过才算成功。
- 需求不明确时，agent 自行澄清并记录假设，不向人提问。
- 结果写入 S3，状态记录在 DynamoDB，任务结束时向 EventBridge 发布事件。
- 本地用 `kac` 命令行提交任务、查询状态和下载结果，鉴权使用 Cognito OAuth。

## 目录

| 路径 | 内容 |
|---|---|
| `runtime/` | AgentCore 容器：Python wrapper、闭环编排器、4 个阶段的 kiro agent 配置、Dockerfile |
| `lambdas/` | Query API 和 Sweeper 两个 Lambda |
| `infra/` | CDK (TypeScript) stack |
| `cli/` | `kac` 命令行工具 |
| `scripts/` | `bootstrap.sh`（前置检查与 secret 引导）、`smoke_test.sh`（部署后冒烟测试）、`local_loop_check.py`（本地容器内跑一次真实闭环）、`cloudshell-sim/`（模拟 CloudShell 测试安装向导） |
| `install.sh`、`deploy/` | CloudShell 交互式安装向导，以及它使用的 CodeBuild 安装器 |
| `docs/` | 部署文档、kac 使用说明、技术验证结论 |
| `.kiro/specs/kiro-cli-on-agentcore/` | 需求、设计、任务清单 |

## 快速开始

**推荐方式**：在 AWS CloudShell 中运行交互式安装向导，全程只需要浏览器。详见 [docs/cloudshell-install.md](docs/cloudshell-install.md)。

```bash
git clone <仓库地址> kiro-cli-on-agentcore && cd kiro-cli-on-agentcore   # 或上传并解压 zip 包
./install.sh
```

**开发者方式**：在本机部署，需要 Docker、Node.js 和 Python 3.12。

```bash
scripts/bootstrap.sh                       # 检查工具、AWS 身份、CDK bootstrap，引导创建 secret
cd infra && npm ci && npx cdk deploy       # 部署到 us-east-1
cd ../cli && uv tool install --python 3.12 '.[aws]'   # 安装 kac
kac configure --from-stack KiroAgentCoreStack
kac login
kac models                                 # 查看可用模型（Claude、GPT 等，取决于 Kiro API key）
kac submit "..." --kiro-dir ~/.kiro --agent my-agent   # 带上本地的 MCP、skills、steering 和 agent
kac submit "修复 issue #12 并提 PR" --context "仓库: https://github.com/org/repo" --ac "所有现有测试通过"
kac wait <task_id> && kac result <task_id>
```

## 文档

| 文档 | 读者 |
|---|---|
| [docs/cloudshell-install.md](docs/cloudshell-install.md) | 不熟悉 AWS 的客户：在 CloudShell 中一步步安装、登录、提交第一个任务、升级、卸载 |
| [docs/kac.md](docs/kac.md) | 所有使用者：kac 命令、退出码、错误码 |
| [docs/deployment.md](docs/deployment.md) | 开发者和运维：本机部署、部署参数、日志与指标、并发配额 |
| [docs/spike-findings.md](docs/spike-findings.md) | 开发者：kiro-cli headless 和 AgentCore 的技术验证结论 |
| [.kiro/specs/kiro-cli-on-agentcore/](.kiro/specs/kiro-cli-on-agentcore/) | 开发者：需求、设计、任务清单 |

## 开发

```bash
make install   # 安装各子项目依赖
make lint      # ruff + tsc
make test      # 全部单元测试
make image     # 本地构建 linux/arm64 镜像
```

## 安全须知

kiro-cli 以 `--trust-all-tools` 运行，agent 可以在容器内执行任意命令。安全边界依靠以下几点：

- **执行角色只读**：执行角色挂载 `ReadOnlyAccess`，并显式 Deny IAM、AssumeRole、S3 删除、secret 修改等操作。
- **microVM 隔离**：每个任务一个 microVM，任务结束 5 分钟后回收。
- **凭证最小化**：Git token 只授权给 agent 需要操作的仓库，建议使用 fine-grained token。
- **同步的 kiro 配置**：`--kiro-dir` 带上的 MCP server 在 microVM 里以同样的权限运行，可以访问执行角色的只读权限和任务环境变量。只同步可信的配置。MCP 配置里的明文密钥会随任务传输，最好改用 `${VAR}` 并放到任务环境变量 secret 里。

已知风险：仓库内容里的 prompt injection 可能让 agent 执行恶意命令、外传容器内的凭证，或改写本服务自己的任务记录和产物。所有任务共用同一个 Kiro API key，用量和限流会集中在这个 key 上。闭环模式下，每个任务至少运行 4 次 kiro-cli。
