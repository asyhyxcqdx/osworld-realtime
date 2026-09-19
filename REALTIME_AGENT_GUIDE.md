# Realtime Agent：配置与运行

面向维护者：YAML 字段协议、实际程序流程、日志字段和轨迹查看器。模型名单、协议、输出上限、坐标适配与密钥变量见 [模型配置表](configs/realtime_agents/README.md)；运行参数与镜像见 [部署与运行](SETUP_GUIDELINE_CN.md)。

---

## 1. 配置文件

版本 `realtime-agent-config/1.0`，共 40 份：10 款模型 × 4 组。

文件命名 `<agent_id>-<model>.yaml`，agent1/2/3/4 分别映射 vanilla/anticipatory/video/combine。`--agent_config` 可显式指定文件；`--model` 若同时给出必须与文件 `api.model` 一致，缺失文件直接报错，不回退其他模型配置。不带 `--model` 时 `run_multienv.py` 默认 `claude-fable-5`，而批量脚本 `run_realtime_batch.py` 不传 `--models` 时默认八款实验模型。

**顶层字段**：config_version、agent_id、display_name、description、observation、action、context、api、constraints、system_prompt、user_prompt；未知顶层字段会被拒绝。两段 prompt 都必须为非空字符串，**YAML 是它们的唯一来源**（代码里没有默认 prompt）：`system_prompt` 是反作弊与评测诚信规则，实际 system = YAML 原文 + `api.coordinate_system` 对应的坐标约定，完整文字保存在每次运行的 `system_prompt.txt`；`user_prompt` 是每个变体的任务说明（角色、实时约束、执行策略、工具与感知、规则与收尾），**替换掉任务配置里那句通用 instruction**，作为对话开头的唯一一条任务 user 消息。

**能力开关必须与 ID 一致**，防止 Agent3 意外获得 sequence 能力：

| 配置 ID | action.mode | max_actions_per_turn | historical_video.enabled |
| --- | --- | ---: | --- |
| vanilla | atomic | 1 | false |
| anticipatory | sequence | 100 | false |
| video | atomic | 1 | true |
| combine | sequence | 100 | true |

