# Realtime GUI Agent 配置协议

版本：`realtime-agent-config/1.0`

本文定义四个实时 GUI Agent 的独立配置文件格式。配置文件同时保存 Agent 的能力边界、API 参数、动作约束和 system prompt。运行器读取配置后，必须先完成校验，再创建共享的 Agent 执行引擎。

## 1. 配置文件布局

建议目录：

```text
configs/realtime_agents/
├── vanilla-<model>.yaml
├── video-<model>.yaml
├── anticipatory-<model>.yaml
└── combine-<model>.yaml
```

四个 Agent 的正式名称和稳定 ID 如下：

| 稳定 ID | 显示名称 | 实验含义 |
| --- | --- | --- |
| `vanilla` | `Vanilla Agent` | 当前截图、单个原子动作 |
| `anticipatory` | `anticipatory agent` | 当前截图、动作序列 |
| `video` | `Video Agent` | 当前截图、历史视频帧、单个原子动作 |
| `combine` | `combine Agent` | 当前截图、历史视频帧、动作序列 |

稳定 ID 用于命令行参数、结果目录和统计字段；显示名称只用于日志和论文展示。文件名格式为 `<agent_id>-<model>.yaml`，其中 `<model>` 必须与 `api.model` 一致。稳定 ID 一旦发布不能改变。

## 2. 顶层字段

每个配置文件必须是 YAML 映射，并且必须包含以下字段：

| 字段 | 类型 | 允许值 | 含义 |
| --- | --- | --- | --- |
| `config_version` | 字符串 | `realtime-agent-config/1.0` | 配置协议版本。解析器只接受明确支持的版本。 |
| `agent_id` | 字符串 | `vanilla`、`video`、`anticipatory`、`combine` | Agent 的稳定机器 ID，必须与文件名中的 `<agent_id>` 和结果目录一致。 |
| `display_name` | 字符串 | 与上表一致 | 人类可读的 Agent 名称。 |
| `description` | 字符串 | 非空 | 对该 Agent 能力边界的简短说明。 |
| `observation` | 映射 | 见第 3 节 | 感知能力配置。 |
| `action` | 映射 | 见第 4 节 | 动作能力配置。 |
| `context` | 映射 | 见第 5 节 | 模型上下文传递策略。正式实验使用完整上下文。 |
| `api` | 映射 | 见第 6 节 | 模型 API 和生成参数。 |
| `constraints` | 映射 | 见第 7 节 | 必须执行的安全和输出约束。 |
| `system_prompt` | 多行字符串 | 非空 | 发送给模型的完整 system prompt，直接写在当前配置文件中。 |

配置解析器必须拒绝缺少必需字段、字段类型错误、未知顶层字段和违反组合约束的配置。建议使用 `additionalProperties: false` 的 JSON Schema 校验转换后的映射。

## 3. `observation` 感知字段

```yaml
observation:
  current_screenshot: true
  historical_video:
    enabled: false
    tool_name: get_frames
    max_queries_per_turn: 0
    max_times_per_query: 8
```

| 字段 | 类型 | 允许值 | 含义 |
| --- | --- | --- | --- |
| `current_screenshot` | 布尔值 | 必须为 `true` | 每个决策回合开始时提供当前桌面截图。四个 Agent 都必须开启。 |
| `historical_video.enabled` | 布尔值 | `true` 或 `false` | 是否向模型注册历史视频帧工具。`false` 时不得发送该工具。 |
| `historical_video.tool_name` | 字符串 | 必须为 `get_frames` | 历史帧工具的稳定名称。 |
| `historical_video.max_queries_per_turn` | 整数 | `0` 或正整数 | 每回合最多调用历史帧工具的次数。`0` 表示不设次数上限，但仍受单回合执行和任务决策回合预算限制。 |
| `historical_video.max_times_per_query` | 整数 | 1 到 8 | 单次工具调用最多请求的时间点数量。必须与工具 schema 一致。 |

历史帧工具一次返回多个时间点不增加回合数。感知工具和动作工具都使用供应商原生 tool use。一个决策回合可以包含多个工具调用：历史帧调用必须先得到结果，随后才能执行动作工具调用；动作工具调用可以连续提交多个原子动作。

