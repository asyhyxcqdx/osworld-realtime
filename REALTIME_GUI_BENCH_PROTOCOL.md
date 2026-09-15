# Realtime GUI Bench 游戏接口协议

版本：`realtime-gui-bench/1.1`

本文是游戏制作 Agent 的正式接口规范。每个游戏网页必须在浏览器全局对象中暴露统一的 `window.BENCH` 状态，OSWorld checker 只读取这个状态中的正式字段评分。游戏内部可以有任意玩法和内部状态，但不能改变本协议规定的字段语义。

## 1. 适用范围

本协议适用于当前 69 道 Realtime GUI Bench 游戏（A/B/C/D 四类）。每个游戏是一个独立的 OSWorld 任务，网页在 Chrome 中运行，Agent 通过真实键盘和鼠标动作操作网页。

本协议只规定：

- 游戏状态如何暴露给 checker。
- 三次机会如何计数。
- `pass@1` 和 `pass@3` 如何由游戏直接提供。
- 游戏结束后 Agent 如何结束 OSWorld 回合。

本协议不规定游戏玩法、视觉设计、内部随机数、动画实现或调试信息的具体内容。

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
| `attempts_completed` | 整数 | `0` 到 `max_attempts` | 已经结束并得到结果的完整尝试次数。成功或失败都算一次；当前尚未结束的尝试不计入。该字段单调递增，不能回退。 | 否 |
| `passed` | 布尔值 | `true` 或 `false` | 游戏是否曾经成功。成功后保持 `true`，不能因后续逻辑或页面动画改回 `false`。 | 兼容校验 |
| `status` | 字符串 | `ready`、`running`、`passed`、`failed` | 游戏生命周期状态，见第 3 节。 | 终态校验 |
| `pass_at_1` | 数字 | 必须为 `0` 或 `1` | 第一次完整尝试是否成功。由游戏在第一次尝试结算时直接写入，之后保持不变。 | **是** |
| `pass_at_3` | 数字 | 必须为 `0` 或 `1` | 在最多三次机会内是否至少成功一次。由游戏在成功或第三次失败结算时直接写入，之后保持不变。 | **是** |

`pass_at_1` 和 `pass_at_3` 使用 JSON 数字 `0`/`1`，不要使用字符串 `"0"`/`"1"`。它们不是由 checker 根据其他字段推导出来的结果。

### 2.2 机器可读 schema

制作 Agent 可以使用下面的 JSON Schema 校验核心状态。`additionalProperties`
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

### 3.1 `ready`

- 页面已加载，规则和开始入口可见。
- 游戏尚未开始，不能提前消耗机会。
- `attempts_completed=0`、`pass_at_1=0`、`pass_at_3=0`。

### 3.2 `running`

- 游戏尚未进入终态，可以是当前尝试进行中，也可以是一次失败已经结算、页面正在等待下一次尝试入口。
- `attempts_completed` 只统计已经结算的尝试，不包含尚未开始的下一次尝试。
- Agent 可以继续观察和操作。

### 3.3 `passed`

- 至少一次尝试成功。
- 页面应显示成功结果并保持终局画面。
- 游戏不能再接受会改变结果的操作。

### 3.4 `failed`

- 三次完整尝试全部失败。
- 页面应显示机会耗尽的结果并保持终局画面。
- 游戏不能再接受会改变结果的操作。

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

游戏必须在每次尝试结算时原子地更新相关字段。推荐按以下顺序更新：

### 5.1 第一次成功

```javascript
window.BENCH.attempts_completed = 1;
window.BENCH.pass_at_1 = 1;
window.BENCH.pass_at_3 = 1;
window.BENCH.passed = true;
window.BENCH.status = "passed";
```

### 5.2 第二次或第三次成功

第二次成功时 `attempts_completed=2`，第三次成功时 `attempts_completed=3`；两种情况都必须写：

```javascript
window.BENCH.pass_at_1 = 0;
window.BENCH.pass_at_3 = 1;
window.BENCH.passed = true;
window.BENCH.status = "passed";
```

### 5.3 失败但仍有机会

失败结算后，将 `attempts_completed` 增加 1，保持：

```javascript
window.BENCH.pass_at_1 = 0;
window.BENCH.pass_at_3 = 0;
window.BENCH.passed = false;
window.BENCH.status = "running";
```

随后显示下一次尝试入口。第三次失败不能继续显示可操作的下一次尝试。

### 5.4 三次失败

第三次失败结算后必须写：

