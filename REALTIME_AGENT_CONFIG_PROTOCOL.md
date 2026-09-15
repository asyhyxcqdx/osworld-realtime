# Realtime Agent 配置协议

版本：`realtime-agent-config/1.0`。正式配置目录只有两模型 × 四组共 8 份 YAML。模型为 `claude-fable-5`（Anthropic Messages）和 `gpt-6-astra`（OpenAI Responses）；旧 Fable5.1、Opus、Sonnet 试验配置从活动目录移除，可由 Git 历史恢复。

## 选择配置

文件命名 `<agent_id>-<model>.yaml`。agent1/2/3/4 分别映射 vanilla/anticipatory/video/combine。默认模型为 claude-fable-5；`--agent_config` 可显式指定文件。`--model` 若同时给出必须与文件 api.model 一致；缺失文件直接报错，不回退其他模型配置。

## 字段

顶层字段为 config_version、agent_id、display_name、description、observation、action、context、api、constraints、system_prompt。未知顶层字段会被拒绝。system_prompt 必须为非空字符串，实际 system = YAML prompt + 原生坐标约定。完整实际文字保存在每次运行的 system_prompt.txt。

| 配置 ID | action.mode | max_actions_per_turn | historical_video.enabled |
| --- | --- | ---: | --- |
| vanilla | atomic | 1 | false |
| anticipatory | sequence | 100 | false |
| video | atomic | 1 | true |
| combine | sequence | 100 | true |

校验器要求能力开关与 ID 一致，防止 Agent3 意外拥有 sequence。sequence 上限可设 1–100；默认 100。所有组允许 DONE、WAIT，禁止 FAIL。

observation.current_screenshot 必须 true。historical_video 的工具名固定 get_frames、max_times_per_query 固定 8；max_queries_per_turn=0 表示不限（无录像能力的组只能为 0）。帧数、查询次数和动作决策数是不同概念。

context 固定 history_policy=full、include_screenshots=true、on_context_limit=fail_with_explicit_error；禁止静默删掉早期上下文。

api 定义 model、protocol、tool_format=native、context_window_tokens、max_output_tokens、temperature、thinking。上下文与输出上限均至少 128000；输出上限不是实际生成量。当前 Fable5 上下文声明 1000000，Astra 为 1050000。thinking.enabled 和 summary 必须 true，effort 当前为 high；temperature 为 null。模型能力声明需与实际供应商相符，HTTP 错误不能自动降低预算或换模型。

constraints 必须完整包含 forbid_refresh、forbid_navigation、require_done_action、forbid_text_only_completion，且都为 true。

coordinate_mapping 已移除，带该字段的旧自定义配置会被明确拒绝。所有模型原样接收 1920×1080 PNG、原样执行原生坐标；没有猜比例、预缩放或自动放大。

## 消息与错误

动作通过 API tools schema 提供，不把工具调用当文本 JSON 解析。Video/combine prompt 明确要求先单独查帧，收到结果后再提交动作；只读历史查询不增加决策回合。混合或非法动作响应不执行其中的任何动作，所有调用 ID 获得对应错误结果，再允许模型纠正，最多两次。

Anthropic 使用 adaptive thinking；Astra 使用 reasoning.effort=high、summary=auto。每次请求的配置、图像哈希、原始回复、用量、动作与 VM 时长都落盘。API 密钥、请求授权头不落盘；Packy 地址由 --api_base_url 指定，系统代理保留。
