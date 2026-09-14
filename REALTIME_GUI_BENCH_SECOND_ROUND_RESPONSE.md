# Realtime GUI Bench 第二轮开放问题回复

日期：2026-09-14
适用版本：`realtime-gui-bench/1.1`、`realtime-gui-bench-anti-cheat/1.1`

感谢第二轮迁移前核对。以下为 OSWorld 侧的确定答案。

## A. 交付与接入

### Q1. `benchmark-id` 和目录对应关系

`benchmark-id` 由 OSWorld 维护并分配，当前使用 `A1`–`A46`、`B1`–`B39`、`C1`–`C39`、`D1`–`D37` 中实际存在的 69 个编号。游戏制作侧不得自行重新编号。

交付目录可以直接按以下形式提供：

```text
<benchmark-id>/
├── index.html
├── test.mjs
└── README.md
```

OSWorld 会把该目录挂载为：

```text
http://127.0.0.1:8765/<benchmark-id>/index.html
```

交付清单必须至少包含以下映射：

```text
benchmark_id | source_directory | BENCH.task | category
```

其中 `BENCH.task` 可以继续使用游戏内部名称，不要求等于 `benchmark_id`。`category` 使用 `A`、`B`、`C`、`D`。

### Q2. `test.mjs` 和其他文件

确认：`test.mjs` 必须和被测 `index.html` 位于同一游戏目录。每个游戏应交付：

- `index.html`；
- `test.mjs` 或等价的离线浏览器验收脚本；
- `README.md`，记录协议版本和测试结果。

`memory.md` 不是 OSWorld 接口要求，可以不交付。若保留，只作为制作侧辅助材料。

OSWorld 会独立运行制作侧测试，并运行自己的全量 checker。测试脚本可以按“环境变量 `CHROME` → PATH → 常见本地路径”的顺序查找 Chrome；OSWorld 不要求写死某个绝对路径。

## B. 校验与验收

### Q3. 离线 checker 和断言清单

OSWorld 会在游戏批量迁移前提供可离线运行的契约 checker。输入为浏览器 CDP 读取的 `window.BENCH` JSON，输出至少包括：

```json
{
  "benchmark_id": "A1",
  "scored": true,
  "status": "passed",
  "errors": []
}
```

checker 校验：

1. 八个必需字段、类型和值域；
2. `protocol_version` 是否为 `realtime-gui-bench/1.1`；
3. `max_attempts === 3`；
4. `0 <= attempts_completed <= 3`；
5. `passed === (pass_at_3 === 1)`；
6. `ready/running/passed/failed` 的状态不变量；
7. `pass_at_1` 和 `pass_at_3` 是否为游戏直接提供的 `0/1`；
8. 终态是否稳定、没有重复结算或第四次尝试。

每个游戏的 `test.mjs` 至少断言：

- 初始状态为合法 `ready`；
- 第一次成功路径；
- 前两次失败、第三次成功路径；
- 三次失败路径；
- `pass_at_1/pass_at_3` 和 `attempts_completed` 的正确值；
- 终态重复读取稳定；
- `__dbg()`（如存在）没有写状态或自动成功入口；
- 刷新/重新打开不会在同一次运行中获得额外有效机会。

接口字段错误、状态不变量错误或无法读取合法 `BENCH` 时，结果记为 `unscored`，并记录明确错误原因；不得自动改成游戏失败。

### Q4. 协议文件和迁移窗口

协议确认后，OSWorld 先覆盖仓库中的两个正式协议文件并同步 checker；覆盖完成即视为迁移窗口开启。游戏制作侧再开始 69 个游戏的批量迁移。

不设定一个脱离实际的日历截止时间；迁移窗口以仓库中的 `1.1` 文件为唯一开始信号。迁移完成后先交付离线测试报告，再进行 OSWorld 全量复验。

## C. 结果正确性

### Q5. `pass_at_1/pass_at_3` 的 K 定义

确认采用**同一个 run 内的三次游戏机会**，不是跨 run 的三个独立样本：

- `pass_at_1`：该 run 的第一次完整尝试是否成功；
- `pass_at_3`：该 run 的三次机会内是否至少成功一次。

论文和结果表中应写成“within-run pass@1/pass@3”或“three-attempt pass@3”，并在方法部分明确它不同于标准随机采样意义下的 pass@K。跨 run 的独立重复实验另行报告，不写入游戏的 `BENCH.pass_at_1/pass_at_3`。

### Q6. 未评分任务和覆盖率

同意不能让“不操作”通过缩小分母而显得更好。统计分成两类：

1. **Agent 未完成**：达到最大回合数、没有提交 `computer_done`、停在 `ready/running`。这类任务按 0 分计入该 Agent 的 69 题主结果；
2. **环境/接口无效**：`BENCH` 缺字段、状态非法、页面无法读取或运行级失效。这类任务单列为 `unscored`，不伪装成游戏失败。

所有报告同时给出：

```text
tasks = 69
completed_tasks = 合法终态数量
coverage = completed_tasks / 69
unscored_tasks = 环境或接口无效数量
within-run pass_at_1/pass_at_3 = 在合法终态任务上统计
coverage-adjusted score = 将 Agent 未完成任务按 0 分计入 69 题分母
```

论文至少同时展示合法终态上的条件分数和 `coverage`；主结论使用 coverage-adjusted score，避免不操作提高平均值。环境错误过多时，单独标记该 run 无效，不与模型能力混合解释。

### Q7. `evaluation_settle_s`

该参数保持可配置。正式默认值从 20 秒调整为 **3 秒**；命令行仍可通过 `--evaluation_settle_s` 覆盖，用于调试或特殊任务。getter 继续保留最多 3 次读取、每次间隔 0.5 秒的重试。

游戏应在逻辑判定完成时写入最终 `BENCH`，动画随后播放并保持同一终态。3 秒等待足以覆盖现有 350–900ms 的表现动画，同时显著减少 69 题实验的无效等待。

## D. 边界确认

### Q8. `BENCH` 写入和 `results`

1. `window.BENCH` 作为数据对象被游戏内部代码赋值，不属于“提供写状态入口”。禁止的是暴露给页面外部或可通过 GUI 调用的 `setBench`、`setState`、`solve` 等函数。checker 的 CDP 读取是只读操作。
2. `results` 可以保留，作为自定义诊断字段。它不参与正式评分，也不替代 `pass_at_1`、`pass_at_3` 或 `attempts_completed`。`additionalProperties: true` 继续保留。

## E. 双方执行顺序

1. OSWorld 覆盖两个协议文件到 `1.1`；
2. OSWorld 更新 checker、运行级失效判定和快捷键动作校验；
3. 游戏制作侧迁移 69 个游戏及同目录 `test.mjs`；
4. 游戏制作侧先运行离线 checker 并提交清单；
5. OSWorld 独立复验后，使用新的 `run_id` 和 `result_dir` 开始正式实验。

在第 1 步完成前，游戏制作侧保持代码冻结，不按旧版协议批量修改。
