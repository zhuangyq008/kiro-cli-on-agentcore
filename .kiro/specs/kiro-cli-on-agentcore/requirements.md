# 需求文档：kiro-cli on AWS AgentCore

## 简介

本项目将 kiro-cli 以 headless 模式运行在 Amazon Bedrock AgentCore Runtime 上，提供一个异步的远程 Kiro agent 服务。调用方（个人通过本地 CLI，或 CI/CD 等系统）提交 prompt 和上下文，服务在隔离的 microVM 中以**闭环模式**执行任务：理解需求、规划、执行、验证、处理反馈，验证不通过时回到相应阶段重做，直到验证通过或达到最大轮数。任务类型包括代码修改、只读 AWS 运维和问答。结果写入 S3，状态记录在 DynamoDB，调用方通过轮询查询状态并下载结果。

每个任务独立执行，结束即止，不支持在同一会话中追问。

部署区域为 `us-east-1`。基础设施使用 CDK (TypeScript) 定义，服务端和本地 CLI 使用 Python 实现。

## 术语

- **Runtime**：Amazon Bedrock AgentCore Runtime 上部署的 agent 容器。
- **Session**：AgentCore Runtime 会话，对应一个独立 microVM。本系统中一个 Session 只运行一个 Task。
- **Task**：一次 prompt 提交及其完整的闭环执行过程，由 `task_id` 唯一标识。
- **Loop（闭环）**：Task 内由编排器驱动的阶段序列：需求理解 → 规划 → 执行 → 验证 → 反馈处理，可回退并重复。
- **Phase（阶段）**：Loop 中的一个步骤。每个阶段由一次独立的 kiro-cli 运行完成。
- **Iteration（轮次）**：一次“执行 + 验证”。回退到任何阶段后再次执行，都算新的一轮。
- **验收标准（Acceptance Criteria）**：验证阶段判定任务是否完成的依据，由调用方提供或由需求理解阶段推导。
- **Orchestrator（编排器）**：Runtime 容器中驱动 Loop 的代码。
- **Query API**：API Gateway (HTTP API) + Lambda 组成的状态与结果查询接口。
- **kac**：本地 Python CLI 工具。
- **Artifacts**：任务最终输出、闭环过程文件及 agent 主动保存的产物文件。

## 需求

### 需求 1：入口鉴权

**用户故事：** 作为服务管理员，我希望所有调用都经过 OAuth 鉴权，这样只有授权的用户和系统能使用服务。

#### 验收标准

1. THE 系统 SHALL 使用新建的 Amazon Cognito User Pool 作为身份提供方。
2. THE Runtime SHALL 配置 inbound JWT authorizer，只接受该 User Pool 签发且 client ID 在允许列表内的 token。
3. THE Query API SHALL 使用同一 User Pool 的 JWT authorizer 鉴权。
4. THE 系统 SHALL 同时支持两类调用方：人类用户（浏览器登录）和系统调用方（client_credentials 流程）。
5. WHEN 请求未携带 token、token 无效或已过期 THEN 系统 SHALL 拒绝请求并返回 401。
6. THE 系统 SHALL 以 token 中的 `sub` 作为任务所有者。

### 需求 2：提交任务

**用户故事：** 作为调用方，我希望提交一个任务后立即拿到任务 ID，不必等待任务执行完成。

#### 验收标准

