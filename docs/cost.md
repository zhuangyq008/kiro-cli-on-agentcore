# 成本估算

读者：评估这套方案的技术负责人和预算负责人。本文给出费用构成、实测数据和估算方法。金额为美元，单价查询于 2026 年 11 月（us-east-1），以官方价格页为准。

## 结论

- 费用几乎全部来自 **Kiro credits**（实测约占 94%），AgentCore 和其他 AWS 服务的费用很少。
- 没有任务时，基础设施的费用约 **1 美元/月**。
- 一个闭环的代码任务（多文件改动加测试，20–30 分钟）约 **2–2.5 美元**；一个单次运行的小任务不到 **0.05 美元**。

## 费用构成

| 项目 | 计费方式 | 单价 |
|---|---|---|
| Kiro credits | 按任务实际消耗，不同模型倍率不同（`kac models` 可以看到） | 订阅内含的 credit 折合 0.02–0.04 美元，超出部分 0.04 美元/credit |
| AgentCore Runtime（V2） | 按秒，按实际使用的 CPU 和内存；等待 I/O 时 CPU 不计费 | 0.1276 美元/vCPU·小时，0.0169 美元/GB·小时 |
| 其他 AWS 服务 | Secrets Manager、DynamoDB、S3、Lambda、API Gateway、CloudWatch、ECR | 见下文，合计很小 |

价格页：[AgentCore](https://aws.amazon.com/bedrock/agentcore/pricing/)、[Kiro](https://kiro.dev/pricing/)。

## 实测数据

数据来自本项目的开发测试环境，2026-11-04 至 2026-11-10：任务表里的 70 个任务，加上冒烟测试和冷启动基准测试产生的会话。

| 指标 | 数值 | 来源 |
|---|---|---|
| 任务总时长 | 8.64 小时 | 任务表 `duration_ms` |
| Kiro credits | 848.5 | 任务表 `credits_used` |
| CPU 用量 | 3.08 vCPU·小时 | CloudWatch `CPUUsed-vCPUHours` |
| 内存用量 | 104.3 GB·小时 | CloudWatch `MemoryUsed-GBHours` |

折算成费用：

| 项目 | 计算 | 金额 |
|---|---|---|
| AgentCore 计算 | 3.08 × 0.1276 + 104.3 × 0.0169 | 2.16 美元 |
| Kiro credits（按 0.04 美元） | 848.5 × 0.04 | 33.94 美元 |

说明：

- 计算费统一按 V2 单价估算。其中 11-04 和 11-05 实际运行在 V1（单价更低），所以这个数字偏高。
- 计算用量里包含了基准测试的几百个空会话，分摊到任务上也偏高。
- 平均下来，计算费约 **0.25 美元/任务小时**，credits 约 **98 个/任务小时**。

### 单个任务

成功任务的中位数，括号内是第 25 到第 75 百分位。

| 任务类型 | 样本 | 时长 | credits | 计算费 | credits 费用（0.04 美元） |
|---|---|---|---|---|---|
| 单次运行（`none`）：问答、小函数、小改动 | 15 | 28 秒（7–38 秒） | 0.14（0.06–0.75） | 不到 0.01 美元 | 不到 0.03 美元 |
| 闭环（`process`），小任务 | 见 CHANGELOG 的基准 | 26–46 秒 | 约 1 | 不到 0.01 美元 | 约 0.04 美元 |
| 闭环（`process`），实际的代码任务 | 18 个中的大任务 | 21–29 分钟 | 51–63 | 约 0.09–0.12 美元 | 约 2.0–2.5 美元 |

闭环模式每个任务至少运行 4 次 kiro-cli，验证不通过还会回退重做，所以 credits 明显高于单次运行。任务超时或失败时，已经消耗的 credits 同样计费。

## 月度估算示例

按每月 22 个工作日。小任务按 0.75 credit（第 75 百分位），闭环代码任务按 55 credits、25 分钟。

| 场景 | 每天的任务 | credits/月 | Kiro（0.04 美元） | AgentCore | 其他 AWS | 合计/月 |
|---|---|---|---|---|---|---|
| 没有任务 | — | 0 | 0 | 0 | 约 1 | **约 1 美元** |
| 小团队试用 | 20 个小任务 + 5 个闭环代码任务 | 6,380 | 255 | 约 12 | 约 5 | **约 270 美元** |
| 接入 CI | 50 个闭环代码任务 | 60,500 | 2,420 | 约 115 | 约 10 | **约 2,550 美元** |

订阅内含的 credits 单价更低（例如 Pro 是 20 美元 1,000 个，折合 0.02 美元），实际的 Kiro 费用取决于订阅方式。

### 其他 AWS 服务

| 服务 | 说明 | 约每月 |
|---|---|---|
| Secrets Manager | 2 个 secret，各 0.40 美元 | 0.80 |
| ECR | 1.7 GB 镜像，0.10 美元/GB | 0.17 |
| DynamoDB | 按需计费，每个任务几十次读写，开启 PITR | 不到 1 |
| S3 | 产物 90 天过期 | 不到 1 |
| Lambda、API Gateway、EventBridge、Scheduler | Sweeper 每 5 分钟一次，查询按请求 | 不到 1 |
| CloudWatch Logs 和 trace | 0.50 美元/GB 写入，保留 30 天 | 随任务量，1–10 |
| Cognito | 每月 10,000 活跃用户以内免费 | 0 |

## 怎样控制成本

- **选对运行方式**：问答、查询、小改动用默认的单次运行；只有需要可靠交付的任务才用闭环。
- **选对模型**：倍率差别很大。以实测环境为例，`claude-haiku-5.5` 是 0.06x，`claude-sonnet-5.5` 是 1.3x，`claude-opus-5.5` 是 2x。部署时可以用 `allowedModels` 限制可用的模型。
- **限制轮数和时长**：`defaultMaxIterations`（默认 3）、`maxIterationsLimit`（默认 5）和任务超时都是上限。
- **写清楚验收标准**：标准含糊会让闭环多跑几轮。

## 怎样查看自己的实际费用

- 每个任务的 credits：`kac status <task_id>`，或查询任务表的 `credits_used`。按 `owner` 汇总就是每个用户的用量。
- AgentCore 用量：CloudWatch 命名空间 `AWS/Bedrock-AgentCore` 的 `CPUUsed-vCPUHours` 和 `MemoryUsed-GBHours`，按 Runtime 筛选。
- 账单：Cost Explorer 按服务筛选 Amazon Bedrock AgentCore；Kiro 的用量在 Kiro 的账单里。