## 4. `action` 动作字段

```yaml
action:
  mode: atomic
  max_actions_per_turn: 1
  allow_done: true
  allow_fail: false
  allow_wait: true
```

| 字段 | 类型 | 允许值 | 含义 |
| --- | --- | --- | --- |
| `mode` | 字符串 | `atomic` 或 `sequence` | `atomic` 表示每回合只能提交一个动作；`sequence` 表示可以提交连续动作数组。 |
| `max_actions_per_turn` | 整数 | `1` 到 `100` | 一回合允许提交的最大原子动作数量。`atomic` 模式必须为 `1`。 |
| `allow_done` | 布尔值 | 必须为 `true` | 是否允许提交 `{"action_type":"DONE"}`。四个 Agent 都必须开启。 |
| `allow_fail` | 布尔值 | 必须为 `false` | 禁止 Agent 提交 `FAIL`；游戏自行进入成功或失败状态，Agent 只能用 `DONE` 结束。 |
| `allow_wait` | 布尔值 | 必须为 `true` | 是否允许提交 `{"action_type":"WAIT"}`。四个 Agent 都必须开启。 |

`atomic` 模式只能调用一次动作工具。`sequence` 模式可以连续调用 1 到 `max_actions_per_turn` 个动作工具，最多 100 个。只有 `DONE` 是终止动作，且必须是该回合的最后一个动作工具调用。

动作工具的名称、参数类型和屏幕坐标范围统一使用 `computer_13` 的实时扩展协议；配置文件不得重新定义动作参数。实时扩展的时序字段如下：

| 动作 | 时序字段 | 语义 |
| --- | --- | --- |
| `MOVE_TO` | `duration_s`，必填，`0`–`10` 秒 | 鼠标从当前位置线性移动到目标坐标的持续时间。 |
| `DRAG_TO` | `duration_s`，必填，`0`–`10` 秒 | 按住左键拖到目标坐标的持续时间。 |
| `WAIT` | `duration_s`，必填，`0`–`60` 秒 | 在当前页面保持不动的明确等待时间。 |
| 其他动作 | 无隐含等待 | 动作完成后立即执行下一个动作或拍摄下一张截图。 |

实时执行器不再使用全局 action pause、随机移动时长或 PyAutoGUI 隐含 `PAUSE`。动作之间需要间隔时，必须显式提交 `WAIT`。

## 5. `context` 上下文字段

```yaml
context:
  history_policy: full
  include_screenshots: true
  on_context_limit: fail_with_explicit_error
```

| 字段 | 类型 | 允许值 | 含义 |
| --- | --- | --- | --- |
| `history_policy` | 字符串 | 必须为 `full` | 每次 API 请求发送当前任务此前的全部上下文，不按固定回合数截断。 |
| `include_screenshots` | 布尔值 | 必须为 `true` | 上下文必须包含此前所有可用截图。截图以 API 支持的图像内容发送，不能只保留文字描述。 |
| `on_context_limit` | 字符串 | 必须为 `fail_with_explicit_error` | 供应商上下文窗口不足时显式报错并记录，不得静默删除最早上下文或改变实验条件。 |

四个 Agent 都使用完整上下文策略。历史视频能力仍由 `observation.historical_video.enabled` 单独控制；`Video Agent` 和 `combine Agent` 额外拥有历史帧工具，但不会因此改变上下文保留规则。

## 6. `api` 模型接口字段

```yaml
api:
  model: claude-sonnet-4-6
  protocol: anthropic_messages
  tool_format: native
  context_window_tokens: 1000000
  max_output_tokens: 128000
  temperature: null
  thinking:
    enabled: true
    effort: high
    summary: true
```