1. WHEN 调用方提交任务 THEN Runtime SHALL 在 5 秒内返回 `task_id` 和初始状态，并在后台继续执行。
2. THE 提交请求 SHALL 包含 `prompt`（必填），并可选包含 `context`（附加上下文，如仓库地址、分支、说明）、`acceptance_criteria`（验收标准列表）、`model`、`effort`、`max_iterations`、`timeout_seconds`。
3. WHEN 提交请求参数校验失败 THEN 系统 SHALL 返回错误码 `VALIDATION_ERROR`（`http_status` 400）及具体错误信息，且不创建任务。错误放在 HTTP 200 的响应体中，因为 AgentCore 会把容器返回的非 2xx 响应替换成通用的 424，调用方将看不到错误信息。
4. WHEN 任务被接受 THEN 系统 SHALL 在 DynamoDB 中写入任务记录，状态为 `RUNNING`。
5. WHILE 任务执行中 THE Runtime SHALL 通过 `/ping` 上报 `HealthyBusy`，防止 Session 因空闲被回收。
6. THE 每个 Session SHALL 只运行一个任务；WHEN 请求发往已运行过任务或正在运行任务的 Session THEN 系统 SHALL 拒绝请求并返回错误码 `SESSION_BUSY`（`http_status` 409）。

### 需求 3：闭环执行

**用户故事：** 作为调用方，我希望 agent 先理解需求、制定计划，执行后独立验证，并根据验证结果自行修正，直到验收标准满足，这样交付的结果是经过验证的，不是一次性的猜测。

#### 验收标准

1. THE Orchestrator SHALL 按以下阶段驱动任务：需求理解（UNDERSTAND）→ 规划（PLAN）→ 执行（EXECUTE）→ 验证（VERIFY）→ 反馈处理（由 Orchestrator 根据验证结果决定下一步）。
2. THE 每个阶段 SHALL 由一次独立的 kiro-cli 运行完成；阶段之间 SHALL 通过工作区中约定的文件交接，不依赖 kiro 的会话恢复。
3. THE 需求理解阶段 SHALL 产出需求文档，包含：目标、范围、验收标准（每条带唯一编号）、假设及依据。
4. WHEN 调用方提供了 `acceptance_criteria` THEN 需求理解阶段 SHALL 原样纳入这些标准，并可补充推导出的标准；WHEN 调用方未提供 THEN 需求理解阶段 SHALL 根据 prompt 和 context 推导验收标准。
5. THE 规划阶段 SHALL 产出计划，每个步骤关联至少一条验收标准，并为每条验收标准给出验证方法。
6. THE 执行阶段 SHALL 按计划执行；WHEN 不是第一轮 THEN 执行阶段 SHALL 收到上一轮验证的反馈并针对性修正。
7. THE 验证阶段 SHALL 使用全新的 kiro 上下文，不继承执行阶段的对话；SHALL 通过实际检查（如运行测试、执行命令、读取结果）逐条判定验收标准，不能仅依据执行阶段的自述。
8. THE 验证阶段 SHALL 产出结构化的验证报告，包含：每条验收标准的结论（通过 / 未通过）和证据、整体结论、反馈意见，以及建议的回退目标。
9. THE Orchestrator SHALL 根据验证报告决定下一步：
   - 全部通过：结束闭环，任务 `SUCCEEDED`。
   - 执行有缺陷：回到执行阶段。
   - 计划有缺陷：回到规划阶段。
   - 需求理解有误：回到需求理解阶段，并带上验证反馈。
10. THE 回退 SHALL 消耗轮次；WHEN 达到最大轮数且验证仍未通过 THEN 任务 SHALL 以 `INCOMPLETE` 结束，并保留最后一轮的验证报告。
11. THE `max_iterations` 默认值 SHALL 为 3，可按任务指定，且不超过服务端配置的上限。
12. WHEN 某阶段未产出约定文件或文件格式不合法 THEN Orchestrator SHALL 带上错误说明重试该阶段一次；仍不合法时任务 SHALL 以 `FAILED` 结束。
13. THE 闭环的阶段顺序、回退规则和轮次计数 SHALL 由 Orchestrator 代码强制执行，不依赖模型自觉遵守。

### 需求 4：需求澄清

**用户故事：** 作为调用方，我希望 agent 在无人值守时自己澄清不明确的地方，并把它的判断写清楚，方便我事后审查。

#### 验收标准

