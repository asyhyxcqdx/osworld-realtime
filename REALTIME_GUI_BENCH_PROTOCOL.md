# Realtime GUI Bench 游戏接口协议

版本：`realtime-gui-bench/1.1`

本文是 69 道 Realtime GUI Bench 游戏网页的正式接口规范。每个网页必须在浏览器全局对象中暴露统一的 `window.BENCH` 状态，OSWorld checker 只读取这个状态中的正式字段评分。游戏内部可以有任意玩法和内部状态，但不能改变本协议规定的字段语义。协议版本 `realtime-gui-bench/1.1` 与基准包版本 `RealtimeGame v1.1(3)` 是两个不同的编号。

## 1. 适用范围

本协议适用于当前 69 道游戏（A/B/C/D 四类）。每个游戏是一个独立的 OSWorld 任务，网页在 Chrome 中运行，Agent 通过真实键盘和鼠标动作操作网页。

本协议规定游戏状态如何暴露给 checker、三次机会如何计数、`pass@1`/`pass@3` 如何由游戏直接提供、游戏结束后 Agent 如何结束回合；不规定玩法、视觉设计、内部随机数、动画实现或调试信息。环境层还统一禁用了刷新、控制台等浏览器快捷键，见 §9。

## 2. 必需的 `window.BENCH`

网页加载后必须立即创建一个普通 JavaScript 对象，并赋值给 `window.BENCH`。所有必需字段必须在页面初始化时存在，不能等游戏开始后才添加。

```javascript
window.BENCH = {
  protocol_version: "realtime-gui-bench/1.1",
  task: "wall_jump",
  max_attempts: 3,
  attempts_completed: 0,
  passed: false,
  status: "ready",
  pass_at_1: 0,
  pass_at_3: 0
};
```

### 2.1 字段定义

| 字段 | 类型 | 必需值或范围 | 明确定义 | 是否评分来源 |
| --- | --- | --- | --- | --- |
| `protocol_version` | 字符串 | 必须为 `realtime-gui-bench/1.1` | 当前网页遵循的协议版本。整个游戏生命周期内不可改变。 | 否 |
| `task` | 字符串 | 非空、建议使用小写 snake_case | 游戏的内部稳定名称，例如 `wall_jump`。用于日志和诊断，不用于决定分数。 | 否 |
| `max_attempts` | 整数 | 当前基准必须为 `3` | 该游戏允许的最大完整尝试次数。不可在运行中改变。 | 间接校验 |
| `attempts_completed` | 整数 | `0` 到 `max_attempts` | 已经结算的完整尝试数：成功、失败各计 1，进行中的那次不计，单调递增不回退。**不是失败次数、当前尝试编号或点击次数。** | 否 |
| `passed` | 布尔值 | `true` 或 `false` | 游戏是否曾经成功。成功后保持 `true`，不能因后续逻辑或页面动画改回 `false`。 | 兼容校验 |
| `status` | 字符串 | `ready`、`running`、`passed`、`failed` | 游戏生命周期状态，见第 3 节。 | 终态校验 |
| `pass_at_1` | 数字 | 必须为 `0` 或 `1` | 第一次完整尝试是否成功。由游戏在第一次尝试结算时直接写入，之后保持不变。 | **是** |
| `pass_at_3` | 数字 | 必须为 `0` 或 `1` | 在最多三次机会内是否至少成功一次。由游戏在成功或第三次失败结算时直接写入，之后保持不变。 | **是** |

`pass_at_1` 和 `pass_at_3` 使用 JSON 数字 `0`/`1`，不要使用字符串 `"0"`/`"1"`。它们不是由 checker 根据其他字段推导出来的结果。

### 2.2 机器可读 schema

