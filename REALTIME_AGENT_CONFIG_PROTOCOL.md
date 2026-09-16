# Realtime Agent 配置协议

版本：`realtime-agent-config/1.0`。新增 Packy 八模型 × 四组共 32 份 YAML，保留 Fable 5、Astra 的 8 份基线配置，合计 40 份。模型、协议、预算与密钥分组见 [配置目录说明](configs/realtime_agents/README.md)。

## 选择配置

文件命名 `<agent_id>-<model>.yaml`。agent1/2/3/4 分别映射 vanilla/anticipatory/video/combine。默认模型为 claude-fable-5；`--agent_config` 可显式指定文件。`--model` 若同时给出必须与文件 api.model 一致；缺失文件直接报错，不回退其他模型配置。

## 字段

顶层字段为 config_version、agent_id、display_name、description、observation、action、context、api、constraints、system_prompt。未知顶层字段会被拒绝。system_prompt 必须为非空字符串，实际 system = YAML prompt + api.coordinate_system 对应的坐标约定。完整实际文字保存在每次运行的 system_prompt.txt。

| 配置 ID | action.mode | max_actions_per_turn | historical_video.enabled |
| --- | --- | ---: | --- |
| vanilla | atomic | 1 | false |
| anticipatory | sequence | 100 | false |
| video | atomic | 1 | true |
| combine | sequence | 100 | true |

校验器要求能力开关与 ID 一致，防止 Agent3 意外拥有 sequence。sequence 上限可设 1–100；默认 100。所有组允许 DONE、WAIT，禁止 FAIL。

observation.current_screenshot 必须 true。historical_video 的工具名固定 get_frames、max_times_per_query 固定 8；max_queries_per_turn=0 表示不限（无录像能力的组只能为 0）。Agent3 每次模型回复至多一个 get_frames；查询返回后可在后续回复继续查询。Agent4 可同一回复提交多个查询。帧数、每次回复的调用数、每回合的查询总数和动作决策数是不同概念。

context 固定 history_policy=full、include_screenshots=true、on_context_limit=fail_with_explicit_error；禁止静默删掉早期上下文。

api 定义 model、protocol、可选 key_env、tool_format=native、context_window_tokens、max_output_tokens、temperature、thinking。上下文声明统一为 1000000；该字段目前用于配置校验，不发送给 API，也不在本地估算或截断实际输入，真实容量由模型服务端判断。输出上限是正整数且不能超过声明窗口。Gemini 3.8 Flash 配置为 65536，并拒绝超出其上限的配置；其他现用模型为 128000。输出上限不是实际生成量。thinking.enabled 和 summary 必须 true；MiniMax M3 的 effort 为 null（只开 adaptive），其余当前配置为 high；temperature 为 null。HTTP 错误不会自动降低预算或换模型。

constraints 必须完整包含 forbid_refresh、forbid_navigation、require_done_action、forbid_text_only_completion，且都为 true。

旧 coordinate_mapping 仍被拒绝。新 api.coordinate_system 可选 native_pixels（缺省）、normalized_0_1000（Gemini）、normalized_0_1000_unclipped（MiniMax），并保留 normalized_0_999 供显式旧配置与轨迹读取；未知值在启动 VM 前被拒绝。该字段经 agent_kwargs 传入 RealtimeAgent，统一决定追加 prompt、模型工具 x/y schema 和转换。实时 CLI 仍拒绝非 1920×1080 的屏幕设置，所有模型接收原始 PNG。相对协议均按 1000 为分母映射，端点分别遵循上游：Gemini 将 1000 限定到最后一个屏幕像素；MiniMax 直接换算为对应屏幕尺寸，不加截边。协议名保存在日志中，HTML 使用该次记录的规则，不按当前模型名重新解释旧轨迹。校验不通过时整段动作不执行，沿用工具错误纠正。其余模型使用 native_pixels 原样传递，禁止按返回值猜比例。完整规则和官方来源见 [模型配置说明](configs/realtime_agents/README.md#协议和坐标适配)。

## 消息与错误

动作通过 API tools schema 提供，不把工具调用当文本 JSON 解析。Video/combine prompt 明确要求先单独查帧，收到结果后再提交动作；只读历史查询不增加决策回合。混合或非法动作响应不执行其中的任何动作，所有调用 ID 获得对应错误结果，再允许模型纠正，最多两次。Agent3 同一回复中的多个 get_frames 也整批拒绝，不执行任何查询、不消耗查询预算，回传每个调用 ID 的错误结果。

Messages 使用 adaptive thinking；MiniMax 不发送 effort/display；Responses 使用 reasoning.effort=high、summary=auto。Gemini Chat 使用 extra_body.google.thinking_config={thinking_level: high, include_thoughts: true}，不同时发送 reasoning_effort。Chat 思考配置目前只为 gemini-3.8-flash 开通；不把 Gemini 扩展参数发送给任意 Chat 模型。每次请求的配置、图像哈希、原始回复、用量、动作与 VM 时长都落盘。api.key_env 指定的环境变量是唯一密钥来源，缺失即报错；未指定时保留旧的密钥兜底规则。API 密钥、请求授权头不落盘；Packy 地址由 --api_base_url 指定，系统代理保留。
