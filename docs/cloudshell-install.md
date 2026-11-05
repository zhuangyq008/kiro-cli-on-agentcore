# 在 AWS CloudShell 中安装（推荐）

本文面向不熟悉 AWS 的用户。全程只用浏览器，不需要在自己的电脑上安装任何软件。预计用时 30 分钟，其中大部分时间是等待云端部署。

## 准备

- **AWS 账号**：用一个有管理员权限的用户登录 AWS 控制台。如果不确定自己的权限，请询问账号管理员。
- **Kiro API key**：在 [app.kiro.dev](https://app.kiro.dev) 登录后，在账号设置里新建一个 API key，它以 `ksk_` 开头。安装向导会提示你粘贴。参见 [Headless mode](https://kiro.dev/docs/cli/headless/)。
- **邮箱**：用来接收登录服务的临时密码。
- **（可选）GitHub token**：只有在 agent 需要访问私有仓库、推送代码或创建 PR 时才需要。向导里有创建步骤。

## 第 1 步：打开 CloudShell

1. 登录 [AWS 控制台](https://console.aws.amazon.com/)。
2. 在页面右上角的区域菜单中选择 **美国东部（弗吉尼亚北部）us-east-1**。
3. 点击页面顶部搜索框右边的 **CloudShell 图标**（一个 `>_` 形状的小方框），或者在搜索框里输入 `CloudShell` 打开。
4. 第一次打开需要等待约 1 分钟，直到出现类似 `[cloudshell-user@ip-... ~]$` 的提示符。

## 第 2 步：获取代码

二选一。

**方式 A：git clone**（代码在 Git 仓库中时）

```bash
git clone <仓库地址> kiro-cli-on-agentcore
cd kiro-cli-on-agentcore
```

**方式 B：上传 zip 包**

1. 在 CloudShell 窗口右上角点击 **Actions（操作）→ Upload file（上传文件）**，选择 zip 包。
2. 上传完成后执行（把文件名换成你的 zip 名）：

```bash
unzip -q kiro-cli-on-agentcore.zip -d kiro-cli-on-agentcore
cd kiro-cli-on-agentcore
```

如果解压后多了一层目录，再 `cd` 进去，直到 `ls` 能看到 `install.sh`。

## 第 3 步：运行安装向导

```bash
./install.sh
```

向导会依次完成以下 8 步，每一步都会说明要做什么，需要时请你确认或输入：

| 步骤 | 你需要做什么 |
|---|---|
| 1. 检查运行环境 | 核对显示的 AWS 账号，输入 `y` 确认 |
| 2. 配置 Kiro API key | 粘贴 API key。屏幕上不会显示输入内容，这是正常的。粘贴后按回车 |
| 3. 配置 Git 凭证 | 不需要访问私有仓库时直接回车跳过 |
| 4. 准备部署工具 | 账号第一次使用时输入 `y`，会自动创建 |
| 5. 创建安装器 | 无需操作 |
| 6. 构建并部署服务 | 输入 `y` 开始，然后等待约 15 到 20 分钟 |
| 7. 创建登录用户 | 输入你的邮箱 |
| 8. 冒烟测试 | 自动运行，约 5 到 10 分钟 |

**模型**：不需要额外设置。Kiro API key 有 Claude 时默认用 `claude-sonnet-5.5`，只有 GPT 时默认用 `gpt-5.6-terra`，都没有时用 `auto`。想指定默认模型或限制可用的模型，在启动向导时加参数，例如：

```bash
./install.sh --default-model gpt-5.6-sol,auto --allowed-models 'gpt-*,auto'
```

`--default-model` 是优先级列表，按顺序取第一个可用的；`--allowed-models` 可以用 `*` 通配符。升级时不带这两个参数会沿用当前设置。

**运行方式**：任务默认 `none`，kiro-cli 直接运行一次，最快最省，验收标准由 agent 自查、不做独立验证。需要闭环（理解 → 规划 → 执行 → 独立验证）时，单个任务用 `kac submit --loop-mode process`，或者安装时把默认值改成闭环：

```bash
./install.sh --loop-mode process
```

从旧版本升级时，向导会提示"新版本默认不再走闭环"并要求确认；之后的升级会沿用当前设置。

**等待期间请注意：**

- CloudShell 在键盘空闲 20 到 30 分钟后会断开。请每隔 15 分钟左右在窗口里按一次回车。
- 即使断开也不用担心，部署在云端继续进行。重新打开 CloudShell，执行：

  ```bash
  cd kiro-cli-on-agentcore && ./install.sh
  ```

  向导会自动发现正在进行的部署，并接着显示进度。已经完成的步骤会直接跳过。

看到 **安装完成！** 就说明部署好了。

## 第 4 步：登录并提交第一个任务

1. 在邮箱里找到临时密码。发件人通常是 `no-reply@verificationemail.com`，没收到的话请检查垃圾邮件。
2. 在 CloudShell 中执行：

   ```bash
   kac login
   ```

3. 按屏幕提示操作：
   1. 复制屏幕上的长网址，在浏览器的新标签页中打开。
   2. 输入用户名（或邮箱）和临时密码，然后按要求设置新密码。新密码至少 12 位，需要包含大写字母、小写字母、数字和符号。
   3. 浏览器随后会显示 **“无法访问此网站”**（或 “This site can't be reached”）。**这是正常的**。
   4. 复制浏览器地址栏中的完整网址（以 `http://localhost:8765/callback?code=` 开头），粘贴回 CloudShell，按回车。
   5. 看到 `logged in` 即登录成功。登录状态保持 30 天。

4. 提交任务：

   ```bash
   kac submit "用 Python 写一个计算斐波那契数列的函数，并附上 pytest 测试" --ac "pytest 全部通过"
   ```

   命令会输出一个任务 ID，例如 `tsk_01J...`。

5. 等待并下载结果：

   ```bash
   kac wait tsk_01J...       # 实时显示进度，结束后返回
   kac result tsk_01J...     # 下载结果到 ./tsk_01J.../ 目录
   cat tsk_01J.../output.md  # 查看报告
   ```

如果新开的 CloudShell 窗口里提示找不到 `kac` 命令，先执行 `source ~/.bashrc`。

更多用法见 [kac 使用说明](kac.md)。

## 升级

拿到新版本代码后，在新代码目录中重新运行 `./install.sh`。向导会显示"版本 旧 → 新"，复用已有的配置和 secret（模型设置、运行方式也会沿用），只更新有变化的部分，重新跑一遍冒烟测试，并把 CloudShell 中的 kac 一起更新。

```bash
cd kiro-cli-on-agentcore
git pull            # 方式 A；方式 B 则重新上传并解压新的 zip 包
./install.sh
kac --version       # 确认 CloudShell 中的 kac 已是新版本
```

在其他电脑上使用 kac 的同事，需要各自更新客户端：`git pull` 后执行 `uv tool install --force --python 3.12 './cli[aws]'`，详见 [kac 使用说明](kac.md#更新与版本)。升级前请先看 [CHANGELOG.md](../CHANGELOG.md) 中的"不兼容的变化"。

## 只重跑冒烟测试

```bash
./install.sh --smoke-only
```

## 常见问题

| 现象 | 处理 |
|---|---|
| `无法获取 AWS 身份` | 刷新页面重新打开 CloudShell；确认已登录控制台 |
| 构建失败 | 向导会显示最后 40 行日志。修复后重新运行 `./install.sh`。完整日志在 CloudWatch 控制台的日志组 `/aws/codebuild/kiro-agentcore-installer` 中 |
| `CDK bootstrap 失败` | 当前用户缺少管理员权限，请联系账号管理员 |
| 没收到临时密码邮件 | 检查垃圾邮件；重新运行 `./install.sh`，在“创建登录用户”一步输入同一个邮箱，选择重新发送 |
| `kac login` 粘贴后报错 `does not belong to this login attempt` | 每次 `kac login` 生成的网址只能用一次，请重新执行 `kac login` |
| `kac` 提示认证错误（退出码 10） | 登录已过期，重新执行 `kac login` |
| 任务失败，错误码 `MODEL_UNAVAILABLE` | Kiro API key 不能用所选模型。执行 `kac models` 查看可用的模型，提交时用 `--model` 指定；或用 `./install.sh --default-model ...` 修改默认模型 |
| 冒烟测试提示 `none of the default models is available` | 默认优先级列表里的模型 API key 都不能用。按上面的说明加 `--default-model` 重新运行 `./install.sh` |

## 部署了哪些东西

| 名称 | 说明 |
|---|---|
| CloudFormation stack `KiroAgentCoreStack` | 服务本身：AgentCore Runtime、Cognito 用户池、DynamoDB 表、S3 bucket、查询 API、Lambda 等 |
| CloudFormation stack `KiroAgentCoreInstaller` | 安装器：CodeBuild 项目和源码 bucket。保留它，以后升级会用到 |
| CloudFormation stack `CDKToolkit` | AWS CDK 的部署基础资源，账号内共用 |
| Secrets Manager：`prod/kiro/api-key`、`prod/kiro/git-credentials` | Kiro API key 和（可选的）GitHub token |

## 卸载

在 [CloudFormation 控制台](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks)中，依次选中 `KiroAgentCoreStack` 和 `KiroAgentCoreInstaller`，点击 **Delete（删除）**。

为了防止数据丢失，以下资源在删除 stack 后会保留，需要时在对应控制台手动删除：

- Cognito 用户池
- DynamoDB 表
- 保存任务结果的 S3 bucket（删除前需要先清空）
- Runtime 日志组

Secrets Manager 中的两个 secret 也需要手动删除。`CDKToolkit` 可能被账号内其他项目使用，一般保留。
