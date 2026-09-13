# RealtimeGame 最终任务包接入记录

## 当前版本与范围

2026-09-08 按用户要求，用 `RealtimeGame(1).zip` 整套替换原来的 107 道任务。
当前版本是 **69 道**，全部属于 `realtime_gui_bench`，不补齐跳号、不另分变体组。68 道按新包编号导入；Random Enemy 按用户转达的学长意见，任务编号确认为 **D1**。其压缩包文件夹前缀仍为 C2，保留这一来源差异记录，不按该前缀改归 C 类。

当前相对最新原包的唯一网页改动：按用户要求，删除 D1 底部重复的次数小字及其专用显示代码；开始/重试提示、三次机会、玩法、时序和评分字段不变。其余 68 个 HTML 与原包逐字节一致，原 ZIP 和原始验收报告未改写。

当前状态：**最新版 69 道已完成全量文件核对、逐题初始化、首次/第二次/第三次成功与三次失败的交互和评分落盘验收**。每道均有四条完整路径的通过证据；具体方法和边界见文末“本轮全量复查”。这不代表随机游戏的验收脚本每次都能成功，更不是模型成绩或 69 道 Docker 全流程测试。下方早期补测记录保留其当时的未完成状态，不作为当前覆盖范围。

源包 SHA-256：`257ae6b195d344bf99120780f1077a1160f7e7a4754beae600cd837347676216`。

| 类别 | 数量 | 编号 |
| --- | ---: | --- |
| A | 22 | A1、A12、A14、A16、A17、A21、A23、A24、A31、A34、A35、A36、A37、A38、A39、A40、A41、A42、A43、A44、A45、A46 |
| B | 12 | B1、B4、B7、B14、B15、B20、B23、B32、B36、B37、B38、B39 |
| C | 17 | C1、C3、C12、C26、C27、C28、C29、C30、C31、C32、C33、C34、C35、C36、C37、C38、C39 |
| D | 18 | D1、D3、D4、D7、D14、D15、D23、D27、D28、D29、D30、D31、D32、D33、D34、D35、D36、D37 |

新包原路径为 `RealtimeGame/d/C2_Random_Enemy/index.html`：外层目录是 `d`，文件夹前缀却是 `C2`；页面规则标题为 `Random Enemy`，内部 `BENCH.task` 为 `random_enemy`。导入时沿用上一版 107 题中该游戏的编号 **D1**；用户随后转达学长意见，确认该任务应为 D1。因此保留当前 D1 任务编号、UUID 和 D 类统计，不需要重新编号。这里的任务编号是本项目清单的 `benchmark_id`，不是网页或 OSWorld 另行提供的官方编号。网页来自最新原包，后续仅有上述获准的底部文字删除。
其余 68 道按新包的 `<类别>/<编号>_<名称>/index.html` 解析编号。描述性名称不用于生成 OSWorld UUID。
网页内部的 `BENCH.task` 是游戏作者的内部名称，不要求与外部编号字符串相等。

## 文件位置

- 网页：`evaluation_examples/websites/realtime_gui_bench/games/<小写编号>/index.html`
- 完整 OSWorld 任务：`evaluation_examples/examples/realtime_gui_bench/<UUID>.json`
- 运行清单：`evaluation_examples/test_realtime_gui_bench.json`
- 导入程序：`scripts/python/generate_realtime_gui_bench.py`
- 浏览器初始化验收：`scripts/python/validate_realtime_gui_bench.py`
- 评分读取：`desktop_env/evaluators/getters/realtime_gui.py`
- 单分数 metric：`desktop_env/evaluators/metrics/realtime_gui.py`
- 详细结果落盘：`lib_run_single._evaluate_with_details()`
- 汇总：`lib_results_logger.update_realtime_gui_bench_summary()`

本次包只含 69 个自包含 HTML，没有附带 `LANDING_SPEC.md`、CSV 或网页测试文件。
不将旧包的 README、memory、test 文件混入新网页目录。
保留 UUID 生成规则：对 `https://os-world.github.io/realtime-gui-bench/<小写编号>` 做 UUIDv5。
同编号的新旧版会有相同 UUID，**新实验必须使用新的 result_dir，不得复用旧实验目录**。

