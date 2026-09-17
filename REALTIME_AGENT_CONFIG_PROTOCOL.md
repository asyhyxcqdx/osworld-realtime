# Realtime Agent 配置协议

版本：`realtime-agent-config/1.0`。共 40 份 YAML：8 款实验模型 × 4 组 + Fable 5、Astra 的 8 份历史基线。模型、协议、预算与密钥变量见 [配置目录说明](configs/realtime_agents/README.md)。

## 选择配置

文件命名 `<agent_id>-<model>.yaml`。agent1/2/3/4 分别映射 vanilla/anticipatory/video/combine。`--agent_config` 可显式指定文件；`--model` 若同时给出必须与文件 `api.model` 一致，缺失文件直接报错，不回退其他模型配置。不带 `--model` 时 `run_multienv.py` 默认 `claude-fable-5`，而批量脚本 `run_realtime_batch.py` 不传 `--models` 时默认八款实验模型。

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

旧 `coordinate_mapping` 仍被拒绝。`api.coordinate_system` 可选 `native_pixels`（缺省）、`normalized_0_1000`（Gemini）、`normalized_0_1000_unclipped`（MiniMax），未知值在启动 VM 前被拒绝。该字段经 `agent_kwargs()` 传入 `RealtimeAgent`，统一决定追加的 prompt、模型工具 `x/y` schema 和提交前的换算；换算与端点行为、上游对照见 [模型配置表](configs/realtime_agents/README.md#协议和坐标适配)。实时入口只接受 1920×1080，所有模型接收原始 PNG。协议名保存在日志中，HTML 按该次记录画标记，不按当前模型名重新解释旧轨迹。校验不通过时整段动作不执行，沿用工具错误纠正。

## 消息与错误

动作通过 API tools schema 提供，不把工具调用当文本 JSON 解析。混合或非法动作响应不执行其中的任何动作，所有调用 ID 获得对应错误结果，再允许模型纠正，最多两次；Agent3 同一回复中的多个 `get_frames` 也整批拒绝，**不执行查询、不消耗查询预算**，只回传每个调用 ID 的错误结果。

Messages 使用 adaptive thinking；MiniMax 不发送 effort/display；Responses 使用 `reasoning.effort=high`、`summary=auto`。Gemini Chat 使用 `extra_body.google.thinking_config={thinking_level: high, include_thoughts: true}`，不同时发送 `reasoning_effort`；**Chat 思考配置目前只为 `gemini-3.8-flash` 开通**，扩展参数不会发给任意 Chat 模型。每次请求的配置、图像哈希、原始回复、用量、动作与 VM 时长都落盘。

密钥与网关：`api.key_env` 指定读哪个环境变量（校验只允许合法变量名），启动时缺失即报错；没配到时会回退 `REALTIME_API_KEY` → `PACKY_API_KEY`。这些变量和网关地址（`REALTIME_API_BASE_URL` / `PACKY_API_BASE_URL` / 按协议的 `ANTHROPIC_BASE_URL`、`OPENAI_BASE_URL`）都可以写在仓库根目录的 `.env` 里，见 [部署与运行](SETUP_GUIDELINE_CN.md)。API 密钥与请求授权头不落盘。
