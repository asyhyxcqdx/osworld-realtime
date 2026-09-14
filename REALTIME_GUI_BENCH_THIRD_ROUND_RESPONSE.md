# Realtime GUI Bench 第三轮交流项回复

日期：2026-09-14
适用版本：`realtime-gui-bench/1.1`、`realtime-gui-bench-anti-cheat/1.1`

感谢迁移前的最后一轮核对。以下答案作为交付和自测约定。

## Q10. `test.mjs` 运行环境

OSWorld 的验收环境满足以下约定：

- Node.js 使用 **22 或更高版本**；当前环境为 Node 24；
- Chrome 使用 `chrome-headless-shell`，当前路径为 `/usr/local/bin/chrome-headless-shell`；脚本保留你们的 `CHROME` → PATH → 常见路径回退逻辑；
- 允许使用 `127.0.0.1` 回环网络和动态端口；
- `--headless`、`--remote-debugging-port`、`--no-sandbox`、临时 `--user-data-dir` 和 `--disable-gpu` 均可使用；
- 每个测试脚本使用独立临时 profile 和独立端口；不得复用其他游戏的 profile；
- 单个游戏测试设置 180 秒超时；超时记为该游戏验收失败并记录原因；
- 69 个测试不要求嵌入每次模型实验。协议迁移验收阶段可串行运行；资源允许时最多并行 4 个，避免同时启动过多 Chrome 实例。

## Q11. `test.mjs` 输出和调用方式

每个游戏目录保留一个可独立运行的 `test.mjs`，调用方式为：

```bash
node test.mjs
```

脚本可以继续输出人类可读日志并使用退出码表示整体成败。为便于汇总，脚本还必须在标准输出最后打印一行机器可读 JSON：

```text
RESULT_JSON={"benchmark_id":"A1","paths":{"ready":true,"pass1":true,"pass3":true,"fail3":true},"ok":true,"errors":[]}
```

字段要求：

- `benchmark_id`：大写外部任务编号；
- `paths.ready/pass1/pass3/fail3`：四条路径是否通过；
- `ok`：四条路径全部通过时为 `true`；
- `errors`：字符串数组，记录失败原因。

OSWorld 提供根目录批量包装脚本，逐目录执行 `node test.mjs`，收集退出码和 `RESULT_JSON`，输出 `realtime_game_test_report.json`。游戏制作侧不需要再维护第二套批量入口。

## Q12. `README.md` 模板

每个游戏的 `README.md` 至少包含以下固定字段：

```markdown
# <benchmark-id> <英文名>

- benchmark_id: A1
- category: A
- BENCH.task: clockface
- interface_protocol: realtime-gui-bench/1.1
- anticheat_protocol: realtime-gui-bench-anti-cheat/1.1
- test_command: node test.mjs
- test_environment: Node >= 22, Chrome CDP over WebSocket
- test_result: PASS or FAIL
- test_date: YYYY-MM-DD
- known_limitations: none

## 原有玩法

简要说明原游戏规则和操作，不要在这里增加新的玩法。

## 测试结果

记录 ready、pass1、pass3、fail3 四条路径及错误信息。
```

不要求每个游戏单独提交快捷键阻断文档。环境会提供一份覆盖全部游戏的快捷键报告；README 只记录协议版本、测试命令和该游戏的测试结果。

## Q13. 反作弊提交材料

修订后每个游戏需要交付：

- `index.html`；
- 与其同目录的 `test.mjs`；
- `README.md`，使用 Q12 模板；
- 测试脚本中的 `RESULT_JSON` 和退出码。

信息泄露、刷新/导航/重置和状态篡改测试结果直接并入 `test.mjs` 的四条路径或错误字段，不再要求另写一份逐游戏反作弊文档。环境级 F5/F12/源码/地址栏阻断由 OSWorld 统一出具报告。

## Q14. 交付清单格式

确认同时提供以下两份文件，放在压缩包根目录：

```text
realtime_gui_bench_manifest.csv
realtime_gui_bench_manifest.md
```

CSV 是机器读取的主清单，至少包含列：

```text
benchmark_id,source_directory,BENCH.task,category
```

Markdown 是供人工审阅的同内容表格。`source_directory` 指向包含 `index.html`、`test.mjs` 和 `README.md` 的游戏目录。目录名使用已确认的 `<benchmark-id>_<英文名>` 格式。

## Q15. 离线 checker 和 changelog

OSWorld 会在开始迁移前、与两个 `1.1` 协议文件同一批次提供：

```text
scripts/python/check_realtime_gui_contract.py
REALTIME_GUI_BENCH_PROTOCOL_CHANGELOG.md
```

checker 的单文件调用方式：

```bash
python scripts/python/check_realtime_gui_contract.py \
  --input bench.json \
  --benchmark-id A1
```

批量调用方式：

```bash
python scripts/python/check_realtime_gui_contract.py \
  --input-dir games \
  --manifest realtime_gui_bench_manifest.csv \
  --output realtime_gui_contract_report.json
```

输出结构至少为：

```json
{
  "benchmark_id": "A1",
  "scored": true,
  "status": "passed",
  "errors": []
}
```

`REALTIME_GUI_BENCH_PROTOCOL_CHANGELOG.md` 会逐条记录：

- `running` 包含等待下一次重试入口；
- 刷新/导航按 `run_id` 由环境判定运行失效；
- `__dbg()` 可作为测试专用只读接口；
- “GUI 可达”不要求清理客户端源码内部常量；
- 快捷键阻断和拒绝轨迹由环境负责；
- `pass_at_1/pass_at_3` 是同一 run 内三次机会；
- `evaluation_settle_s` 默认 3 秒；
- Agent 未完成与环境/接口无效的统计区分。

## 迁移开始条件

游戏制作侧在收到以下文件后开始迁移：

1. 两个 `1.1` 正式协议文件；
2. 离线 checker；
3. 协议 changelog；
4. 环境级快捷键阻断说明。

在这些文件到位前，继续保持 69 个游戏代码冻结。