## Instruction 与 Config

所有任务统一使用已经确认的 instruction：

> 阅读当前页面中的游戏规则，并按照规则完成游戏。如果当前尝试失败且页面允许继续尝试，请继续完成后续尝试。游戏显示成功，或者所有尝试机会均已用完后，请立即结束任务并停留在当前页面。

保留完整 OSWorld 字段：`id`、`benchmark_id`、`snapshot`、`instruction`、`source`、`config`、`trajectory`、`related_apps`、`evaluator`、`proxy`、`fixed_ip`、`possibility_of_env_change`。

初始化复用现有 Config 类型，不增加游戏专用初始化函数：

1. `upload_file`：上传 HTML 到 VM 的 `/tmp/realtime_gui_bench/<编号>/index.html`。
2. `launch`：启动 `python3 -m http.server 8765 --directory /tmp/realtime_gui_bench`。
3. `launch`：启动 Chrome 调试端口 1337。
4. `launch`：运行 socat，转发 VM 的 9222 到 1337。
5. `chrome_open_tabs`：打开 `http://127.0.0.1:8765/<编号>/index.html`。
6. `activate_window`：激活 Google Chrome。
7. `sleep`：等待 3 秒。Config 不点击 Start/Reveal，不提前开始游戏。

网页不需要外网：`proxy=false`、`fixed_ip=false`、`possibility_of_env_change=low`。
模型接入、动作空间、Prompt、WAIT、API 错误处理不属于本次替换范围，保持原状。

## 评分口径

唯一游戏结果来源仍是 `window.BENCH`；不从 `__dbg()` 中取答案作为正式评分。
`__dbg()` 可在无模型验收中作为只读诊断信息；自动正确轨迹不代表模型完成能力。

2026-09-08 核对新版 69 个 HTML：`BENCH.attempts` 初始为 0，每次失败结算时加 1，成功分支不增加，实际含义是**已失败次数**，不是“当前正在第几次尝试”。
`BENCH.passed` 表示整局是否通过。当前评分采用：

```python
pass_at_3 = float(passed)
pass_at_1 = float(passed and attempts == 0)
```

| 已结算情况 | passed | attempts | pass_at_1 | pass_at_3 |
| --- | --- | ---: | ---: | ---: |
| 未开始或第一次尚未完成 | false | 0 | 0 | 0 |
| 第一次通过 | true | 0 | 1 | 1 |
| 失败一次后通过 | true | 1 | 0 | 1 |
| 失败两次后通过 | true | 2 | 0 | 1 |
| 三次均失败 | false | 3 | 0 | 0 |

getter 不再根据 `results` 数组中的 hit 或 true 计算分数，不再要求它每项对应一次完整尝试、不超过三项。
原始 `results` 只作为诊断数据，连同其他 BENCH 字段保存在详细结果的 `raw_bench` 内。它为空、缺失、记录子操作或采用其他编码，都不改变从 `passed/attempts` 读到的分数。

保留此前约定的双指标及 OSWorld 单标量接口：

- `pass_at_1`：第一次尝试成功为 1，否则为 0。
- `pass_at_3`：前三次中至少一次成功为 1，否则为 0。
- `env.evaluate()` 和 `result.txt`：仍只返回/保存标量 `pass_at_3`。
- `result.json`：保存 `benchmark_id`、`result`、`pass_at_1`、`pass_at_3`、`attempt_results`、`status`，另保存原始状态 `raw_bench` 便于核查。
- 不要求尝试机会全部用完才读取已有结果；未开始或尚未成功的有效状态可以正常得到 0。

`attempt_results` 是依据失败次数和整局通过状态**推导的已完成尝试历史**：`[False] * attempts + ([True] if passed else [])`。
未完成的当前尝试不补写为失败，也不把这份推导结果写回网页。它不是原始 `BENCH.results` 的拷贝。

例如 A41 第一局送对两张、第三张送错，第二局全部送对：原始事件有 6 条，但完整尝试只有两次。

