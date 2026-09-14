# Realtime GUI Bench 开放问题回复

日期：2026-09-14
回复对象：Realtime GUI Bench 游戏制作侧

以下答案用于 69 个游戏的接口迁移。协议文件会在本回复确认后按本文修改，游戏制作侧以修订后的文件为准，不要按旧文字提前批量改造。

## A. 必须确认

### A1. 协议版本

本次修改包含状态语义和反作弊责任边界变化，因此升级版本：

- 接口协议：`realtime-gui-bench/1.1`
- 反作弊协议：`realtime-gui-bench-anti-cheat/1.1`

69 个游戏的 `window.BENCH.protocol_version` 必须写入 `realtime-gui-bench/1.1`。README 中记录的反作弊版本写入 `realtime-gui-bench-anti-cheat/1.1`。

### A2. 协议文件和测试交接

修订版会覆盖仓库中的两个正式文件：

- `REALTIME_GUI_BENCH_PROTOCOL.md`
- `REALTIME_GAME_ANTICHEAT_ACCEPTANCE.md`

不同时维护另一份同名协议副本。游戏制作侧收到仓库中这两个文件的更新后再开始迁移。

每个游戏的 `test.mjs`（或等价测试脚本）应随游戏交付，OSWorld 会独立运行这些测试，并另外运行自己的全量接口校验。测试脚本可以通过 CDP 读取只读 `__dbg()`，但不得调用写状态或自动成功函数。

### A3. `invalid` 和跨加载状态

游戏不需要、也不应该通过 `localStorage`/`sessionStorage` 保存跨实验机会。反作弊协议中的“游戏应将实例标记为 `invalid`”会改写为：

- 同一 `run_id` 内发生刷新、导航、复制标签页或重新打开页面时，由 OSWorld 环境将该运行标记为 `invalid`；
- 该运行不进入正式评分，游戏页面无需在刷新后恢复或识别旧实例；
- 新的 `run_id` 使用新的浏览器上下文/实验环境，可以从 `ready` 开始新的三次机会；
- 游戏不得依赖刷新来恢复同一次运行的有效状态。

因此不要求游戏实现跨加载持久化，也不要求游戏自行写入 `invalid` 字段。

### A4. `BENCH` 读取时机

当前 OSWorld 运行器的正式时序为：

1. Agent 提交 `computer_done` 并结束任务；
2. 运行器等待 `evaluation_settle_s=20` 秒；
3. checker 读取 `window.BENCH`；
4. getter 默认最多读取 3 次，每次失败间隔 0.5 秒，单次读取超时 10 秒。

因此 350–900ms 的动画不会造成竞态。游戏仍建议在逻辑成功/失败判定时先原子更新 `BENCH`，再播放动画；但不要求删除原有动画。动画结束后必须保持相同终态。

### A5. “GUI 可达”的范围

本协议中的“GUI 可达”指 Agent 通过正式允许的屏幕观察和 `computer_13` 动作能够获得或触发的内容。它不包括 Agent 无法调用的 CDP、JavaScript、终端或测试脚本接口。

因此：

- 正常可见的规则、反馈和玩法目标可以保留；
- `__dbg()` 可以返回测试所需的内部状态，但不能提供写状态或自动成功函数；
- DOM 节点、`data-*`、class/id、`title`、meta、`noscript`、内联脚本常量、console 和错误堆栈不要求逐项脱敏；
- 这些内容不得通过正常游戏画面主动展示答案，也不得提供页面上的“查看源码/调试/自动成功”入口；
- F12、查看源码、地址栏和开发者工具的阻断由 OSWorld 动作校验器负责，游戏制作侧不需要模拟该阻断器。

客户端游戏的 JavaScript 本身必然包含玩法逻辑，因此本协议不要求实现无法达到的“源码中绝不出现内部答案”。正式实验依赖环境隔离 GUI 越权通道，离线验收则可以通过 CDP 读取 `__dbg()`。

### A6. 校验失败与测试责任

#### A6.1 接口不变量失败

如果 `BENCH` 缺字段、类型错误、状态不一致或 `attempts_completed` 越界，这是**接口无效/未评分**，不是游戏失败分数：

- 不计入 `scored_tasks`；
- 不把它改写为 `pass_at_1=0` 或 `pass_at_3=0`；
- 在结果和日志中记录接口错误；
- 游戏制作侧应优先修复，因为这类错误会使该题无法产生有效成绩。

页面明确进入 `status="failed"` 且字段合法，才是可评分的 0 分结果。

#### A6.2 测试责任

游戏制作侧逐游戏负责：

- 新 `BENCH` 字段和状态不变量；
- 第一次成功、后续成功、三次失败路径；
- 页面自身不存在重置/自动成功控件；
- `__dbg()` 只读且不提供写状态入口；
- 刷新/重新打开后不依赖旧页面状态继续同一次运行。

OSWorld 环境统一负责：

- F5、F12、开发者工具、源码、地址栏和导航快捷键的动作拒绝；
- 新 `run_id` 的浏览器上下文；
- 轨迹中的拒绝动作记录；
- CDP 读取 `BENCH` 和正式评分。

快捷键阻断不要求 69 个游戏分别提交，环境提供一份覆盖全部游戏的报告。

## B. 建议确认

### B1. `task` 字段

`window.BENCH.task` 是游戏内部稳定名称，不要求等于外部 `benchmark_id`，也不用于关联评分。现有的 `clockface`、`random_enemy`、`double_jump` 等名称可以保留。

要求只有：非空、稳定、建议使用小写 snake_case；同一游戏不能在运行中改变。外部任务编号仍由 OSWorld 任务清单的 `benchmark_id` 管理。

### B2. 任务接入方式

正式任务使用本地 HTTP 服务，不使用 `file://`：

```text
http://127.0.0.1:8765/<benchmark-id>/index.html
```

正式 URL 不带测试开关、调试开关、答案参数或可改变状态的 query/hash。`__dbg()` 可以始终存在，但只能被离线测试/诊断读取，不能出现在 Agent 工具结果中。

### B3. 浏览器上下文和 `computer_done`

- 每个新的 `run_id` 使用全新的任务环境/浏览器上下文，或由环境保证等价的干净状态；
- 同一 `run_id` 内刷新和导航由环境判定为运行失效；
- 四份 Agent 配置和 instruction 已明确要求在成功或机会耗尽后提交 `DONE`；注册到模型的工具名称是 `computer_done`；
- 游戏制作侧不需要实现 `computer_done`，只需保持 `BENCH` 终态可读。

### B4. `unscored_tasks` 的最终统计

`unscored_tasks` 在最终报告中单独列出，不计入 `pass_at_1`/`pass_at_3` 的平均分分母，也不自动计为 0 分。当前汇总字段已经采用：

```text
tasks = 69
scored_tasks = 合法终态数量
unscored_tasks = 69 - scored_tasks
pass_at_1/pass_at_3 = 仅对 scored_tasks 求平均
```

游戏不需要增加“无操作超时后自动失败”的兜底逻辑。Agent 未操作、运行失效或接口错误应保持可区分的未评分状态。

## C. 迁移执行顺序

1. OSWorld 更新两份协议到 `1.1`，并同步更新 checker 和动作校验器；
2. OSWorld 提供一次环境级快捷键阻断测试报告；
3. 游戏制作侧按新协议迁移 69 个游戏、所有交付副本和 `test.mjs`；
4. 双方分别运行游戏侧接口测试和 OSWorld 全量评分测试；
5. 使用新的 `run_id` 和 `result_dir` 记录正式结果，不复用旧协议结果目录。

在第 1 步完成前，不开始 69 个游戏的批量字段迁移。
