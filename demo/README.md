# 演示材料

给客户介绍这套方案时用的材料，以及重新生成它们的脚本。

| 材料 | 位置 | 说明 |
|---|---|---|
| 方案介绍 PPT | `deck/build/kiro-cli-on-agentcore.pptx`（同时生成 PDF） | 14 页，文字和图形都可以编辑，每页有讲解备注 |
| 演示视频 | `video/build/kiro-cli-on-agentcore-demo.mp4` | 约 4 分钟，1080p，中文配音和字幕。终端部分是真实运行的录制 |
| 示例任务 | `legacy/` | 演示 2 用的 Python 2 脚本和验收标准 |

`build/` 目录不进版本库。

## 现场演示脚本（约 15 分钟）

前提：服务已经部署，本机已经 `kac login`。部署后先运行几次 `kac models` 预热（见 [docs/deployment.md](../docs/deployment.md)）。

| 时间 | 做什么 | 讲什么 |
|---|---|---|
| 0–2 分钟 | PPT 第 2、3 页 | 团队用 agent 的三个问题，方案是什么 |
| 2–4 分钟 | PPT 第 4 页（架构） | 按 ①②③ 讲调用路径；指出唯一出账号的数据流 |
| 4–7 分钟 | 演示 1（下方命令） | 提交后拿到任务编号就可以离开；结果可以下载，可以在本地复验 |
| 7–12 分钟 | 演示 2（下方命令） | 闭环的四个阶段；等待时讲 PPT 第 6 页；完成后打开 `output.md` 看每条验收标准的证据 |
| 12–15 分钟 | PPT 第 9、10、11 页 | 安全设计、需要了解的风险、成本 |

演示 1，单次运行，约 1 分钟：

```bash
kac models | head -7
T=$(kac submit "写一个 Python 函数 decode_float32(registers)：把 Modbus 保持寄存器（大端，每两个寄存器一个值）解码成 float32 列表，寄存器个数为奇数时抛出 ValueError。附带 pytest 单元测试并运行通过。把 modbus_decode.py 和 test_modbus_decode.py 放到 ARTIFACTS_DIR。")
kac wait "$T" --interval 5
kac result "$T" --out result && ls result/artifacts
python3 -m pytest -q result/artifacts
```

演示 2，闭环加独立验证，约 3 分钟：

```bash
cd demo/legacy
grep -n "print\|urllib2\|iteritems\|except\|raise" plc_report.py
T=$(kac submit "把 context 里的 plc_report.py 从 Python 2 迁移到 Python 3.12，保持行为不变，并补充 pytest 单元测试。把迁移后的脚本、测试文件和 MIGRATION.md 放到 ARTIFACTS_DIR。" \
    --context-file plc_report.py --ac-file acceptance.txt --loop-mode process)
kac wait "$T" --interval 5
kac result "$T" --out migrated && ls migrated/artifacts
less migrated/output.md
```

现场演示的注意事项：

- 网络或 Kiro 服务不稳定时任务可能变慢或失败。准备好演示视频作为备份。
- 演示 2 的验收标准每次由 agent 另外推导 1 到 3 条，所以通过的条数每次可能不同（例如 6/6 或 7/7）。
- 0.3.0 之前的版本在闭环模式下不接受带全角标点的验收标准，`acceptance.txt` 因此没有用全角标点。

## 重新生成演示视频

```bash
python3 -m pip install playwright imageio-ffmpeg boto3 && python3 -m playwright install chromium
python3 demo/video/tts.py       # 配音：Amazon Polly，每句一个音频，按内容缓存
python3 demo/video/record.py    # 终端录制：对已部署的服务运行两个真实任务，约 5 分钟，消耗约 5 个 credits
python3 demo/video/render.py    # 合成：build/kiro-cli-on-agentcore-demo.mp4，约 15 分钟
```

- 画面是 `video/player.html`，它根据时间画出每一帧；直接用浏览器打开可以预览（需要先运行过一次 `render.py`）。
- 旁白和字幕在 `video/storyboard.json`。改了文字后重新运行 `tts.py` 和 `render.py`，终端录制不用重做。
- 英文名词的读法在 `storyboard.json` 的 `pronunciations` 里。
- `record.py` 需要 kac 能通过认证：先 `kac login`，或者设置 `KAC_CLIENT_ID` 和 `KAC_CLIENT_SECRET`（见 [docs/kac.md](../docs/kac.md)）。
- 终端里长时间的等待在视频里做了加速，画面右上角会标出实际耗时。
- `python3 demo/video/render.py --no-voice` 生成只有字幕的版本；`--still 42.5 out.png` 只渲染一帧，用来检查版式。

## 重新生成 PPT

```bash
python3 -m pip install python-pptx playwright && python3 -m playwright install chromium
python3 demo/deck/build_deck.py
```

- 内容在 `deck/deck_content.py`。"演示"那一页的数字取自 `video/build/casts/` 里的终端录制。
- 脚本同时生成 PPTX 和一份版式相同的 PDF、PNG 预览；文字放不下时会打印 `OVERFLOW`。
- 预览用的是 Noto Sans CJK，PPTX 里指定的是微软雅黑。在 PowerPoint 里打开后，个别位置的换行可能略有不同。
- 架构图来自 `video/architecture.svg`，README 里的 `docs/images/architecture.png` 也是它。