| 字段 | 类型 | 允许值 | 含义 |
| --- | --- | --- | --- |
| `model` | 字符串 | 非空模型标识 | 本配置使用的模型名称。模型名属于实验配置，必须写入配置并记录到结果；API 密钥不能写入配置。 |
| `protocol` | 字符串 | `anthropic_messages`、`openai_responses` | 正式 realtime 配置支持的 API 消息协议；底层 `openai_chat` 兼容代码不用于这些配置。 |
| `tool_format` | 字符串 | 必须为 `native` | 工具调用格式。正式实验必须使用供应商原生 tool use。 |
| `context_window_tokens` | 整数 | 不小于 `128000` | 供应商声明的单次请求最大上下文 token 数，必须足以容纳完整历史消息和截图。 |
| `max_output_tokens` | 整数 | 不小于 `128000` 的正整数 | 单次模型响应允许生成的最大 token 数。它是输出上限，不是上下文窗口大小；供应商不支持该上限时必须显式报错，不能静默降低。 |
| `temperature` | 数字或空值 | `0` 到 `2` 或 `null` | 采样温度。使用自适应 thinking 且供应商不允许温度参数时必须为 `null`。 |
| `thinking.enabled` | 布尔值 | 必须为 `true` | 是否请求供应商启用 thinking。正式实验必须开启。 |
| `thinking.effort` | 字符串 | `low`、`medium`、`high`、`max`；Responses 另支持 `xhigh` | 实验规定的固定 thinking effort。不同供应商的适配器必须尽量映射到相同等级；不支持时显式报错。 |
| `thinking.summary` | 布尔值 | 必须为 `true` | 是否请求供应商返回可读 thinking summary。它不代表可以获取隐藏的完整思维链。 |
| `coordinate_mapping` | 映射或空值 | 仅在经过校准的 Anthropic 图像路径使用 | Claude 标准视觉分辨率会返回缩放后图像坐标时，声明模型坐标尺寸和 VM 原生屏幕尺寸；动作执行器按两个轴分别映射回原生坐标。Astra 等其他协议必须省略此字段。 |

配置对实验使用固定的 `thinking.effort`，不把供应商内部的 adaptive 或 enabled 实现方式当作实验变量。Anthropic 等供应商如果只能通过 adaptive thinking 实现固定 effort，由 API 适配器负责映射；这不改变配置中的 effort 等级。其他协议不支持该 effort 时必须拒绝配置，而不是静默降低等级。

模型名称必须写入 Agent 配置文件，以便配置自包含和实验复现。命令行可以选择性覆盖模型，但覆盖值必须写入 `experiment.json` 和 `trajectory.jsonl`；API 密钥只从环境变量读取。