```json
{
  "benchmark_id": "A41",
  "result": 1.0,
  "pass_at_1": 0.0,
  "pass_at_3": 1.0,
  "attempt_results": [false, true],
  "status": "passed",
  "raw_bench": {
    "task": "kitchen_order_match",
    "attempts": 1,
    "maxAttempts": 3,
    "passed": true,
    "results": ["hit", "hit", "miss", "hit", "hit", "hit"],
    "status": "passed"
  }
}
```

读取器仍校验真正的评分字段：`passed` 必须为布尔值，`attempts` 为 0～3 的整数，`maxAttempts=3`，`task` 为非空名称。
`status` 必须与它们一致：通过时为 passed；未通过且失败次数不到 3 时为 running；三次失败后为 failed。三次已失败之后再报通过不属于合法的三次机会内成功。
状态缺失或关键字段矛盾继续按原重试逻辑读取，仍无效则报错；不将这种异常当成正常 0 分。
Config 中 evaluator 的 `attempts=3` 是读取状态的重试次数，不是让游戏自动玩三次。

最终通过按用户提供的 `passed` 规范执行；首次成功指标利用已核实的失败计数补充计算。不使用 debug 答案，不需要修补 A38 的空列表，不会将 A41 送对一张订单误判成整局通过。
正式读取仍走 Chrome CDP 的 BENCH 对象；不新增 URL hash 回退或其他评分接口。

## 模型结果位置

```text
<新的 result_dir>/
├── summary/
│   ├── results.json
│   └── realtime_gui_bench_metrics.json
└── <action_space>/<observation_type>/<model>/realtime_gui_bench/<UUID>/
    ├── result.txt
    ├── result.json
    ├── traj.jsonl
    ├── screenshot*.png
    └── recording.mp4
```

`summary/results.json` 保留现有运行器的字段及行为，没有改为仅有两个指标。
其中运行记录的 `success/error` 与网页的 `running/passed/failed` 不是同一层状态。
扩展汇总 `realtime_gui_bench_metrics.json` 保留 `overall/A/B/C/D`，字段为 `tasks/scored_tasks/unscored_tasks/pass_at_1/pass_at_3`。
应有任务数从实际总清单和任务 JSON 中读取，当前为 69 / 22 / 12 / 17 / 18，不能由连续编号范围推算。
均值基于已有有效结果；`unscored_tasks = tasks - scored_tasks` 表示缺少有效成绩的任务数，包含尚未运行或未能保存有效成绩的任务，不代表已确认的运行错误。正常评出 0 分的任务也计入 `scored_tasks`。
旧字段 `error_tasks` 已更名为 `unscored_tasks`，计算方式和单题评分不变。新生成的汇总使用新名称，历史成绩与验收文件不批量改写。

四种 Agent 分别使用独立的 `--result_dir`，各自保存单题结果与汇总。同一场实验中断后续跑使用原结果目录；新版任务或重新进行一次独立实验使用新的结果目录，不需要删除旧成绩或录像。

## 替换备份与验收证据

工作与证据目录：`/mnt/zhaorunsong/yhyx/realtime-game-replacement-20260908-iZbpyP/`。

- `previous-integration.tar.gz`：替换前网页、任务清单、相关脚本/评分/测试/本文档的备份。
- `previous-games/`、`previous-task-configs/`：移出活动目录的原 107 道网页和 JSON，可恢复。
- `inventory.json`：原 ZIP 路径、SHA-256、每道源文件路径、编号和文件哈希。
- `extracted/`：原包解压件。
- `validation/`：新版初始化报告及逐题截图。
- `docker-results/`：无模型验收专用目录，与模型成绩分开。
- `a38-evidence.json`、`a38-result.png`：A38 真实键盘操作后读到的结果。

旧模型结果和录像未删除、未覆盖。本次移出的仅是本 domain 的旧任务集；独立旧 `realtime_gui` domain 不属于替换目标。

### 替换阶段验收状态（历史记录）

以下为全量试玩之前的阶段记录；当前覆盖情况以文末本轮全量复查为准。

