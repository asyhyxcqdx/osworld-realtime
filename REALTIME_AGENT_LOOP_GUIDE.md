# 实时 GUI Agent Loop 导读

本文说明四个实时 Agent 从一次截图到下一次截图的完整执行过程。当前正式实验使用供应商原生 `tool use`；模型不能用文本 JSON 代替动作工具。

## 1. 一回合的定义

一次回合（decision round）是：

```text
当前截图
  → 模型思考
  → 可选：查询历史视频帧（一次或多次）
  → 提交一个或多个动作工具调用
  → 环境执行动作
  → 环境返回新的截图和执行结果
```

历史帧查询属于感知阶段，不会增加回合数，也不会执行鼠标或键盘动作。模型可能因为多次感知而产生多次 HTTP 请求，但这些请求仍属于同一个回合。

四个 Agent 的差异由 `MODES` 定义：

| Agent | 当前截图 | 历史帧 | 每回合动作数 |
| --- | --- | --- | --- |
| Agent1 / Vanilla | 有 | 无 | 1 |
| Agent2 / anticipatory | 有 | 无 | 1–100 |
| Agent3 / Video | 有 | 有 | 1 |
| Agent4 / combine | 有 | 有 | 1–100 |

对应实现见 [realtime_protocol.py](/mnt/zhaorunsong/yhyx/OSWorld/mm_agents/realtime_protocol.py#L18)。

## 2. 运行入口

实验从 [run_multienv.py](/mnt/zhaorunsong/yhyx/OSWorld/scripts/python/run_multienv.py#L115) 进入。当指定 `--agent_variant` 时，运行器会：

1. 根据 Agent 和模型名定位 YAML 配置；
2. 用 [realtime_config.py](/mnt/zhaorunsong/yhyx/OSWorld/mm_agents/realtime_config.py#L19) 校验配置；
3. 将 YAML 转成 `RealtimeAgent` 的构造参数；
4. 创建环境和 Agent；
5. 调用 [lib_run_realtime.py](/mnt/zhaorunsong/yhyx/OSWorld/lib_run_realtime.py#L12) 执行任务。

例如，Agent1 默认读取：

```text
configs/realtime_agents/vanilla-claude-sonnet-5.yaml
```

配置文件中的 `system_prompt` 会直接发送给模型。模型名、API 协议、工具格式、输出上限和动作预算也来自该配置。

## 3. 工具 schema 如何生成

[realtime_protocol.py](/mnt/zhaorunsong/yhyx/OSWorld/mm_agents/realtime_protocol.py#L50) 从 OSWorld 现有的 `ACTION_SPACE` 自动生成动作工具：

```text
MOVE_TO    → computer_move_to
CLICK      → computer_click
PRESS      → computer_press
TYPING     → computer_typing
WAIT       → computer_wait
DONE       → computer_done
```

工具参数的类型、枚举值和坐标范围来自 `computer_13`，配置文件不重复定义这些参数。`FAIL` 不会注册为工具。

历史帧工具固定名为 `get_frames`，参数是一个长度为 1–8 的 `times_s` 数组。只有 Video Agent 和 combine Agent 注册该工具。

## 4. 模型请求

[ModelWire.request](/mnt/zhaorunsong/yhyx/OSWorld/mm_agents/realtime_agent.py#L151) 将统一的内部工具描述转换为供应商格式：

| 协议 | 工具字段 |
| --- | --- |
| Anthropic Messages | `name`、`description`、`input_schema` |
| OpenAI Chat Completions | `type=function`、`function.parameters` |
| OpenAI Responses | `type=function`、`parameters` |

请求中包含：

- system prompt；
- 当前任务 instruction；
- 当前截图；
- 当前任务此前保留的完整消息历史；
- 当前回合允许使用的感知工具和动作工具；
- `max_tokens`、temperature 和 thinking 配置。

模型返回的原始响应不会丢弃。`provider_response`、`usage`、reasoning summary、tool call ID 和 stop reason 会进入轨迹日志。

## 5. 感知阶段

[RealtimeAgent.predict](/mnt/zhaorunsong/yhyx/OSWorld/mm_agents/realtime_agent.py#L509) 首先把当前截图和任务时间放入消息，然后请求模型。

如果模型返回 `get_frames`：

1. Agent 校验 `times_s`；
2. 调用环境侧的 `env.controller.get_frames()`；
3. 保存查询到的图片文件；
4. 将 `tool_result` 按供应商要求放回消息；
5. 再次请求模型。

这一步可以重复多次。历史帧查询不会执行动作，因此不会增加 `action_count` 或 `decision_count`。

感知工具和动作工具不能出现在同一个模型响应中。模型必须先完成感知，再在后续响应中提交动作。

## 6. 动作阶段

模型返回动作工具调用后，Agent 将工具名转换成 OSWorld 动作字典：

```json
{
  "action_type": "PRESS",
  "parameters": {"key": " "}
}
```

转换和校验由 `_decode_action_calls()` 完成：

- Agent1/3 超过一个动作调用时拒绝整次响应；
- Agent2/4 超过 100 个动作调用时拒绝整次响应；
- 动作参数必须符合 `computer_13` schema；
- 刷新、后退、前进和离开页面的按键会被拒绝；
- `DONE` 只能出现在动作序列的最后；
- `FAIL` 始终被拒绝。

校验失败时不会执行部分动作。Agent 会把错误放回当前回合，让模型重新提交动作；连续多次格式错误后终止任务。

## 7. 环境执行和回传

[lib_run_realtime.py](/mnt/zhaorunsong/yhyx/OSWorld/lib_run_realtime.py#L113) 根据 Agent 模式选择：

```python
env.step(actions[0])          # atomic
env.step_sequence(actions)    # sequence
```

序列动作在 VM 内一次发送，并由 [desktop_env/server/realtime.py](/mnt/zhaorunsong/yhyx/OSWorld/desktop_env/server/realtime.py#L241) 按顺序执行。VM sequence 的最大长度是 100。

执行完成后，环境返回：

- 新截图；
- reward；
- `done`；
- `info`；
- 实际执行时间。

`record_action_result()` 将这些结果包装成动作工具的 `tool_result`，并附加到当前回合的消息中。下一回合请求模型时，模型可以看到自己上一次动作的执行结果。

## 8. 回合历史和轨迹

每个回合的消息会保存到 `self.rounds`。正式配置使用完整上下文，因此运行器传递 `max_trajectory_length=None`，不按固定回合数主动删除历史。

环境侧将所有事件按时间顺序写入一个文件：

```text
<result_dir>/trajectory.jsonl
```

常见事件包括：

| 事件 | 含义 |
| --- | --- |
| `initial_observation` | 初始截图和任务时间 |
| `model_request` | 发给模型的消息、工具名和参数 |
| `model_response` | 原始 API 响应、reasoning、usage 和 tool calls |
| `tool_result` | 历史帧查询结果 |
| `action_submitted` | 转换后的动作 |
| `action_executed` | VM 实际执行结果和新截图 |
| `action_tool_result` | 回传模型的动作执行结果 |
| `evaluation` | 最终游戏评分 |

截图文件本身单独保存；JSONL 中保存截图路径和哈希，避免把大段 base64 重复写入日志。

## 9. `DONE` 的终止语义

`DONE` 是 Agent 唯一的终止动作。Agent 只有在以下情况调用它：

- 游戏页面明确显示成功；或
- 游戏已经耗尽所有机会。

Agent 不提交 `FAIL`。游戏失败由游戏页面和评估接口表达；如果 Agent 不再操作，运行器最终会记录当前游戏状态。`DONE` 执行后，VM 返回 `done=true`，运行器结束动作循环并进入评估阶段。

## 10. 当前实现边界

Agent loop 已经完成代码闭环，但全量实验前仍需验证：

1. `computer_13` 的实时持续时间和动作间隔是否满足 69 个游戏；
2. 真实 VM 是否正确返回动作执行结果；
3. 各商业 API 是否完整返回 usage 和 reasoning summary；
4. 69 个游戏是否全部符合统一的 `BENCH` 和 `pass_at_1/pass_at_3` 协议。

因此，当前适合先运行单个游戏的真实 VM 测试，再进行全量实验。
