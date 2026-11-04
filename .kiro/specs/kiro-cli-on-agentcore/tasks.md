# 实现计划：kiro-cli on AWS AgentCore

每个任务都包含对应的自动化测试（需求 14）。方括号内为关联的需求编号。

- [x] 1. 技术验证
  - [x] 1.1 确认 kiro agent 配置文件格式（系统提示、工具列表），以及能否去掉文件写入工具同时保留 shell [3.7]
  - [x] 1.2 验证 `--no-interactive --agent` 下，agent 能否稳定地把文件写到指定绝对路径 [3.2]
  - [x] 1.3 记录 stream-json 事件结构，确认如何识别正常结束、异常结束和认证失败 [5.6]
  - [x] 1.4 确认在 `linux/arm64` 容器中安装固定版本 kiro-cli 的方式和数据目录位置 [5.1]
  - [x] 1.5 确认只设置 `KIRO_API_KEY` 时是否还需要其他初始化 [5.1]
  - [x] 1.6 结论写入 `docs/spike-findings.md`，必要时更新 design.md（日志组预建的验证推迟到 11.5 / 14.3）

- [x] 2. 项目骨架
  - [x] 2.1 建立目录结构、`.gitignore`、根目录 `Makefile`（lint、test、build 入口）
  - [x] 2.2 `runtime/`、`lambdas/`、`cli/` 的 `pyproject.toml`，配置 pytest 和 ruff
  - [x] 2.3 `infra/` CDK TypeScript 工程，配置 jest

- [x] 3. Runtime 基础模块
  - [x] 3.1 `config.py`：读取并校验环境变量，提供默认值 [6.2, 6.4]
  - [x] 3.2 `identity.py`：从 Authorization 头解析 `sub` [1.6]
  - [x] 3.3 `redact.py`：已知 secret 值脱敏 [12.3]
  - [x] 3.4 `secrets.py`：读取 API key 和可选的 Git 凭证，校验 Git 凭证格式 [5.1, 5.3]
  - [x] 3.5 单元测试

- [x] 4. 阶段文件结构与一致性检查
  - [x] 4.1 `schemas.py`：requirements / plan / execution / verify 的 pydantic 模型 [3.3, 3.5, 3.8]
  - [x] 4.2 跨文件一致性检查：调用方标准不可删改、计划覆盖、验证完整、代码计算整体结论 [3.4, 3.5, 3.7, 3.9]
  - [x] 4.3 假设修改历史的对比 [4.4, 4.5]
  - [x] 4.4 单元测试

- [x] 5. kiro-cli 运行器
  - [x] 5.1 `kiro_runner.py`：拼接命令行、只向子进程注入凭证、读取 stream-json 写日志、识别认证失败 [5.1, 5.2, 5.6, 12.3]
  - [x] 5.2 截止时间与进程组终止（SIGTERM 后 SIGKILL）[7.5]
  - [x] 5.3 测试用假 kiro-cli（按剧本写文件、返回退出码、挂起）
  - [x] 5.4 单元测试

- [x] 6. 工作区与 AWS 适配层
  - [x] 6.1 `workspace.py`：目录初始化、任务标记文件、Git 已跟踪文件变更检测 [2.6, 3.7]
  - [x] 6.2 `store.py`：任务建档、阶段与轮次更新、心跳、条件写终态 [2.4, 7.2, 7.3, 7.7]
  - [x] 6.3 `artifacts.py`：阶段增量同步与最终上传 [8.1, 8.2]
  - [x] 6.4 `events.py`、`metrics.py` [10.1–10.3, 13.6]
  - [x] 6.5 单元测试（moto）

- [x] 7. 阶段定义与 agent 配置
  - [x] 7.1 4 个阶段的 kiro agent 配置及系统提示 [3.1, 3.3–3.8, 4.1–4.4]
  - [x] 7.2 `phases.py`：每个阶段的输入准备、prompt 组装、输出路径 [3.2, 3.6]
  - [x] 7.3 单元测试