1. THE 系统 SHALL 不在任务执行过程中向人类提问或等待人类输入。
2. WHEN 需求存在不明确之处 THEN agent SHALL 先重新阅读 prompt、context 和相关仓库内容尝试澄清。
3. WHEN 仍无法确定 THEN agent SHALL 采用影响面最小、最保守的假设，并在需求文档中记录每条假设及其依据。
4. WHEN 验证阶段判定失败原因是需求理解有误 THEN 回到需求理解阶段时 SHALL 修正或替换相关假设，并记录修改原因。
5. THE 最终报告 SHALL 列出所有生效的假设，以及假设被修改的历史。

### 需求 5：执行 kiro-cli

**用户故事：** 作为调用方，我希望任务由 kiro-cli 在无人值守模式下完整执行，能读写代码、调用 shell 和 AWS 只读操作。

#### 验收标准

1. THE Runtime SHALL 从 Secrets Manager 读取 Kiro API key，默认 secret 名为 `prod/kiro/api-key`，可通过部署参数修改；所有任务共用这一个 key，以 `KIRO_API_KEY` 环境变量提供给 kiro-cli。
2. THE Runtime SHALL 以 `--trust-all-tools` 运行 kiro-cli，执行过程中不出现任何交互确认。
3. THE Runtime SHALL 从 Secrets Manager 读取 Git 凭证（默认 secret 名 `prod/kiro/git-credentials`，可修改），并以约定的环境变量（如 `GITHUB_TOKEN`）注入 kiro-cli 进程；WHEN 未配置 Git 凭证 THEN 任务 SHALL 照常执行，只是没有 Git 凭证可用。
4. THE 仓库的 clone、修改、提交、推送等操作 SHALL 由 agent 根据 prompt 和 context 自行完成。
5. THE Runtime SHALL 为 agent 提供约定的产物目录（以 `ARTIFACTS_DIR` 环境变量告知），agent 写入该目录的文件在任务结束时上传到 S3。
6. WHEN kiro-cli 进程异常退出或认证失败 THEN 系统 SHALL 将任务状态置为 `FAILED`，并记录错误摘要。

### 需求 6：模型与思考等级配置

**用户故事：** 作为调用方，我希望按任务指定模型和思考等级，在速度、成本与质量之间取舍。

#### 验收标准

1. THE 系统 SHALL 支持在提交请求中指定 `model` 和 `effort`（low / medium / high / xhigh / max），同一任务的所有阶段使用相同的值。
2. WHEN 请求未指定 `model` THEN 系统 SHALL 按服务端配置的默认模型优先级列表（默认 `claude-sonnet-5.5`、`gpt-5.6-terra`、`auto`），使用第一个当前 API key 可用的模型；WHEN 未指定 `effort` THEN 使用服务端配置的默认值。
3. THE 系统 SHALL 支持服务端配置允许的模型列表（精确 ID 或 `*` / `?` 通配符，默认 `auto`、`claude-*`、`gpt-*`）和 effort 列表；WHEN 请求值不在允许列表中或不是合法的模型 ID THEN 系统 SHALL 返回 `VALIDATION_ERROR`。
4. THE 默认值和允许列表 SHALL 可通过部署参数修改，无需改代码。
5. WHEN 任务开始执行 THEN 系统 SHALL 先用当前 API key 查询可用模型，确认请求的模型可用；WHEN 不可用 THEN 任务 SHALL 以 `FAILED` 结束，并给出明确的错误码。
6. THE 任务记录 SHALL 保存实际使用的 `model` 和 `effort`。
7. THE 系统 SHALL 提供查询可用模型的接口（`kac models`），列出当前 API key 可用的模型、费率倍数、是否被允许，以及未指定模型时会使用的默认模型。

### 需求 7：任务状态、进度与超时

**用户故事：** 作为调用方，我希望随时查询任务状态和闭环进度，并且失控的任务不会无限运行。

#### 验收标准