- 69 个唯一编号、UUID、HTML 与总清单一致；初次导入时源文件哈希全部一致。当前仅 D1 有用户批准的底部文字删除，其余 68 个 HTML 哈希仍与原包一致。
- 评分与清单回归：**116 passed**，覆盖首次/第二次/第三次通过、三次失败、未开始/未结束、A38 空列表、A41 部分订单与超过三条记录、原始状态单独保存、非法关键字段拒绝、详细结果落盘、跳号汇总与移除编号排除，以及无成绩时的未评分计数、0 分任务仍计为已评分、D1 删除重复提示后保留正常提示与评分字段。
- 每道 3 次独立浏览器加载已经完成，共 207 次。3 秒 Config 同等稳定等待后，再延迟 65 秒检查：69/69 通过初始字段、可见开始入口、相位稳定和无 JS 异常检查；已浏览全量初始截图联系表。
- 浏览器验收不等于 69×3 次 Docker 完整 Config。Docker 验收另行执行，不能混称。
- 全量初始页截图、成功/失败/重试操作和 Docker 报告以实际生成的证据为准，未完成项目不能宣称全部通过。
- A31、B1、C1、D1 的 Docker Config 均能完成，并读到合法初始 0 分；操作测试尚未成功。当前诊断脚本把网页坐标直接用作桌面坐标，遗漏浏览器边框偏移，后续需修正验收脚本重跑，不能将这些超时判作游戏失败。容器已随脚本退出关闭。

### 2026-09-08 新评分实测

证据目录：`/mnt/zhaorunsong/yhyx/realtime-game-replacement-20260908-iZbpyP/scoring-wxSAaYp1/`，入口为 `report.json`，可复跑脚本为 `verify_scoring.py`。

- 69 个真实 HTML 初始页面通过正式 CDP getter + metric 读取，均得到合法初始 0 分及空的已完成尝试历史。
- A38、A41、B1 分别完成第一次通过、第二次通过、第三次通过和三次失败，共 **12 条真实键鼠操作流程**，包含 57 个中途/最终评分检查点，全部通过。
- A38 即便原始 `results=[]`，首次通过仍得到 1/1，重试后通过得到 0/1，三次失败得到 0/0。
- A41 每轮只送对部分订单时始终得到 0/0；在第三次才完成全部订单时，原始事件有 9 条，仍正确得到 pass_at_1=0、pass_at_3=1、attempt_results=[false,false,true]。
- 每条流程通过现有 `_evaluate_with_details()` 保存详细结果和汇总，并核对 `result.txt` 与 `result.json` 的标量一致。原始 BENCH 保存在 `raw_bench`，没有修改网页状态来构造分数。
- B1 的初轮验证脚本曾因等待移动目标稳定错过一秒窗口；后改为正常鼠标坐标点击后重跑通过。只修改验收动作，游戏时限未改，旧失败日志保留。
- 这是浏览器层面的正式评分链路验证，没有调用模型，也不是 Docker/模型完整基线运行。全部网页再次核对 SHA-256，与原 ZIP 一致。

### 已确认的原包差异

1. **A38 没有写入 results**：真实键盘操作到出口后，`passed=true,status="passed",results=[]`。失败时只增加 `attempts`。旧读取器曾因此报错；新版读取器使用整局状态与失败次数，已无需修改该网页。
2. `BENCH` 对象确实 69/69 存在；`window.__dbg()` 实际为 68/69，B1 没有该函数，部分其他游戏未在 debug 返回中包含全部标准字段。不阻碍正式评分，因为 evaluator 不依赖 debug 接口。
3. A1 新包已增加 Start 门控；原包 D2 本版不再包含。旧版 A1、D2 的初始化风险记录不能直接当作本版结论。
4. **A41 的 results 不是整局尝试数组**：实际依次送对三张订单，结果为 `["hit"]`、`["hit","hit"]`、`["hit","hit","hit"]`，但前两次 `passed=false,status="running"`，第三次才通过，`attempts` 始终为 0。证据在 `a41-evidence.json` 和 `a41-result.png`。不得把第一个 hit 直接当作第一次完整尝试通过；也不能因此说该游戏没有统一 status 接口。