重写或新增游戏时，可以用下面的 JSON Schema 校验核心状态。`additionalProperties`
设为 `true`，是为了允许游戏保存自定义诊断字段；自定义字段不能改变核心字段含义。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": true,
  "required": [
    "protocol_version",
    "task",
    "max_attempts",
    "attempts_completed",
    "passed",
    "status",
    "pass_at_1",
    "pass_at_3"
  ],
  "properties": {
    "protocol_version": {"const": "realtime-gui-bench/1.1"},
    "task": {"type": "string", "minLength": 1},
    "max_attempts": {"type": "integer", "const": 3},
    "attempts_completed": {"type": "integer", "minimum": 0, "maximum": 3},
    "passed": {"type": "boolean"},
    "status": {"enum": ["ready", "running", "passed", "failed"]},
    "pass_at_1": {"type": "number", "enum": [0, 1]},
    "pass_at_3": {"type": "number", "enum": [0, 1]}
  }
}
```

除 JSON Schema 外，仓库还提供离线校验脚本
`scripts/python/check_realtime_gui_contract.py`：对导出的 BENCH JSON（单个
`--input` 或整个目录 `--input-dir`，可用 `--manifest` 映射 benchmark id）做与
checker 一致的字段与状态一致性检查，输出 `scored` / `agent_incomplete` /
`unscored` 判定，全部合法时退出码为 0。它不启动虚拟机，适合写游戏时快速自检。

### 2.3 字段不变量

游戏必须始终满足以下关系：

1. `protocol_version`、`task`、`max_attempts` 不改变。
2. `0 <= attempts_completed <= max_attempts`。
3. `passed === (pass_at_3 === 1)`。
4. `status === "passed"` 时，`passed === true` 且 `pass_at_3 === 1`。
5. `status === "failed"` 时，`passed === false`、`pass_at_1 === 0`、`pass_at_3 === 0` 且 `attempts_completed === 3`。
6. `status === "ready"` 时，`attempts_completed === 0`、`passed === false`、`pass_at_1 === 0`、`pass_at_3 === 0`。
7. `status === "running"` 时，游戏尚未进入终态；它可以表示当前尝试进行中，也可以表示失败结算后正在等待下一次尝试入口。`passed`、`pass_at_1`、`pass_at_3` 必须仍为 `false`/`0`。
8. 一旦 `status` 变为 `passed` 或 `failed`，所有必需字段冻结，不能重新开始或继续消耗机会。重新加载只用于创建新的 OSWorld `run_id` 实例；同一 `run_id` 内的刷新、导航和复制页面由环境标记为无效运行。

## 3. 状态机

```text
ready --开始第一次尝试--> running
running --本次成功--> passed
running --本次失败且仍有机会--> running
running --第三次失败--> failed
```

四个状态的字段取值由 §2.3 的不变量完全确定：`passed` 是唯一可以出现 `pass_at_* = 1` 的终态，`failed` 只在三次尝试全部结算后出现，两者一旦进入就冻结终局画面、不再接受改变结果的操作。

## 4. 尝试的统一定义

一次“完整尝试”从游戏开始或点击下一次重试入口开始，到该次成功或失败结算为止。以下事件各使 `attempts_completed` 增加 **1 次**：

- 当前尝试成功并结算。
- 当前尝试失败并结算。

以下事件不增加 `attempts_completed`：

- 页面加载。
- 显示规则或等待开始。
- 当前尝试中的任意中间事件。
- 等待动画、等待重试按钮或等待下一轮提示。
- 游戏成功后的停留。

游戏不得把“失败次数”“当前尝试编号”“点击次数”“子事件数量”或 `results` 数组长度混用为 `attempts_completed`。所有游戏必须使用本协议的同一语义。

## 5. 分数写入规则

游戏在每次尝试结算时**原子地**更新字段。四种结算情况必须写入的值：

| 结算情况 | attempts_completed | pass_at_1 | pass_at_3 | passed | status |
| --- | --- | --- | --- | --- | --- |
| 第一次尝试成功 | 1 | **1** | 1 | true | `passed` |
| 第二次尝试成功 | 2 | 0 | 1 | true | `passed` |
| 第三次尝试成功 | 3 | 0 | 1 | true | `passed` |
| 失败但仍有机会（第 1、2 次失败） | +1 | 0 | 0 | false | `running` |
| 第三次尝试失败 | 3 | 0 | 0 | false | `failed` |

规则：`attempts_completed` 在每次失败结算后加 1，成功后冻结在当时的次数；每次失败结算后必须显示下一次尝试入口，第三次失败后不能再显示可操作的入口。

## 6. OSWorld checker 的读取约定

checker 通过 Chrome DevTools Protocol 执行 `window.BENCH` 的只读表达式。它必须：

1. 确认页面存在 `window.BENCH` 且为对象。
2. 校验第 2 节的必需字段、类型、范围和不变量。
3. 只读取 `pass_at_1` 和 `pass_at_3` 作为分数。
4. 将完整的 `BENCH` 对象原样保存到详细结果，便于复查。
5. 游戏 `status=ready/running` 保留原值，标量为 0，不改写为 failed：提前 DONE 或耗尽回合预算而未成功都记**有效 0 分**。结束原因由 runner 写入唯一字段 `termination_reason`（`done` / `decision_limit` / `execution_error` / `run_error` / `interrupted`），`result.json` 与 `agent_metrics.json` 都会记录；异常保留错误详情，不伪造游戏评分。字段口径与出表见 [执行同学作业单](HANDOFF_CN.md)。
6. 对 `status=passed` 或 `status=failed` 的任务记录终态和游戏直接提供的两个分数。

checker **不得**：

- 根据 `attempts_completed` 计算 `pass_at_1` 或 `pass_at_3`。
- 根据 `results`、DOM 文本、URL hash 或 `window.__dbg()` 计算分数。
- 把接口错误、页面未结束或模型中断伪装成游戏失败。
- 修改网页中的任何 `BENCH` 字段。

OSWorld 的兼容标量结果可以使用游戏提供的 `pass_at_3`；详细结果必须同时保留 `pass_at_1`、`pass_at_3`、`status`、`attempts_completed` 和原始 `BENCH`。

## 7. Agent 终止动作

游戏显示成功，或者 `status` 变为 `failed` 后，模型必须调用注册的 `computer_done` 工具，参数为空对象 `{}`。运行器将该工具调用转换为内部终止动作：

```json
{"action_type":"DONE"}
```

普通文字回复、停止调用模型、点击网页上的成功提示或等待超时，都不能替代 `DONE`。`DONE` 由 OSWorld 运行器解释为“停止当前任务并读取最终评分”。动作空间**不提供 `FAIL`**：脚本即使提前提交 `DONE`，也只是按当时状态正常评分，游戏状态不会被改写成 failed。

## 8. 可选诊断接口

### 8.1 URL hash

游戏可以同步以下 URL hash，方便人工排查，但 checker 不从 hash 评分：

```text
#task=wall_jump&attempts_completed=2&passed=1&status=passed&pass_at_1=0&pass_at_3=1
```

hash 中的字段必须与 `window.BENCH` 当前值一致。hash 不是正式接口的替代品。

### 8.2 `window.__dbg()`

游戏可以提供只读调试函数：

```javascript
window.__dbg = function () {
  return {
    task: window.BENCH.task,
    attempts_completed: window.BENCH.attempts_completed,
    passed: window.BENCH.passed,
    status: window.BENCH.status,
    phase: "game-specific-state"
  };
};
```

`__dbg()` 是测试专用只读接口。离线验收脚本（`scripts/python/validate_realtime_gui_bench.py`）通过 CDP 只读取其中的 `phase`；正式 Agent 工具和上下文不会提供该接口，正式评分也不读取它。不得提供自动成功函数或修改 `BENCH` 的函数。

## 9. 禁用快捷键（环境级，非游戏实现）

覆盖全部 69 个游戏，由 OSWorld 环境统一执行；**游戏网页本身不需要做任何事**。

下面这些 `PRESS` / `HOTKEY` 在动作执行前就会被拦下，**不会发送到虚拟机**：

| 用途 | 按键或组合 |
| --- | --- |
| 刷新页面 | `f5`、`ctrl+r`、`ctrl+shift+r`、`browserrefresh` |
| 开发者工具 | `f12`、`ctrl+shift+i`、`ctrl+shift+j`、`ctrl+shift+c` |
| 查看源码 | `ctrl+u` |
| 地址栏 | `ctrl+l` |
| 前进 / 后退 / 主页 | `alt+left`、`alt+right`、`browserback`、`browserforward`、`browserhome` |
| 保存 / 打印 | `ctrl+s`、`ctrl+p` |

把组合拆开也算违规：例如先 `KEY_DOWN ctrl`、下一回合再 `PRESS u`，同样会被拦下——程序记住哪些修饰键还按着，跨回合也能识别。

**鼠标绕道也不行**：用鼠标点刷新按钮或手势前进/后退绕不过按键过滤，运行器会在动作后、评分前检查标签页、URL 和页面加载时间，发现变化就把这次运行判为无效（`run_error`）且不评分。

被拦的动作不执行，但会在任务目录的 `trajectory.jsonl` 里留一条 `action_rejected` 记录（写明被拦的动作和所属回合），便于事后审计。**发生拦截的那次运行没有有效成绩，需要重跑。**

## 10. 新增或重写游戏的验收清单

字段语义与结算规则由 §2.3 的不变量和 §5 的写入表完整规定，逐条照抄没有意义。除那些硬性检查外，还必须确认：

- [ ] `window.__dbg()`（如果提供）是只读的，不提供自动通关或修改 `BENCH` 的入口。
- [ ] 用真实鼠标/键盘事件可以走完一次成功路径和一次三次失败路径。
- [ ] 页面不依赖外部网络就能创建和更新 `window.BENCH`。
- [ ] `results`、DOM 文本、URL hash 和调试字段都不会成为 checker 的必要依赖。

协议变更必须升级 `protocol_version` 并使用新的实验 `run_id`；变更历史从 Git 提交记录追溯。
