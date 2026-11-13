"""Slide content for the customer deck. Geometry is in inches on a 13.333 x 7.5 slide."""

from __future__ import annotations

import re
from pathlib import Path

from deck_model import (
    ACCENT,
    BLUE,
    DARK,
    GREEN,
    INK,
    LINE,
    MUTED,
    PANEL,
    WHITE,
    H,
    Image,
    P,
    Rect,
    Run,
    Slide,
    T,
    W,
)

MX = 0.6  # horizontal margin
CW = W - 2 * MX  # content width
FOOTER = "kiro-cli on AgentCore · 方案介绍"


def page(title: str, number: int, notes: str = "") -> Slide:
    s = Slide(notes=notes)
    s.add(
        Rect(MX, 0.55, 0.11, 0.5, fill=ACCENT),
        T(MX + 0.28, 0.45, CW - 0.3, 0.7, P(Run(title, bold=True)), size=26, valign="middle"),
        Rect(MX, 6.98, CW, 0.012, fill=LINE),
        T(MX, 7.05, 6, 0.3, FOOTER, size=10, color=MUTED),
        T(W - MX - 1, 7.05, 1, 0.3, P(str(number), align="right"), size=10, color=MUTED),
    )
    return s


def card(s: Slide, x: float, y: float, w: float, h: float, title: str, body: str, badge: str | None = None,
         title_size: float = 18, body_size: float = 14) -> None:
    s.add(Rect(x, y, w, h, fill=PANEL, radius=0.12))
    top = y + 0.3
    if badge:
        s.add(Rect(x + 0.3, top, 0.5, 0.5, fill=ACCENT, shape="oval"),
              T(x + 0.3, top, 0.5, 0.5, P(Run(badge, bold=True), align="center"), size=16, valign="middle"))
        top += 0.72
    s.add(T(x + 0.3, top, w - 0.6, 0.45, P(Run(title, bold=True)), size=title_size, valign="middle"),
          T(x + 0.3, top + 0.58, w - 0.6, h - (top - y) - 0.75, body, size=body_size, color=MUTED, line_spacing=1.35))


def table(s: Slide, x: float, y: float, widths: list[float], rows: list[list[str]], row_h: float | list[float],
          size: float = 12, first_col_bold: bool = True) -> float:
    """Header row plus body rows; returns the y coordinate below the table."""
    heights = row_h if isinstance(row_h, list) else [0.45] + [row_h] * (len(rows) - 1)
    cy = y
    for ri, (row, h) in enumerate(zip(rows, heights, strict=True)):
        header = ri == 0
        fill = INK if header else (PANEL if ri % 2 else WHITE)
        s.add(Rect(x, cy, sum(widths), h, fill=fill, line=None if header else LINE))
        cx = x
        for ci, (cell, w) in enumerate(zip(row, widths, strict=True)):
            bold = header or (ci == 0 and first_col_bold)
            s.add(T(cx + 0.14, cy, w - 0.28, h, P(Run(cell, bold=bold)), size=size,
                    color=WHITE if header else INK, valign="middle", line_spacing=1.15))
            cx += w
        cy += h
    return cy