用户确认三次内任意一次完整通过即算通过，并要求完善新版评分。上述评分适配阶段未修改 69 个网页；随后用户批准了 D1 的纯显示删减，未修改玩法、难度或计数逻辑。

验收脚本、只读诊断及截图不得写进正式 Config；未经同意不改变网页玩法、时序、难度或填造评分结果。

### 2026-09-08 Random Enemy 补测与全量验收边界复核

证据目录：`/mnt/zhaorunsong/yhyx/realtime-game-review-20260908-hUns1p/`，入口为 `report.json`，脚本为 `check_random_enemy.py`。报告同时记录原包路径及补测时的 D1 接入编号。用户随后确认 D1 为正式编号；原始验收报告保留，不改写历史记录。

- 重新核对最新 ZIP：共 69 个 HTML，只有 `d/C2_Random_Enemy` 存在原始前缀与导入编号不一致；其余 68 道一致。
- 重新浏览现有 6 张全量开局截图联系表；69 道已有三次独立浏览器加载检查，共 207 次。这仍不代表 69 道都完成了交互试玩或 Docker 全流程。
- Random Enemy 本轮覆盖首次、第二次、第三次成功及三次失败，4 条鼠标操作流程、18 个正式 getter/metric 检查点通过；重试按钮、终局画面和结果落盘正常，未出现页面 JS 错误，源 HTML 哈希未变。
- 该补测通过浏览器鼠标事件实际操作，短时敌人位置由只读 `__dbg()` 辅助定位，并查看开局、重试、成功和失败截图。它是无模型浏览器验收，不是纯截图 Agent 成绩，也不是 Docker 全流程。
- 发现显示口径不一致：底部 `Attempt N / 3` 使用已失败次数，因此第一次成功仍显示 0，第二次成功显示 1；重试提示显示的是下一次尝试编号。当前评分按已失败次数读取，不受该显示问题影响。
- 发现地址栏同步滞后：前两次失败后，BENCH.attempts 已增加，但 URL hash 仍保留 attempts=0，直到成功或三次失败才同步。正式 getter 直接读取 BENCH，因此这次评分不受影响；不能声称 URL hash 始终同步。只记录，没有修改游戏。

截至该次补测，具有首次/第二次/第三次成功和三次失败完整浏览器验收记录的是 A38、A41、B1、Random Enemy 共 4 道；其余 65 道当时尚未完成同等交互验收。之后的全量复查见下文。

### 2026-09-08 用户批准删除 D1 底部次数小字

- 删除的仅是底部 `#hint` 元素、`.hint` 样式，以及专门更新它的 `hintEl`、`setHint()` 和调用。正常的 `Attempt 1 / 3` 浮动提示、`Attempt 2 / 3` 等重试弹窗继续保留。
- `BENCH` 字段、失败时的自增、三次机会、成功判定和 URL 同步逻辑未改。先前记录的中途 URL 同步滞后不属于本次修改范围，评分仍直接读取 BENCH。
- 修改后 HTML SHA-256：`ff9793d77a6d5333116bc7587b5fcebe51c83ee088d1304c3246c0d1ebb07adb`。原始版本可从用户 ZIP 恢复；再次从原 ZIP 导入时需保留这项已批准的显示删减，不能把恢复的旧提示误认为已修复版本。
- 新证据目录：`/mnt/zhaorunsong/yhyx/realtime-game-d1-hint-removal-20260908-hyeGc2/`。4 条浏览器鼠标流程与 18 个评分检查点全部通过，覆盖首次、第二次、第三次成功和三次失败；每个检查点确认 `#hint` 不存在，开始/重试提示正确，无页面 JS 错误，结果正常保存。
- 已查看删除后的游戏中和重试截图。此次仍是只读调试辅助定位的无模型浏览器验收，不是 Docker/模型全流程，也不代表其他 65 道已完成交互验收。

## 2026-09-08 本轮全量复查

本轮证据目录：`/mnt/zhaorunsong/yhyx/realtime-bench-full-audit-20260908-yvMaP8/`。所有诊断成绩均保存在此目录，不混入模型实验结果。此次没有修改网页、正式 Config、评分代码、Agent 或动作适配器；仅新增/调整任务外的验收脚本并更新本文档。