1. THE 任务状态 SHALL 为以下之一：`RUNNING`、`SUCCEEDED`、`INCOMPLETE`、`FAILED`、`TIMED_OUT`。
2. THE 任务记录 SHALL 包含：`task_id`、`session_id`、所有者、状态、当前阶段、当前轮次、最大轮数、阶段历史、prompt 摘要、`model`、`effort`、创建时间、开始时间、结束时间、耗时、错误摘要（如有）、S3 产物前缀。
3. WHEN 阶段切换 THEN 系统 SHALL 更新任务记录中的当前阶段和轮次，并在阶段历史中追加该阶段的开始时间、结束时间和结果。
4. THE 系统 SHALL 支持整个任务的超时（覆盖所有阶段），默认值可配置，上限不超过 Session 最长存活时间（8 小时）减去收尾余量。
5. WHEN 任务运行超过超时时间 THEN 系统 SHALL 终止当前 kiro-cli 进程，将状态置为 `TIMED_OUT`，并上传已产生的产物。
6. WHEN Session 被意外回收导致任务中断 THEN 该任务 SHALL 最终被标记为 `FAILED`，不永久停留在 `RUNNING`。
7. THE 任务记录 SHALL 通过 DynamoDB TTL 在创建 90 天后自动删除。

### 需求 8：结果与产物持久化

**用户故事：** 作为调用方，我希望任务结束后能下载最终报告和完整的闭环过程，即使 Session 已经结束。

#### 验收标准

1. THE 系统 SHALL 将每个任务的产物写入 `s3://<bucket>/<task-id>/`。
2. THE 系统 SHALL 在每个阶段结束后上传该阶段的过程文件，使任务中途中断时已完成阶段的结果仍可查看。
3. WHEN 任务结束（任何终态）THEN 系统 SHALL 生成最终报告 `output.md`，包含：任务结论、需求理解与验收标准、生效的假设及修改历史、每一轮的验证结论、最终验证报告、执行摘要；并上传 `ARTIFACTS_DIR` 中的文件。
4. THE S3 bucket SHALL 禁止公开访问，使用服务端加密，并强制 TLS。
5. THE S3 bucket SHALL 配置生命周期规则，对象在创建 90 天后自动删除。
6. THE 过程日志 SHALL 输出到 AgentCore 内置的可观测性（CloudWatch Logs / traces），不重复存到 S3；系统创建的 CloudWatch 日志组 SHALL 设置 30 天保留期。
7. THE 任务记录 SHALL 包含对应的 AgentCore session ID，便于在 CloudWatch 中定位日志和 trace。

### 需求 9：查询 API

**用户故事：** 作为调用方，我希望不用 AWS 凭证、只靠 Cognito token 就能查询任务和下载结果。

#### 验收标准

1. THE Query API SHALL 提供：查询单个任务状态与进度、列出当前调用方的任务（按创建时间倒序、支持分页）、获取任务产物的下载链接。
2. THE Query API SHALL 只返回属于当前调用方的任务；WHEN 查询他人任务 THEN 返回 404。
3. THE 产物下载 SHALL 通过 S3 预签名 URL 提供，有效期不超过 15 分钟。
4. THE Query API 的 Lambda SHALL 只拥有读取任务表和对产物 bucket 生成预签名链接所需的最小权限。

### 需求 10：完成事件

**用户故事：** 作为系统集成方，我希望在任务结束时收到事件，以后可以接入 Slack、webhook 等通知渠道。

#### 验收标准

1. WHEN 任务进入终态 THEN 系统 SHALL 向 EventBridge 发布一条事件，包含 `task_id`、`session_id`、所有者、状态、轮次、S3 产物前缀。
2. THE 事件 SHALL 不包含 prompt 全文、输出内容或任何凭证。
3. WHEN 事件发布失败 THEN 系统 SHALL 记录错误日志，但不影响任务状态的正确性。

### 需求 11：本地 CLI 工具（kac）

**用户故事：** 作为在笔记本上工作的开发者，我希望用一个命令行工具完成登录、提交、查询和下载。