def demo_facts(casts: dict) -> dict:
    """Numbers shown on the demo slide, read from the terminal recordings made for the video."""
    facts = {"d1_seconds": "约 1 分钟", "d1_credits": "不到 1", "d1_tests": "全部",
             "d2_seconds": "约 3 分钟", "d2_credits": "约 4", "d2_criteria": "全部"}

    def output(cast: str, step: str) -> tuple[str, float]:
        st = next(s for s in casts[cast]["steps"] if s["id"] == step)
        return "".join(c[1] for c in st["chunks"]), st["seconds"]

    def fmt(seconds: float) -> str:
        seconds = round(seconds)
        return f"{seconds // 60} 分 {seconds % 60:02d} 秒" if seconds >= 60 else f"{seconds} 秒"

    if "demo1" in casts:
        text, seconds = output("demo1", "wait")
        facts["d1_seconds"] = fmt(seconds)
        if m := re.search(r"credits: ([\d.]+)", text):
            facts["d1_credits"] = f"{float(m.group(1)):.2f}"
        if m := re.search(r"(\d+) passed", output("demo1", "pytest")[0]):
            facts["d1_tests"] = f"{m.group(1)} 个"
    if "demo2" in casts:
        text, seconds = output("demo2", "wait")
        facts["d2_seconds"] = fmt(seconds)
        if m := re.search(r"credits: ([\d.]+)", text):
            facts["d2_credits"] = f"{float(m.group(1)):.2f}"
        if m := re.search(r"criteria passed: (\d+)/(\d+)", text):
            facts["d2_criteria"] = f"{m.group(1)}/{m.group(2)}"
    return facts