```javascript
window.BENCH.attempts_completed = 3;
window.BENCH.pass_at_1 = 0;
window.BENCH.pass_at_3 = 0;
window.BENCH.passed = false;
window.BENCH.status = "failed";
```

## 6. OSWorld checker 的读取约定

checker 通过 Chrome DevTools Protocol 执行 `window.BENCH` 的只读表达式。它必须：

1. 确认页面存在 `window.BENCH` 且为对象。
2. 校验第 2 节的必需字段、类型、范围和不变量。
3. 只读取 `pass_at_1` 和 `pass_at_3` 作为分数。
4. 将完整的 `BENCH` 对象原样保存到详细结果，便于复查。
5. 对 `status=ready` 或 `status=running` 的任务标记为未完成，兼容标量为 0，不将它改写为 failed。当前汇总只对 passed/failed 计算终态均值，未完成与缺少有效结果计入 unscored_tasks；因此该均值不是覆盖全部 69 题的最终成功率。接口错误或运行级失效不产生伪造的游戏评分。
6. 对 `status=passed` 或 `status=failed` 的任务记录终态和游戏直接提供的两个分数。

checker **不得**：

- 根据 `attempts_completed` 计算 `pass_at_1` 或 `pass_at_3`。
- 根据 `results`、DOM 文本、URL hash 或 `window.__dbg()` 计算分数。
- 把接口错误、页面未结束或模型中断伪装成游戏失败。
- 修改网页中的任何 `BENCH` 字段。

OSWorld 的兼容标量结果可以使用游戏提供的 `pass_at_3`；详细结果必须同时保留 `pass_at_1`、`pass_at_3`、`status`、`attempts_completed` 和原始 `BENCH`。

## 7. Agent 终止动作

游戏显示成功，或者 `status` 变为 `failed` 后，模型必须通过 action tool 提交明确的终止动作：

```json
{"action_type":"DONE"}
```

普通文字回复、停止调用模型、点击网页上的成功提示或等待超时，都不能替代 `DONE`。`DONE` 由 OSWorld 运行器解释为“停止当前任务并读取最终评分”。

实时基准动作空间不提供 `FAIL`。Agent 只能使用 `DONE` 结束任务；如果网页仍为 `ready` 或 `running` 就提交 `DONE`，运行器按 Agent 未完成处理。

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

`__dbg()` 是测试专用只读接口。离线验收脚本可以通过 CDP 读取 `phase`、坐标、目标和随机状态等内部字段；正式 Agent 工具和上下文不会提供该接口，正式评分也不读取它。不得提供自动成功函数或修改 `BENCH` 的函数。

## 9. 游戏制作 Agent 验收清单

每个游戏提交前必须完成以下检查：

- [ ] 初始加载立即存在完整的 `window.BENCH`。
- [ ] `protocol_version`、`task`、`max_attempts` 存在且稳定。
- [ ] 页面开始前为 `status=ready`，不会提前消耗机会。
- [ ] 当前尝试只在成功或失败结算时增加一次 `attempts_completed`。
- [ ] 第一次成功、第二次成功、第三次成功和三次失败分别产生正确的两个 pass 字段。
- [ ] 成功后 `passed=true` 且状态冻结为 `passed`。
- [ ] 三次失败后状态冻结为 `failed`，不能开始第四次尝试。
- [ ] `results`、DOM 文本和调试字段不会成为 checker 的必要依赖。
- [ ] `window.__dbg()`（如果提供）为只读接口，不提供自动通关或修改 `BENCH` 的入口。
- [ ] 使用真实鼠标/键盘事件可以完成一次成功路径和一次三次失败路径。
- [ ] 页面没有依赖外部网络才能创建或更新 `window.BENCH`。

## 10. 旧接口迁移说明

历史旧包使用 `attempts`、`results`、`passed` 和 `status`，并且部分代码把 `attempts` 当作失败次数。当前 69 道游戏已完成迁移，本节仅说明旧包差异。

重写游戏时：

1. 用 `attempts_completed` 替换 `attempts`。
2. 新增并由游戏直接维护 `pass_at_1` 和 `pass_at_3`。
3. `results` 如需保留，只能作为游戏自定义诊断数据，不能作为评分依据。
4. 不要为了兼容旧 checker 同时维护两套可能不一致的计数。
5. 任务配置、checker 测试和网页完成迁移后，再删除旧 getter 中的推导逻辑。

协议变更必须升级 `protocol_version`，并使用新的实验 `run_id`。`realtime-gui-bench/1.1` 的变更记录见 `REALTIME_GUI_BENCH_PROTOCOL_CHANGELOG.md`。