#### 验收标准

1. THE kac SHALL 提供以下命令：`configure`、`login`、`submit`、`status`、`wait`、`result`、`list`。
2. THE `login` SHALL 完成 Cognito 登录，并将 token 缓存到本地用户目录，文件权限仅限当前用户读写；WHEN token 过期且有 refresh token THEN 自动刷新。
3. THE `submit` SHALL 支持传入 prompt、context（字符串或文件）、验收标准（可重复的参数或文件）、`--model`、`--effort`、`--max-iterations`、`--timeout`，并输出 `task_id`。
4. THE `status` 和 `wait` SHALL 显示当前阶段和轮次（如 `VERIFY 2/3`）。
5. THE `wait` SHALL 轮询任务状态直到进入终态，并以不同的退出码区分 `SUCCEEDED`、`INCOMPLETE`、`FAILED`、`TIMED_OUT`，便于在脚本和 CI 中使用。
6. THE `result` SHALL 下载任务产物到本地目录（默认 `./<task-id>/`）。
7. THE kac SHALL 支持 `--json` 输出，便于脚本解析。
8. THE kac SHALL 支持通过环境变量提供系统调用方凭证（client_credentials），用于 CI 场景。
9. THE kac SHALL 通过配置文件或环境变量读取服务端点（Runtime ARN、Query API URL、Cognito 配置）。

### 需求 12：安全与权限

**用户故事：** 作为服务管理员，我希望在 agent 拥有全部工具权限的前提下，用基础设施手段限制它能造成的影响。

#### 验收标准

1. THE Runtime 执行角色 SHALL 只拥有：AWS 只读权限、读取指定 secrets、写入产物 bucket、读写任务表、发布 EventBridge 事件、写日志和 trace 所需权限。
2. THE Runtime 执行角色 SHALL 不拥有任何 IAM 写权限，也不能修改 secrets 或删除 S3 对象。
3. THE 系统 SHALL 不在日志、任务记录、事件或产物中写入 Kiro API key、Git 凭证或 Cognito token。
4. THE Runtime SHALL 使用公网网络模式。
5. THE 文档 SHALL 说明 `--trust-all-tools` 带来的已知风险，并建议 Git token 使用仅限所需仓库的 fine-grained token。

### 需求 13：部署、前置配置与可观测性

**用户故事：** 作为服务管理员，我希望按引导完成前置配置后，用一条命令部署整套服务，并能观察其运行情况。

#### 验收标准

1. THE 全部基础设施 SHALL 用 CDK (TypeScript) 定义，部署到 `us-east-1`。
2. THE CDK SHALL 不创建 Kiro API key 和 Git 凭证的 secret，只按名称引用。
3. THE 项目 SHALL 提供前置配置脚本：检查所需 secret 是否存在、格式是否正确，缺失时引导管理员创建（交互输入，值不回显，不出现在命令行参数或 shell 历史中，只短暂存在于权限 0600 的临时文件并在使用后立即删除）；并检查部署所需工具（Node.js、Docker、AWS CLI、CDK bootstrap）。
4. THE 部署 SHALL 包含：容器镜像构建与推送、AgentCore Runtime、Cognito User Pool 及 app clients、DynamoDB 表、S3 bucket、API Gateway + Lambda、EventBridge 总线、IAM 角色。
5. THE 部署输出 SHALL 包含 kac 所需的全部配置值。
6. THE Runtime SHALL 启用 AgentCore 内置可观测性，并输出任务级指标：提交数、各终态数量、执行耗时、轮次数。
7. THE 文档 SHALL 包含前置配置、部署步骤、kac 安装与使用说明，以及并发上限需参照账号 AgentCore Service Quotas 的说明。

### 需求 14：测试

**用户故事：** 作为维护者，我希望关键行为都有自动化测试，确保改动不会破坏闭环逻辑和安全配置。

#### 验收标准