### 最新版本与初始化

- `source_audit.json`：重新逐项核对 ZIP、活动网页、完整任务 JSON 和总清单，确认为 **69 道（A22/B12/C17/D18）**，编号与 UUID 唯一；正式 Config 与当前生成规范一致。活动任务集没有多出的旧编号或旧网页。
- 68 个 HTML 与最新 ZIP 逐字节一致；D1 仅保留用户此前批准的底部次数显示删除，差异见 `approved-d1-diff.txt`。整个试玩后再次核对，哈希未变。
- 旧 107 道的备份、历史模型成绩及独立旧 `realtime_gui` domain 仍保留，但不在本次 69 题运行清单内。“没有混入旧任务”不表示删除电脑上所有历史文件。
- `initialization/initial_state_report.json`：全部 69 道各做 **3 次独立浏览器上下文加载，共 207 次**。先等待与 Config 相同的 3 秒，再额外等待 65 秒，检查初始 BENCH、游戏相位、可见规则/开始入口以及 JS 错误；207 次均通过，未提前开始或消耗尝试。
- 已逐页复核全部开局截图联系表，以及全部游戏首次成功和三次失败的最终截图联系表（`initialization/contact_sheets/`、`visual-review/`）。没有发现规则、开始按钮或最终结果被游戏自身布局遮挡。

这里的 207 次是浏览器冷加载验收，不是 207 次 VM 启动，也没有将重复验收写入 Config。

### 每道游戏的真实交互与评分

逐题总报告：`full_gameplay_audit.json`，其中每个编号均链接到独立 `play/<批次>/<编号>/report.json`，内含按键/鼠标轨迹、截图、读分检查及保存位置。

每道均完成以下四种完整路径：

| 路径 | 最终 attempts | pass_at_1 | pass_at_3 / result.txt |
| --- | ---: | ---: | ---: |
| 第一次成功 | 0 | 1 | 1 |
| 失败一次后成功 | 1 | 0 | 1 |
| 失败两次后成功 | 2 | 0 | 1 |
| 三次全失败 | 3 | 0 | 0 |

共 **276 条完整路径、1518 个正式读分检查点**。除最终结局外，还检查未开始、每次失败后、等待 Next 期间、点击 Next 后的后续尝试以及终局稳定等待后的状态。全部 69 道均有四路径完整通过记录；这些通过记录未出现页面 JS 错误。

方法是 Playwright 发送真实鼠标/键盘事件，并用只读 DOM / `__dbg()` 辅助选择位置、路线和时机。没有调用游戏成功函数，没有写入 BENCH，没有修改随机数、时间、答案或难度。它验证网页可操作、可重试、可正确评分，**不是仅凭截图推理的模型试玩，也不能作为四种 Agent 的实验成绩**。

这轮没有发现需要修改网页或正式评分适配的阻塞问题。特别是 A38 的空 results 和 A41 的多条订单记录，都已在四条完整路径中验证，不影响基于 passed/attempts 的双指标。

### 落盘与汇总

- 逐一核对 276 份实际 `result.json`、对应 `result.txt` 及最终读分对象，标量、双指标、推导的尝试历史和原始 BENCH 一致。
- `consolidated-results/<路径>/` 汇集已有验收成绩，并通过真实结果记录器重建四份完整 69 题汇总：首次成功组为 1/1；第二次、第三次成功组为 0/1；三次失败组为 0/0。每份均为 `scored_tasks=69, unscored_tasks=0`，A/B/C/D 数量正确。
- 汇集目录仅为验收产物，不是新模型测量。浏览器诊断目录中的 `computer_13/screenshot/scripted_acceptance` 是结果布局标签；实际输入由上述 Playwright 脚本执行，**不能据此声称已经验证 computer_13 的 Prompt 或解析器**。
- 本轮再次运行 getter、metric、结果落盘和任务清单四组回归测试：**116 passed**；两个现有依赖弃用/版本提示不影响测试结果。

### Docker 完整链路与录像