- [x] 8. 闭环编排器与最终报告
  - [x] 8.1 `orchestrator.py`：PREFLIGHT、阶段推进、回退决策、轮次计数、重试、超时、FINALIZE [3.1, 3.9–3.13, 6.5, 7.4–7.5]
  - [x] 8.2 `report.py`：生成 `output.md` 和 `result.json` [4.5, 8.3]
  - [x] 8.3 状态机测试，覆盖 design.md 测试策略中列出的全部路径

- [x] 9. Runtime 入口与容器
  - [x] 9.1 `app.py`：参数校验、单任务限制、建档、后台线程与 async task [2.1–2.6]
  - [x] 9.2 `Dockerfile`（arm64、固定 kiro-cli 版本、非 root 用户、预置 agent 配置）
  - [x] 9.3 单元测试；本地构建镜像并验证 `/ping` 和 `/invocations`；用 `scripts/local_loop_check.py` 在容器中跑通一次真实闭环

- [x] 10. Query API 与 Sweeper
  - [x] 10.1 Query Lambda：单任务查询、分页列表、产物预签名链接、归属隔离 [9.1–9.4]
  - [x] 10.2 Sweeper Lambda：超期判断、条件更新、发布事件 [7.6]
  - [x] 10.3 单元测试

- [x] 11. CDK stack
  - [x] 11.1 Cognito（User Pool、resource server、两个 app client、域名）[1.1, 1.4]
  - [x] 11.2 DynamoDB、S3、EventBridge 总线 [7.7, 8.4, 8.5]
  - [x] 11.3 Runtime（镜像 asset、JWT authorizer、header allowlist、生命周期、环境变量、执行角色）[1.2, 12.1, 12.2, 12.4]
  - [x] 11.4 HTTP API + Query Lambda、Sweeper + Scheduler [1.3, 7.6]
  - [x] 11.5 日志组 30 天保留、输出 kac 所需配置 [8.6, 13.5]
  - [x] 11.6 断言测试 [14.4]

- [x] 12. kac CLI
  - [x] 12.1 配置加载（文件、环境变量、`--from-stack`）[11.9]
  - [x] 12.2 认证：PKCE 登录、token 缓存与刷新、client_credentials [11.2, 11.8]
  - [x] 12.3 命令：submit、status、wait、result、list，`--json` 输出与退出码 [11.1, 11.3–11.7]
  - [x] 12.4 单元测试

- [x] 13. 脚本与文档
  - [x] 13.1 `scripts/bootstrap.sh` [13.3]
  - [x] 13.2 `scripts/smoke_test.sh` [14.5]
  - [x] 13.3 `docs/deployment.md`、`docs/kac.md`、README，包括 `--trust-all-tools` 风险说明和 Service Quotas 说明 [12.5, 13.7]

- [x] 14. 部署与端到端验证（通过 CloudShell 安装向导在测试账号中完成）
  - [x] 14.1 运行 bootstrap 脚本并部署
  - [x] 14.2 运行冒烟测试，确认三个场景结果符合预期
  - [x] 14.3 确认日志组保留期、S3 生命周期和 TTL 实际生效

- [ ] 15. CloudShell 交互式安装 [15]
  - [x] 15.1 安装器 stack `deploy/installer.yaml`：CodeBuild（arm64，privileged）、源码 bucket、只能扮演 CDK bootstrap 角色的构建角色
  - [x] 15.2 `deploy/buildspec.yml`、`codebuild-deploy.sh`、`codebuild-smoke.sh`；冒烟测试改为三个场景并行
  - [x] 15.3 `install.sh` 中文向导：环境与账号确认、secret 引导、CDK bootstrap、启动并跟踪云端构建（可断点续看）、创建用户、冒烟测试、安装 kac
  - [x] 15.4 kac `login --paste`（CloudShell 中默认开启）及测试
  - [x] 15.5 修复：运行时日志组由 AgentCore 自动创建，改用 LogRetention 设置 30 天保留；数据资源改为 RETAIN_ON_UPDATE_OR_DELETE，首次部署失败时不留孤立资源
  - [x] 15.6 `docs/cloudshell-install.md` 小白文档
  - [x] 15.7 在 Amazon Linux 2023 模拟环境中对测试账号完整运行向导（部署 + 冒烟测试）
  - [ ] 15.8 由用户在真实 CloudShell 中按文档走一遍（含邮箱收信和 `kac login`）