1. THE Runtime SHALL 有单元测试，覆盖参数校验、身份解析、脱敏、闭环状态机的每条转换路径（通过、回到执行、回到规划、回到需求、达到最大轮数、阶段输出不合法、超时）。
2. THE Query API 和 Sweeper SHALL 有单元测试，覆盖归属隔离、分页、超期判断和并发写冲突。
3. THE kac SHALL 有单元测试，覆盖参数解析、token 缓存与刷新、退出码映射。
4. THE CDK stack SHALL 有断言测试，覆盖关键安全属性（bucket 公开访问阻止、TLS、生命周期、TTL、日志保留期、执行角色的显式 Deny）。
5. THE 项目 SHALL 提供部署后的端到端冒烟测试脚本。

## 不在本期范围

- 同一 Session 内多轮追问，以及恢复 kiro 会话
- 任务执行中向人类提问、等待人类回答
- 取消正在运行的任务
- webhook 回调和 Slack 通知（可通过订阅 EventBridge 事件后续扩展）
- 多个 Kiro API key 或按租户分配 key
- 按阶段使用不同的模型或 effort
- VPC 网络模式
- Web 界面

## 已知风险

- 所有任务共用一个 `KIRO_API_KEY`，Kiro 侧的用量额度和限流会集中在这一个 key 上。闭环模式每个任务至少运行 4 次 kiro-cli，credit 消耗约为单次执行的数倍。
- `--trust-all-tools` 下，安全边界完全依赖 IAM 只读、microVM 隔离和凭证最小化。
- 验证阶段由同一模型完成，独立上下文可以减少、但不能消除“自己检查自己”的偏差。
- 并发上限受账号 AgentCore Service Quotas 限制。

### 需求 15：通过 CloudShell 交互式安装（面向不熟悉 AWS 的用户）

**用户故事：** 作为不熟悉 AWS 的客户，我希望只用浏览器，在 AWS CloudShell 里跟着中文向导一步步完成部署，不需要在自己电脑上安装任何工具。

#### 验收标准

1. THE 项目 SHALL 提供 `install.sh` 交互式向导，可在 AWS CloudShell 中直接运行；代码可以通过 `git clone` 或上传 zip 包获得。
2. THE 向导 SHALL 使用中文，每一步说明要做什么，并在部署前请用户确认目标 AWS 账号；区域固定为 `us-east-1`。
3. THE 向导 SHALL 引导用户创建缺失的 Kiro API key secret 和（可选的）GitHub token secret，说明获取方法；输入内容不显示在屏幕上。
4. THE 镜像构建和 `cdk deploy` SHALL 在云端（CodeBuild，arm64）执行，不依赖 CloudShell 的算力、存储和 Docker。
5. WHEN CloudShell 断开或向导中途退出 THEN 云端部署 SHALL 继续；WHEN 用户重新运行向导 THEN 向导 SHALL 发现进行中的部署并接着显示进度，已完成的步骤直接跳过。
6. THE 向导 SHALL 询问用户邮箱并创建登录用户，临时密码由 Cognito 发送到该邮箱。
7. THE 向导 SHALL 在部署后自动运行端到端冒烟测试（必做），并显示每个测试项的结果。
8. THE 向导 SHALL 在 CloudShell 中安装并配置好 `kac`；`kac login` 在 CloudShell 中 SHALL 支持“粘贴浏览器回调地址”的登录方式。
9. THE 安装器 stack SHALL 在安装后保留；WHEN 用户用新版本代码重新运行向导 THEN 向导 SHALL 完成升级。
10. WHEN 构建或部署失败 THEN 向导 SHALL 显示 CloudFormation 报告的失败原因和构建日志摘要，并提示修复后重新运行；首次部署失败 SHALL 不留下孤立的数据资源。
11. THE 项目 SHALL 提供面向小白的图文步骤文档 `docs/cloudshell-install.md`，包括打开 CloudShell、获取代码、运行向导、登录、提交第一个任务、升级、常见问题和卸载。