`docker_verified_report.json` 汇总四类代表任务：A31、B1、C36、D1。均执行各自正式完整 Config，在真实 Docker/QEMU 桌面中通过 `env.step()` 发送 PyAutoGUI 操作，从初始合法 0 分到首次成功 1 分，并核对详细结果、标量、轨迹截图、MP4 和 FFmpeg 日志落盘。

- A31、B1、D1 的有效操作证据来自 `docker_report_v2.json`。该轮最初调用 ffprobe 时缺少动态库，因而原报告标为需要复查；并非录像未生成。现已用 OpenCV 独立解码原文件，核对时长与最终画面，原报告保留不改写。
- C36 的有效证据来自 `docker_report_v3.json`。在成功后继续保持最终页 420 秒；测得录制墙钟约 422.28 秒，MP4 为 **424.2 秒、12726 帧、30 FPS**，中段与末帧可解码，最终仍是成功页，FFmpeg 日志正常结束并下载保存。没有修改原录制 FPS。该测试主要保持静止终局，不等同于数小时动态任务的全时长压力验证。
- 已查看四段录像的起始/最终画面联系表 `docker-recording-contact-sheet.jpg`。真实 Chrome 偶尔有右上角更新提示、翻译提示，但未遮挡这四道的中央规则、开始或结果区域；它们是浏览器环境提示，不是网页改版。本轮未修改 Chrome 配置或游戏来隐藏它们。
- 早期验收脚本的网页/桌面坐标偏移遗漏已在任务外脚本中校准；C36 若在 Start 后先等待整次截图往返再按键，会错过短窗口，因此在同一动作块内点击 Start、等待 0.35 秒、按 J。此处是验收动作安排，不是延长游戏时间或修补 Agent。

以上是 **4 道代表任务的完整 Docker 检查**，不能描述成 69 道均已在 Docker 中完成四种结局。全量 69 道的交互、重试与读分覆盖来自前述浏览器验收。

### 试跑异常与稳定性边界

首轮调试不是全部一次通过。失败日志、截图和原报告都保留；`full_gameplay_audit.json` 明确列出每道采用哪次完成四条路径的复查记录，不能把它解释成自动操作成功率 100%。

- A31、B14、B15、B23 等验收动作最初选错按钮、按键或时间窗口；只修改验收脚本后，四路径通过。
- A38 的移动需等待浏览器动画实际推进；D14 的连续鼠标轨迹需在限时内完成。减少脚本往返开销/等待实际动画后通过，网页时限和玩法未改。
- C1、C3 曾在多个浏览器同时录屏时跳跃失败，相同操作在关闭诊断视频、降低并发后通过，并进行了额外复查。这表明测试负载会影响实时操作结果；没有据此认定任意机器或并发下都等效，也没有把负载问题通过修改游戏隐藏。
- 对 C1、C3、D14、D27、D28、D31、D32、D34、D35、D36 另做一批四路径重复（`play/stability_repeat/`），40 条中 39 条按预期完成。D35 的第三次成功路径实际变为三次失败，页面记录与失败画面一致；该条原始记录保留，不能计作预期路径通过。
- D35 的判定依赖越界瞬间的“当前最大缺口”。只在某一时刻对准缺口并不保证移动过程中该缺口仍最大。复查脚本增加等待缺口较宽且相对其他缺口有余量的条件；这是调整实际键盘操作策略，没有调用网页的自动移动调试入口或修改判定。随后又独立执行三轮四路径复查，**12/12 按预期完成**，结果保存于 `play/d35_gap_margin_1/`、`play/d35_gap_margin_2/`、`play/d35_gap_margin_3/`。这不抹去先前失败，也不证明任意随机轨迹都可由同一脚本稳定通过。

本轮检查已结束，验收创建的 Docker VM 与浏览器已关闭，原有无关容器、旧备份、历史成绩和录像未删除。

有限次数验收不能证明所有随机种子、系统负载、浏览器版本或未来 Agent 输出均无问题。当前结论是：最新 69 道均已有完整开局、三次机会、四种结局、正确评分与保存的可复查证据，未发现需要改游戏才能接入的阻塞问题；随机实时操作是否成功仍受策略与执行时机影响。