- **action**：sequence 上限可设 1–100（默认 100）；所有组允许 DONE、WAIT，禁止 FAIL。
- **observation**：`current_screenshot` 必须 true；`historical_video` 工具名固定 `get_frames`、`max_times_per_query` 固定 8；`max_queries_per_turn=0` 表示不限（无历史帧能力的组只能为 0）。注意区分：帧数、每次回复的调用数、每回合的查询总数和动作决策数是四个不同概念。
- **context**：固定 `history_policy=full`、`include_screenshots=true`、`on_context_limit=fail_with_explicit_error`，禁止静默删掉早期上下文。
- **api**：model、protocol、可选 key_env、`tool_format=native`、context_window_tokens、max_output_tokens、temperature、thinking。`context_window_tokens` 统一声明 1000000，**只用于配置校验**，不发送给 API、也不在本地估算或截断，真实容量由服务端判断；输出上限是正整数且不能超过声明窗口（Gemini 3.8 Flash 为 65536 并拒绝更大的值，其余现用模型 128000），它也不是实际生成量。`thinking.enabled`/`summary` 必须 true，MiniMax M3 的 `effort` 为 null（只开 adaptive）、其余为 high，`temperature` 为 null。HTTP 错误不会自动降预算或换模型。
- **constraints**：必须完整包含 forbid_refresh、forbid_navigation、require_done_action、forbid_text_only_completion，且都为 true。
- **坐标**：旧 `coordinate_mapping` 被拒绝；`api.coordinate_system` 可选 `native_pixels`（缺省）、`normalized_0_1000`（Gemini）、`normalized_0_1000_unclipped`（MiniMax），未知值在启动 VM 前被拒绝。该字段经 `agent_kwargs()` 传入 `RealtimeAgent`，统一决定追加的 prompt、模型工具的 `x/y` schema 和提交前的换算；换算细节与上游对照见 [模型配置表](configs/realtime_agents/README.md#协议和坐标适配)。实时入口只接受 1920×1080，所有模型接收原始 PNG。协议名保存在日志中，HTML 按该次记录画标记，不按当前模型名重新解释旧轨迹。校验不通过时整段动作不执行。

**密钥与网关**：`api.key_env` 指定读哪个环境变量（校验只允许合法变量名），启动时缺失即报错；没配到时回退 `REALTIME_API_KEY` → `PACKY_API_KEY`。这些变量和网关地址（`REALTIME_API_BASE_URL` / `PACKY_API_BASE_URL` / 按协议的 `ANTHROPIC_BASE_URL`、`OPENAI_BASE_URL`）都可以写在仓库根目录的 `.env` 里，见 [部署与运行](SETUP_GUIDELINE_CN.md)。API 密钥与请求授权头不落盘。

---

## 2. 程序流程

批量入口是 `scripts/python/run_realtime_batch.py`（逐模型记账、可续跑），它按模型顺序调用 `scripts/python/run_multienv.py`。

1. `run_multienv.py` 按模型和变体加载 YAML，校验 ID、感知与动作能力，再用统一 `agent_kwargs()` 创建 Agent；`system_prompt` 缺失或为空直接报错。
2. `run_realtime_example()` 执行任务 reset；必要时安装服务；核对 VM 两份 realtime 源码哈希，开始 30 FPS 目标录像。
3. 对话开头固定一条任务 user 消息，文字来自 YAML 的 `user_prompt`（每个变体一套：角色、实时约束、执行策略、工具与感知、规则与收尾；`video`/`combine` 才有 `get_frames` 相关段落）。配置里没有 `user_prompt` 时才回退成任务配置那句 `Task: <instruction>`。每个决策轮只追加截图时间和当前原始 1920×1080 截图；每次请求包含任务说明、完整历史回复及工具结果和本轮观测，单次请求内任务说明只有一份。
4. 如模型调用 `get_frames`，VM 按所给秒数读取已完成片段并返回图片与实际时间，宿主保存查询图片并回填 API；可重复，不增加决策数。
5. 如模型提交动作，先确认回复未因输出上限截断，再由 Agent1/3 校验恰好一个、Agent2/4 校验 1–100 个；注册键名、参数范围和 DONE 位置都要合法。
6. VM 按顺序执行整段动作，之后只返回一个当前截图；KEY_DOWN 持续到 KEY_UP，WAIT 的 `duration_s` 在 VM 内睡眠，普通动作没有隐含 pause。
7. 完整 VM 参数、实际开始/结束/耗时及截图写入 `trajectory.jsonl`。按相同调用 ID 回传模型时，每个动作只保留自己的动作类型、状态字段和实测时间，**不回显 VM 坐标、不重复附带整段执行流水**；未记录的时间不补造。
8. 模型调用 `computer_done({})`，运行时转换为内部 DONE 并结束循环，读取 BENCH、保存 `result.json`/`result.txt`；**落分之前还会调一次轨迹判官**（`mm_agents/realtime_auditor.py`，见 [`AGENTS.md`](AGENTS.md) 第 4 节）：判为 `CHEAT` 就把最终分写成 0，判官没跑成功则不写 `result.txt`（那条任务重跑）；`result.json` 里的 `pass_at_1`/`pass_at_3` 始终是游戏原值，另加一个 `judge` 块记判官结论；录制在 finally 中停止，下载 MP4、索引和日志，然后自动生成只读 `trajectory.html`（失败的运行也展示已有事件，导出失败不改变实验结果）。

正式游戏在 reset 后记录页面身份，并在每次动作后与评分前比较标签页、URL（不含 hash）和加载时间：重载、换页或复制页面时写 `run_error` 并保持未评分。该检查在环境侧，不提供给模型，也不读 `__dbg`。

示例：`computer_press({"key":"space"})` 转为 `{"action_type":"PRESS","parameters":{"key":"space"}}`（空格键不是字面空格）。模型未提交的动作不会从文字计划里自动补出。

**错误处理**：Agent3 同一回复多个 `get_frames`、混合查询与动作、非法参数或文字代替工具时，该回复的动作一概不执行，先回传每个调用 ID 的错误结果，再请求纠正，最多两次；执行阶段出错则不重放序列。模型知道截图时间与之前动作的实际时间，不接收持续更新的视频流，自己决定查询的时间点与数量；模型生成回复期间游戏与录屏仍在继续。

**结束口径**：正常结束或达到决策上限后读取 BENCH，未成功记有效 0 分，结束原因分别是 `done` / `decision_limit`；游戏内部 ready/running 状态照实保存，无有效成绩的异常不会伪造成游戏失败。**跨模型继续需要批量的 `--keep-going`**，单个模型内的任务失败本来就会继续下一个任务；reset、初始页面检查和录制启动等早期错误可能只有运行日志。

---

## 3. 三种 API 协议

动作通过 API tools schema 提供，不把工具调用当文本 JSON 解析。Messages 使用 adaptive thinking；MiniMax 不发送 effort/display；Responses 使用 `reasoning.effort=high`、`summary=auto`；Gemini Chat 使用 `extra_body.google.thinking_config={thinking_level: high, include_thoughts: true}` 且不同时发送 `reasoning_effort`——**Chat 思考配置目前只为 `gemini-3.8-flash` 开通**，扩展参数不会发给任意 Chat 模型。

三种协议都请求流式回复并收齐完成标志后才解析、提交动作，不执行中途收到的半段序列：Messages 按块拼接文本、思考、签名和工具 JSON；Chat 按索引拼接工具调用、思考扩展及最终 usage；Responses 等待完整 `response.completed`。读取超时保持 120 秒，HTTP 暂时错误及读取超时/断连最多尝试三次，失败重试丢弃该次不完整回复；模型的 length/max_tokens 等输出截断仍明确停止。请求记录 `stream_requested`，回复记录实际是否按 SSE 返回的 `stream_received`（供应商忽略流式而返回普通 JSON 时标 false，不冒充成功）。

Gemini Chat 的取帧回图封装：结果含图片时 `role=tool` 只说明结果在后续图片消息里并保留调用 ID，随后的 `role=user` 按调用 ID 分组，依次放查询完成时间、各帧信息和图片，详细时间与状态只发一份；部分帧失败时状态仍保留在该组中，整次查询没有图片时详细结果直接留在 `role=tool`。Messages / Responses 的取帧封装保持原有结构。

---

## 4. 日志字段

三份关键记录必须对齐，不能只凭模型计划认定动作发生：

| 记录 | 含义 |
| --- | --- |
| `model_response.calls` | 模型原始调用 |
| `action_executed.info.sequence_actions` | VM 实际执行的动作 |
| `tool_result.frames` | 历史帧实际返回 |

`model_request.request_messages` 保存本次请求的完整消息快照，图片数据在日志中替换为哈希和长度；不同请求会重复显示同一段历史，新的决策轮不会再新增任务说明。模型回复进入后续请求的 assistant 消息（Responses 用原生输出项），Claude 的工具结果放在 user 消息的 tool_result 块里，**因此 user 不都代表任务指令**。每次请求的配置、图像哈希、原始回复、用量、动作与 VM 时长都落盘。

`trajectory.jsonl` 里会出现的事件（写分析脚本时按事件名过滤，不要假设所有事件都有相同字段）：

| 事件 | 含义 |
| --- | --- |
| `initial_observation` | 任务开始时的状态与首张截图 |
| `model_request` / `model_response` | 每次模型请求与回复（回复含 `calls`、`usage`、思考、`stream_received`） |
| `format_error` | 回复格式非法，已回传错误并要求纠正 |
| `model_error` | 模型/接口层失败（该次请求没有可用回复） |
| `tool_result` | `get_frames` 的返回（`result.frames[].status` = ok / not_ready / error） |
| `frame_query_artifacts` | 取帧图片落盘记录（`query_<n>_<i>.png` 与请求/实际时刻） |
| `action_submitted` | 模型本轮提交的完整动作序列 |
| `action_executed` | VM 实际执行结果（`info.sequence_actions` 含实测耗时） |
| `action_tool_result` | 每个动作工具调用回传给模型的回执 |
| `action_rejected` | 命中了禁用快捷键，动作未发送（**该次运行没有有效成绩**） |
| `action_execution_error` | 执行阶段报错（含被拒绝动作的执行错误详情） |
| `evaluation` | 读分结果与 `termination_reason` |
| `run_error` | 主循环异常结束（无有效成绩） |

---

## 5. 轨迹查看器

每次运行结束后，任务目录会自动生成 `trajectory.html`（停止录屏之后生成，失败只记警告，不影响成绩或动作时序）。**直接双击打开即可离线查看，不需要模型或 VM**，原始 `trajectory.jsonl`、截图和录像不会被改写。

页面内容：

- 左侧按决策轮和事件排列，可搜索、筛选模型回复/动作/历史帧/异常，键盘左右箭头切换事件。
- 截图可放大，鼠标悬停显示原图像素坐标；动作标记只画在对应的**模型输入截图**上，不把旧坐标画在执行后的新画面上。箭头是 HTML 的坐标标注，不是从截图里提取的真实鼠标位置（可关闭“动作坐标标注”看原图）。
- 模型文字、供应商实际返回的思考或摘要、工具名称与参数分别显示；没有思考文本时会明确标注，不能解释成"模型没思考"。
- 历史帧显示请求时刻、实际取帧时刻和 `ok/not_ready/error`（没取到的帧不会补造图片）；执行动作显示 VM 开始/结束时刻、请求时长与实际耗时，便于核对 WAIT。
- 每次请求的完整消息历史可展开（按原日志顺序）；评分显示原日志的 evaluation 状态和 pass@1/pass@3（提交 DONE 不会被解释成游戏成功）；用量显示供应商原始 usage 字段，不自动换算费用。

“日志时间”是宿主机记录事件的时间，“截图/历史帧时间”是 VM 录屏时间轴，模型请求耗时包含网络与排队、不是纯思考时间。

**转换已有轨迹**（从仓库根目录执行）：

```bash
python scripts/python/render_realtime_trajectory.py /path/to/task/trajectory.jsonl
python scripts/python/render_realtime_trajectory.py /path/to/task          # 传任务目录
python scripts/python/render_realtime_trajectory.py /path/to/results --recursive
python scripts/python/render_realtime_trajectory.py <输入> --output /path/to/view.html
```

HTML 内嵌截图，单独复制也能看图；相同图片和历史消息只存一份，但**每个请求仍完整保留原来的消息、重复次数和顺序**。`recording.mp4` 不嵌入 HTML（避免页面变成巨大的视频副本），要看录像请保留原结果目录；缺少录像不影响截图与文字浏览。查看器只用 Python 标准库导出，浏览器端不依赖网络、CDN 或第三方脚本，重导出不会改动原始日志或已发送给模型的消息。