`combine-gpt-6-astra.yaml` 使用 Responses 协议，保持 `high` 思考档位和摘要开启：适配器发送 `reasoning: {effort: high, summary: auto}`，保留返回的 reasoning 内容及 encrypted content，并省略 temperature。接口地址通过 `--api_base_url` 指定，密钥通过 `OPENAI_API_KEY` 注入。参数依据：[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)、[reasoning summaries](https://developers.openai.com/api/docs/guides/reasoning#reasoning-summaries)。

Responses 使用流式传输，收到完整的 `response.completed` 才解析和提交动作序列。流中断、失败或截断时不执行半段动作；流式传输不改变四组的回合和行动定义。

`max_output_tokens` 与上下文窗口是两个不同概念。配置还必须记录供应商声明的上下文窗口大小；正式实验要求上下文窗口至少为 `128000`，更大的窗口（例如 1M）不自动改变输出上限。模型的实际输出上限和上下文窗口都必须写入实验元数据。

## 7. `constraints` 约束字段

```yaml
constraints:
  forbid_refresh: true
  forbid_navigation: true
  require_done_action: true
  forbid_text_only_completion: true
```

| 字段 | 类型 | 允许值 | 含义 |
| --- | --- | --- | --- |
| `forbid_refresh` | 布尔值 | 必须为 `true` | 禁止 `F5`、`Ctrl+R`、`browserrefresh` 等刷新操作。 |
| `forbid_navigation` | 布尔值 | 必须为 `true` | 禁止后退、前进、主页、重新打开页面或离开当前游戏页面。 |
| `require_done_action` | 布尔值 | 必须为 `true` | 游戏成功或机会耗尽后，必须提交明确的 `DONE` 动作。 |
| `forbid_text_only_completion` | 布尔值 | 必须为 `true` | 纯文本回复不能结束任务。 |

这些约束同时写入 system prompt，并由动作解析器执行。提示词与解析器冲突时，以解析器拒绝非法动作的结果为准。

## 8. 四份配置的规范值

四个配置文件必须使用以下能力组合；只有 `system_prompt`、`description`、`api.model` 和明确记录的 API 实验参数可以按实验需要调整。

```yaml
# vanilla-claude-sonnet-5.yaml
config_version: realtime-agent-config/1.0
agent_id: vanilla
display_name: Vanilla Agent
observation:
  current_screenshot: true
  historical_video:
    enabled: false
    tool_name: get_frames
    max_queries_per_turn: 0
    max_times_per_query: 8
action:
  mode: atomic
  max_actions_per_turn: 1
```

```yaml
# anticipatory-claude-sonnet-5.yaml
config_version: realtime-agent-config/1.0
agent_id: anticipatory
display_name: anticipatory agent
observation:
  current_screenshot: true
  historical_video:
    enabled: false
    tool_name: get_frames
    max_queries_per_turn: 0
    max_times_per_query: 8
action:
  mode: sequence
  max_actions_per_turn: 100
```

```yaml
# video-claude-sonnet-5.yaml
config_version: realtime-agent-config/1.0
agent_id: video
display_name: Video Agent
observation:
  current_screenshot: true
  historical_video:
    enabled: true
    tool_name: get_frames
    max_queries_per_turn: 0
    max_times_per_query: 8
action:
  mode: atomic
  max_actions_per_turn: 1
```

```yaml
# combine-claude-sonnet-5.yaml
config_version: realtime-agent-config/1.0
agent_id: combine
display_name: combine Agent
observation:
  current_screenshot: true
  historical_video:
    enabled: true
    tool_name: get_frames
    max_queries_per_turn: 0
    max_times_per_query: 8
action:
  mode: sequence
  max_actions_per_turn: 100
```

以上示例省略了四份文件中必须重复出现的 `context`、`api`、`constraints` 和 `system_prompt` 字段。正式文件不能依赖未定义的默认值；每个字段都必须显式写出。

## 9. 校验规则

解析器至少执行以下检查：

1. `config_version`、`agent_id`、`display_name` 与文件路径和稳定名称一致。
2. 四个 Agent 的 `current_screenshot`、`allow_done`、`allow_wait` 必须为 `true`，`allow_fail` 必须为 `false`。
3. `historical_video.enabled=false` 时，不注册 `get_frames` 工具，也不允许模型发起该工具调用。
4. `action.mode=atomic` 时，`max_actions_per_turn` 必须为 `1`，并拒绝第二个动作工具调用。
5. `action.mode=sequence` 时，动作工具调用数量不能超过 `max_actions_per_turn`，上限为 `100`。
6. `tool_format` 必须为 `native`；旧的文本 JSON tool protocol 不属于正式实验配置。
7. `context.history_policy` 必须为 `full`，不得按回合数静默截断上下文。
8. `api.context_window_tokens` 和 `api.max_output_tokens` 都必须不小于 `128000`；供应商不支持时拒绝启动。
9. `api.thinking.enabled` 和 `api.thinking.summary` 必须为 `true`，`api.thinking.effort` 必须被供应商适配器明确处理。
10. 四个约束字段必须全部为 `true`，不能通过配置关闭刷新和页面导航保护。
11. system prompt 必须是非空多行字符串，并明确当前 Agent 的感知、动作和终止规则。

## 10. `computer_13` 实时动作空间审计

当前协议沿用 `computer_13` 的动作名称，并增加了显式时序字段。正式实验前仍必须单独审计并记录：

- 鼠标移动、拖拽的显式持续时间是否与录屏时间轴一致；
- `WAIT`、动作序列和最终截图的时间语义是否一致；
- 坐标系、浏览器边框偏移和 VM 屏幕尺寸是否统一；
- 是否需要新增实时专用动作字段（例如持续时间或目标执行时间）。

如果动作空间需要新增字段，应升级动作 schema 版本，并同时更新 Agent、VM 执行器、轨迹记录和测试；不能只在配置文件中自行添加未定义参数。

配置文件只描述 Agent 能力和模型交互协议，不描述具体游戏答案、隐藏状态、评分规则或 API 密钥。游戏任务 instruction 仍由 OSWorld 任务 JSON 提供。