def build_slides(architecture_png: Path, casts: dict) -> list[Slide]:
    slides: list[Slide] = []

    # 1 ---------------------------------------------------------------- cover
    s = Slide(background=DARK, notes="开场：一句话说明这是什么，以及今天要回答的四个问题：解决什么、怎么用、安不安全、花多少钱。")
    s.add(
        Rect(0.9, 2.05, 1.2, 0.07, fill=ACCENT),
        T(0.9, 2.3, 6, 0.4, P(Run("方案介绍", color=ACCENT)), size=16),
        T(0.9, 2.7, 11.5, 1.3, P(Run("kiro-cli on AgentCore", bold=True)), size=54, color=WHITE, valign="middle"),
        T(0.9, 4.15, 11.5, 0.6, "把 Kiro 的编码 agent 变成运行在你自己 AWS 账号里的异步服务", size=24, color="C9D4E5"),
        T(0.9, 6.3, 8, 0.4, "版本 0.3.0 · 2026 年 11 月", size=14, color="9FB0C8"),
    )
    slides.append(s)

    # 2 ---------------------------------------------------------------- problem
    s = page("团队使用编码 agent 时遇到的三个问题", 2,
             "先问客户现在怎么用 agent：在谁的电脑上跑、结果放哪里、有没有接 CI。用他们自己的回答引出这三点。")
    cw = (CW - 2 * 0.3) / 3
    items = [
        ("占用开发者的电脑", "任务运行的时候人只能等。长任务、多任务排不开，合上电脑任务就断了。"),
        ("权限和结果分散", "凭证、日志和产出留在每个人的机器上。谁跑了什么、用了多少、结果在哪里，没有统一的记录。"),
        ("接不进流水线", "很难在 CI 里批量、无人值守地运行，也没有可靠的方式判断任务是否真的完成。"),
    ]
    for i, (title, body) in enumerate(items):
        card(s, MX + i * (cw + 0.3), 1.65, cw, 3.3, title, body, badge=str(i + 1))
    s.add(Rect(MX, 5.35, CW, 1.1, fill="FFF6E5", radius=0.1),
          T(MX + 0.35, 5.35, CW - 0.7, 1.1,
            P(Run("目标　", bold=True, color="B36B00"), "让 agent 的任务像 CI 任务一样：提交后在云端运行，有记录、有通知、有验收。"),
            size=18, valign="middle"))
    slides.append(s)

    # 3 ---------------------------------------------------------------- solution
    s = page("方案：账号里的异步 agent 服务", 3,
             "强调两点：用的是官方 kiro-cli，不是另一个 agent；所有资源在客户自己的账号里。")
    s.add(T(MX, 1.5, CW, 1.0,
            "kiro-cli 以无人值守方式运行在 Amazon Bedrock AgentCore Runtime 上。开发者或 CI 用一条命令提交任务，"
            "任务在云端独立运行，结束后取回报告和产物。", size=18, line_spacing=1.4))
    cw = (CW - 3 * 0.25) / 4
    caps = [
        ("异步提交", "kac submit 返回任务编号后就可以离开；kac wait 和 kac result 随时取结果。"),
        ("隔离运行", "每个任务一个 microVM，2 到 3 秒启动，任务结束后回收。"),
        ("可选闭环", "理解、规划、执行、独立验证。验收标准逐条给出证据，不通过则回退重做。"),
        ("留存和通知", "结果写入 S3，状态记录在 DynamoDB，任务结束时发布 EventBridge 事件。"),
    ]
    for i, (title, body) in enumerate(caps):
        card(s, MX + i * (cw + 0.25), 2.95, cw, 2.7, title, body, title_size=18, body_size=14)
    slides.append(s)

    # 4 ---------------------------------------------------------------- architecture
    s = page("架构", 4, "按 ①②③ 讲调用路径，再讲数据落在哪里，最后指出唯一出账号的数据流：发往 Kiro 服务的 prompt 和代码。")
    iw = 11.4
    s.add(Image(architecture_png, (W - iw) / 2, 1.4, iw, iw * 770 / 1720),
          T(MX, 6.55, CW, 0.35, P("全部资源由 CloudFormation 部署在你的账号（us-east-1）。模型推理由 Kiro 服务完成，在账号之外。", align="center"),
            size=12, color=MUTED))
    slides.append(s)

    # 5 ---------------------------------------------------------------- usage
    s = page("怎么用：三条命令", 5, "可以在这里切到演示视频的第一段。")
    s.add(Rect(MX, 1.55, 7.2, 3.35, fill=DARK, radius=0.1))
    code = [
        ('$ kac submit "修复 issue #12 并提 PR" \\', "E6EDF7"),
        ('    --context "仓库: https://github.com/org/repo" \\', "E6EDF7"),
        ('    --ac "所有现有测试通过" \\', "E6EDF7"),
        ("    --loop-mode process", "E6EDF7"),
        ("tsk_01M79GC227G1VHJW97K00AM9CZ", "5CC8FF"),
        ("", "E6EDF7"),
        ("$ kac wait tsk_01M79GC2...     # 等待完成，显示阶段进度", "E6EDF7"),
        ("$ kac result tsk_01M79GC2...   # 下载报告和产物", "E6EDF7"),
    ]
    s.add(T(MX + 0.3, 1.8, 6.6, 2.9, *[P(Run(line, mono=True, color=c)) for line, c in code], size=13, line_spacing=1.3))
    s.add(T(MX, 5.15, 7.2, 1.7,
            P("本地的 MCP、skills、steering 和自定义 agent 可以用 --kiro-dir 随任务带上", bullet=True, space_after=5),
            P("模型可选 Claude、GPT 等，取决于 Kiro API key，用 kac models 查看", bullet=True, space_after=5),
            P("结果包括 output.md 报告、result.json 和产物文件", bullet=True),
            size=14))
    table(s, 8.1, 1.55, [0.75, 1.85, 2.03], [
        ["", "单次运行（默认）", "闭环"],
        ["过程", "kiro-cli 运行一次", "理解、规划、执行、验证，不通过则回退"],
        ["验收", "agent 自查", "独立验证，逐条给出证据"],
        ["适合", "问答、查询、小改动", "要求测试通过、要提 PR 的任务"],
        ["实测", "几秒到 1 分钟", "小任务约 30 秒起，代码任务几分钟到半小时"],
    ], [0.45, 0.85, 0.75, 0.85, 1.0], size=12)
    s.add(T(8.1, 5.6, 4.63, 0.6, "用 --loop-mode 选择；部署时可以修改默认值。", size=12, color=MUTED))
    slides.append(s)

    # 6 ---------------------------------------------------------------- closed loop
    s = page("闭环模式：流程由代码控制，结果经过独立验证", 6,
             "这是和直接跑 agent 最大的区别。可以切到演示视频的第二段。")
    bw, gap = 2.55, 0.64
    phases = [("UNDERSTAND", "理解需求"), ("PLAN", "规划"), ("EXECUTE", "执行"), ("VERIFY", "独立验证")]
    for i, (en, zh) in enumerate(phases):
        x = MX + i * (bw + gap)
        last = i == len(phases) - 1
        s.add(Rect(x, 1.6, bw, 1.15, fill=GREEN if last else INK, radius=0.12),
              T(x, 1.72, bw, 0.4, P(Run(en, bold=True), align="center"), size=15, color=WHITE, valign="middle"),
              T(x, 2.14, bw, 0.45, P(zh, align="center"), size=16, color=WHITE, valign="middle"))
        if not last:
            s.add(T(x + bw, 1.6, gap, 1.15, P(Run("→", bold=True), align="center"), size=26, color=ACCENT, valign="middle"))
    s.add(Rect(MX, 3.0, CW, 0.55, fill="FFF6E5", radius=0.08),
          T(MX, 3.0, CW, 0.55, P("验证未通过时，回退到相应的阶段重做。默认最多 3 轮，可以配置。", align="center"),
            size=14, color="B36B00", valign="middle"))
    s.add(T(MX, 3.95, CW, 2.8,
            P(Run("流程由代码控制　", bold=True), "阶段顺序、回退和轮次不依赖模型自觉。每个阶段单独运行一次 kiro-cli，"
              "阶段之间用 JSON 文件交接并校验。", bullet=True, space_after=12),
            P(Run("验证是独立的　", bold=True), "验证阶段使用全新的上下文，看不到执行阶段的自述，必须对每条验收标准给出证据。"
              "全部通过才算成功。", bullet=True, space_after=12),
            P(Run("不向人提问　", bold=True), "需求不明确时，agent 自行澄清并把假设写进报告。", bullet=True),
            size=16, line_spacing=1.35))
    slides.append(s)

    # 7 ---------------------------------------------------------------- scenarios
    s = page("适用场景", 7, "按客户的实际情况挑一两个场景展开，其余略过。")
    end = table(s, MX, 1.5, [2.9, 5.0, 4.23], [
        ["场景", "例子", "建议的方式"],
        ["存量代码迁移和升级", "Python 2 到 3、Java 8 到 17、依赖升级，用测试做验收", "闭环；按仓库批量提交"],
        ["在 CI 里处理 issue", "修复 bug 并提 PR，现有测试必须通过", "闭环；CI 用机器账号；PR 经评审合入"],
        ["补测试和文档", "为老模块补单元测试，生成接口文档", "单次运行或闭环"],
        ["设备和产线数据脚本", "协议解析（Modbus、OPC UA）、报表和数据转换脚本", "单次运行"],
        ["AWS 资源盘点和巡检", "梳理账号里的资源配置，输出巡检报告", "部署时开启 viewonly 或自定义只读策略"],
    ], 0.72, size=13)
    s.add(T(MX, end + 0.3, CW, 0.7,
            P(Run("目前不适合　", bold=True, color="B36B00"), "需要人实时确认的任务；需要访问内网资源的任务（当前版本不支持部署到 VPC）。"),
            size=14, color=MUTED))
    slides.append(s)

    # 8 ---------------------------------------------------------------- demo
    f = demo_facts(casts)
    s = page("演示：两个真实任务", 8, "数字来自录制演示视频时的真实运行。播放视频：kiro-cli-on-agentcore-demo.mp4。")
    pw = (CW - 0.3) / 2
    panels = [
        ("演示 1　单次运行", "写一个解码 Modbus 寄存器的 Python 函数，并附带单元测试", [
            ("耗时", f["d1_seconds"]), ("Kiro credits", f["d1_credits"]),
            ("产出", "函数和测试文件"), ("本地复验", f"{f['d1_tests']}测试通过")]),
        ("演示 2　闭环加独立验证", "把一个 Python 2 的产线良率报表脚本迁移到 Python 3.12，带 4 条验收标准", [
            ("耗时", f["d2_seconds"]), ("Kiro credits", f["d2_credits"]),
            ("验收标准", f"{f['d2_criteria']} 通过，逐条有证据"), ("产出", "迁移后的脚本、测试、MIGRATION.md")]),
    ]
    for i, (title, task, rows) in enumerate(panels):
        x = MX + i * (pw + 0.3)
        s.add(Rect(x, 1.55, pw, 4.55, fill=PANEL, radius=0.12),
              T(x + 0.35, 1.8, pw - 0.7, 0.45, P(Run(title, bold=True)), size=19, valign="middle"),
              T(x + 0.35, 2.4, pw - 0.7, 0.9, task, size=14, color=MUTED, line_spacing=1.35))
        for j, (k, v) in enumerate(rows):
            y = 3.5 + j * 0.6
            s.add(Rect(x + 0.35, y, pw - 0.7, 0.012, fill=LINE),
                  T(x + 0.35, y + 0.05, 1.6, 0.5, k, size=13, color=MUTED, valign="middle"),
                  T(x + 1.95, y + 0.05, pw - 2.3, 0.5, P(Run(v, bold=True)), size=14, valign="middle"))
    s.add(T(MX, 6.3, CW, 0.4, "演示视频约 4 分钟，终端部分是真实运行的录制，等待过程做了加速并在画面上标出实际耗时。", size=12, color=MUTED))
    slides.append(s)

    # 9 ---------------------------------------------------------------- security
    s = page("安全设计", 9, "安全团队最关心的一页。讲完直接翻下一页，主动说明风险。")
    table(s, MX, 1.45, [2.3, 9.83], [
        ["关注点", "设计"],
        ["部署位置", "全部在你自己的 AWS 账号，由 CloudFormation 管理，可以完整卸载"],
        ["身份认证", "Cognito 用户池，关闭自助注册。人用 OAuth 加 PKCE 登录，系统用各自的 machine client"],
        ["任务归属", "每个人只能查询和下载自己提交的任务"],
        ["任务隔离", "每个任务一个 microVM，任务结束 5 分钟后回收"],
        ["任务的 AWS 权限", "默认没有。需要时按最小范围开启：只看配置、只读，或自定义策略。显式拒绝 IAM、AssumeRole 和删除类操作"],
        ["服务自身的数据", "任务不能扫描任务表、下载产物 bucket、读取本服务的日志"],
        ["数据保护", "S3 和 DynamoDB 加密，仅 TLS，禁止公开访问。产物 90 天过期，日志保留 30 天"],
        ["凭证", "保存在 Secrets Manager。日志、任务记录和产物在写出前做脱敏"],
    ], [0.42] + [0.6] * 8, size=13)
    slides.append(s)

    # 10 --------------------------------------------------------------- risks
    s = page("需要了解的风险和建议", 10, "主动讲风险比被问出来好。每一条都有对应的建议做法。")
    end = table(s, MX, 1.45, [6.3, 5.83], [
        ["风险", "建议"],
        ["任务的 prompt 和 agent 读到的代码会发送到 Kiro 服务做推理", "使用前确认 Kiro 的数据处理条款，决定哪些仓库可以接入"],
        ["agent 可以在 microVM 内执行任意命令并访问公网。任务读到的内容里如果有 prompt injection，可能外传 API key 和 Git token",
         "只处理可信的仓库。Git token 用 fine-grained token，最小权限，设置有效期"],
        ["出站网络不受限制，当前版本不支持部署到 VPC", "敏感仓库暂不接入"],
        ["所有任务共用一个 Kiro API key", "为这个服务单独申请 key，并确认订阅条款允许多人共用"],
        ["验证和执行使用同一个模型，验证通过不等于一定正确", "保留人工评审，目标仓库开启分支保护"],
    ], [0.42, 0.72, 1.0, 0.62, 0.62, 0.72], size=13, first_col_bold=False)
    s.add(T(MX, end + 0.25, CW, 0.6,
            "本项目以示例代码的形式提供，用于概念验证和评估。数据流向、权限边界和上线前检查清单见 docs/security.md。",
            size=12, color=MUTED))
    slides.append(s)

    # 11 --------------------------------------------------------------- cost
    s = page("成本：主要是模型用量", 11, "数字是测试环境的实测，客户的数字取决于任务大小和所选模型。")
    cw = (CW - 2 * 0.3) / 3
    stats = [("约 $1 / 月", "没有任务时", "基础设施的固定费用"),
             ("< $0.05", "单次运行的小任务", "问答、小函数、小改动"),
             ("$2 – 2.5", "闭环的代码任务", "多文件改动加测试，20 到 30 分钟")]
    for i, (num, title, body) in enumerate(stats):
        x = MX + i * (cw + 0.3)
        s.add(Rect(x, 1.5, cw, 1.95, fill=PANEL, radius=0.12),
              T(x + 0.3, 1.62, cw - 0.6, 0.75, P(Run(num, bold=True, color="D97B00")), size=32, valign="middle"),
              T(x + 0.3, 2.42, cw - 0.6, 0.4, P(Run(title, bold=True)), size=16, valign="middle"),
              T(x + 0.3, 2.86, cw - 0.6, 0.4, body, size=13, color=MUTED, valign="middle"))
    end = table(s, MX, 3.75, [2.3, 3.6, 1.6, 1.55, 1.45, 1.63], [
        ["每月估算", "每天的任务", "Kiro", "AgentCore", "其他 AWS", "合计"],
        ["没有任务", "—", "0", "0", "约 $1", "约 $1"],
        ["小团队试用", "20 个小任务 + 5 个闭环代码任务", "$255", "约 $12", "约 $5", "约 $270"],
        ["接入 CI", "50 个闭环代码任务", "$2,420", "约 $115", "约 $10", "约 $2,550"],
    ], 0.52, size=13)
    s.add(T(MX, end + 0.22, CW, 0.75,
            "按每月 22 个工作日、每个 credit 0.04 美元估算。实测中约 94% 的费用是 Kiro credits，AgentCore 计算约 0.25 美元 / 任务小时。"
            "数据来自 2026 年 11 月的测试环境，估算方法见 docs/cost.md。", size=12, color=MUTED, line_spacing=1.35))
    slides.append(s)

    # 12 --------------------------------------------------------------- install & ops
    s = page("安装和运维", 12)
    lw = 5.9
    s.add(Rect(MX, 1.5, lw, 5.2, fill=PANEL, radius=0.12),
          T(MX + 0.35, 1.72, lw - 0.7, 0.45, P(Run("安装：CloudShell 里一条命令", bold=True)), size=18, valign="middle"),
          Rect(MX + 0.35, 2.35, lw - 0.7, 0.6, fill=DARK, radius=0.08),
          T(MX + 0.55, 2.35, lw - 1.1, 0.6, P(Run("$ ./install.sh", mono=True, color="E6EDF7")), size=15, valign="middle"),
          T(MX + 0.35, 3.15, lw - 0.7, 0.4, "向导依次完成 8 步，全程只用浏览器，约 30 分钟：", size=13, color=MUTED))
    steps = ["检查运行环境", "配置 Kiro API key", "配置 Git 凭证（可跳过）", "准备部署工具",
             "创建安装器", "构建并部署服务", "创建登录用户", "冒烟测试"]
    for i, name in enumerate(steps):
        col, row = divmod(i, 4)
        s.add(T(MX + 0.35 + col * 2.7, 3.7 + row * 0.5, 2.6, 0.45,
                P(Run(f"{i + 1}  ", bold=True, color="D97B00"), name), size=13, valign="middle"))
    s.add(T(MX + 0.35, 5.9, lw - 0.7, 0.6, "有经验的团队也可以在本机用 CDK 部署，见 docs/deployment.md。", size=12, color=MUTED))
    rx = MX + lw + 0.4
    s.add(T(rx, 1.6, W - MX - rx, 5.0,
            P(Run("升级　", bold=True), "再次运行 ./install.sh，沿用当前设置", bullet=True, space_after=14),
            P(Run("可观测　", bold=True), "CloudWatch 日志、trace 和指标：任务数、时长、轮次、credits",
              bullet=True, space_after=14),
            P(Run("通知　", bold=True), "任务结束时向 EventBridge 发布 TaskCompleted 事件", bullet=True, space_after=14),
            P(Run("并发　", bold=True), "受账号的 AgentCore 配额限制，可以申请提额", bullet=True, space_after=14),
            P(Run("用量　", bold=True), "任务表记录每个任务的提交者、模型和 credits", bullet=True, space_after=14),
            P(Run("卸载　", bold=True), "删除两个 CloudFormation stack，数据类资源保留待手动清理", bullet=True),
            size=15, line_spacing=1.35))
    slides.append(s)

    # 13 --------------------------------------------------------------- PoC plan
    s = page("建议的下一步：两周概念验证", 13, "以一个具体的时间和负责人结束：谁选仓库，哪天装环境。")
    cw = (CW - 2 * 0.3) / 3
    plan = [
        ("第 1 到 2 天　准备", ["确认 Kiro 的条款和可以接入的仓库", "在沙箱账号里安装",
                         "准备 3 到 5 个有代表性的任务和验收标准"]),
        ("第 3 到 8 天　运行", ["每类任务运行 5 到 10 次", "记录验收通过率、耗时和 credits", "接入一次 CI 或事件通知"]),
        ("第 9 到 10 天　评估", ["对照成功标准做判断", "整理安全评审的问题和结论", "决定是否扩大范围"]),
    ]
    for i, (title, items) in enumerate(plan):
        x = MX + i * (cw + 0.3)
        s.add(Rect(x, 1.5, cw, 2.75, fill=PANEL, radius=0.12),
              T(x + 0.3, 1.68, cw - 0.6, 0.45, P(Run(title, bold=True)), size=17, valign="middle"),
              T(x + 0.3, 2.3, cw - 0.6, 1.8, *[P(it, bullet=True, space_after=6) for it in items], size=14, line_spacing=1.3))
    s.add(T(MX, 4.55, CW, 0.45, P(Run("建议的成功标准", bold=True)), size=17, valign="middle"),
          T(MX, 5.1, CW, 1.7,
            P("闭环任务的验收通过率，以及产出经人工评审后可以直接合入的比例", bullet=True, space_after=6),
            P("单个任务的耗时和成本在预期范围内", bullet=True, space_after=6),
            P("安全团队对数据流向和权限边界没有阻塞性的意见", bullet=True),
            size=15, line_spacing=1.3))
    slides.append(s)

    # 14 --------------------------------------------------------------- references
    s = page("资料", 14)
    table(s, MX, 1.5, [4.0, 8.13], [
        ["文档", "内容"],
        ["README.md", "项目概览和快速开始"],
        ["docs/cloudshell-install.md", "在 CloudShell 中一步步安装、登录、提交第一个任务、升级和卸载"],
        ["docs/kac.md", "kac 命令、退出码和错误码"],
        ["docs/security.md", "数据流向、权限边界、已知风险、责任划分、上线前检查清单"],
        ["docs/cost.md", "费用构成、实测数据、月度估算"],
        ["docs/deployment.md", "本机部署、部署参数、日志和指标、并发配额"],
        ["kiro-cli-on-agentcore-demo.mp4", "演示视频，约 4 分钟"],
    ], 0.56, size=13)
    slides.append(s)

    assert all(isinstance(x, Slide) for x in slides) and H > 0 and BLUE  # noqa: S101
    return slides
