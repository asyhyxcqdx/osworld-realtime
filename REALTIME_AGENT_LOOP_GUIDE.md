# 实时 Agent loop

本文件描述实际程序流程。批量入口是 `scripts/python/run_realtime_batch.py`（逐模型记账、可续跑），它按模型顺序调用 `scripts/python/run_multienv.py`；四组配置见 [配置协议](REALTIME_AGENT_CONFIG_PROTOCOL.md)，运行参数见 [部署与运行](SETUP_GUIDELINE_CN.md)。

1. `run_multienv.py` 按模型和变体加载 YAML，校验 ID、感知与动作能力，再用统一 `agent_kwargs()` 创建 Agent；YAML 的 `system_prompt` 是唯一 prompt 来源，缺失或为空直接报错，代码里没有默认 prompt。
2. `run_realtime_example()` 执行任务 reset；必要时安装服务；核对 VM 两份 realtime 源码哈希，开始 30 FPS 目标录像。
3. 对话开头固定一条任务说明 user 消息，保留阅读游戏规则、继续尝试、禁止导航这三句；结束工具要求只放在 system prompt，避免重复。每个决策轮只追加截图时间和当前原始 1920×1080 截图。每次 API 请求包含任务说明、完整历史回复及工具结果和本轮观测，单次请求内任务说明只有一份。YAML system prompt 后追加 api.coordinate_system 对应的坐标约定，API tools 字段使用同一协议的 schema。Gemini、MiniMax 的相对坐标在动作校验/提交前转换成原生像素，原始 assistant 历史保持模型返回值；其他模型原样传递。
4. 如模型调用 get_frames，VM 按所给秒数读取已完成视频片段，返回图片及实际时间；宿主保存查询图片并把结果回填 API。可重复，不增加决策数。Agent3 一次回复至多一个 get_frames，收到结果后才能再次查询；Agent4 允许一次回复多个 get_frames。
5. 如模型提交动作，先确认回复未因输出上限截断，再由 Agent1/3 校验恰好一个，Agent2/4 校验 1–100 个；注册键名、参数范围和 DONE 位置都需合法。显式 length/max_tokens/incomplete 响应保留原始记录并停止，不执行半段序列。
6. VM 按顺序执行整段动作，之后只返回一个当前截图；截图紧跟执行结束取得，不额外等待界面重绘，所以它可能仍显示动作前或动作中的画面。KEY_DOWN 持续到 KEY_UP；WAIT 的 duration_s 在 VM 内睡眠，普通动作没有隐含 pause。
7. 完整 VM 参数、实际开始/结束/耗时及截图写入 trajectory.jsonl。按相同调用 ID 回传模型时，每个动作仅保留自己的动作类型、现有状态字段和实测时间，不回显 VM 坐标、不重复附带整段执行流水；未记录的时间不补造。模型原始调用保持原样，新截图引出下一次动作决策。
8. 模型调用 `computer_done({})`，运行时转换为内部 DONE 动作并结束循环，读取游戏 BENCH，保存 result.json/result.txt 和汇总；录制在 finally 中停止，下载 MP4、索引和日志，然后自动生成只读 `trajectory.html`；失败的运行也展示已有事件，HTML 导出失败不改变实验结果。

正式游戏在 reset 后记录页面身份，在每次动作后及评分前比较标签页、URL 和加载时间。重载、换页或复制页面时写 run_error 并保持未评分。该检查发生在环境侧，不提供给模型，也不读取 __dbg。

各配置的 system prompt 都要求先探索环境与游戏机制、谨慎试探、珍惜每次 attempt（首句与动作时序说明四组一致，写在各 YAML 里）。模型可见的截图时间与工具结果时间保留三位小数，内部计时、动作执行、取帧选择和原始日志保留原值。

任务正常结束或达到决策上限后读取 BENCH：未成功记有效 0 分，结束原因分别是 `done` / `decision_limit`；游戏内部 ready/running 状态照实保存，无有效成绩的异常不会伪造成游戏失败。主循环中的异常保存执行/运行错误和指标；**跨模型继续需要批量的 `--keep-going`**，单个模型内的任务失败本来就会继续下一个任务。reset、初始页面检查和录制启动等早期错误可能只有运行日志。

示例：`computer_press({"key":"space"})` 转为 `{"action_type":"PRESS","parameters":{"key":"space"}}`。空格键不是字面空格。模型未提交的动作不会从文字计划中自动补出。

Agent3 同一回复多个 `get_frames`、混合查询与动作、非法参数或文字代替工具时，该回复的动作一概不执行：先回传每个调用的错误结果，再请求纠正，最多两次；执行阶段出错则不重放序列，以免重复已执行的前缀。

模型知道截图时间与之前动作的实际时间，不接收持续更新的视频流；它自己决定历史查询的时间点与数量（单次 1–8 张，`not_ready` 没有图片）。模型生成回复期间，游戏和录屏仍在继续。

Gemini 当前使用 Chat 接口。取帧结果包含图片时，`role=tool` 只说明结果位于后续图片消息，并保留对应调用 ID；后续 `role=user` 按调用 ID 分组，依次放查询完成时间、各帧信息和图片，详细时间与状态只发送一份。部分帧失败时，其状态仍在该组中保留；整次查询没有图片时，详细结果直接留在 `role=tool`，不新增图片消息。Messages/Responses 的取帧封装保持原有结构。

三种模型 API 协议均请求流式回复。Messages 按块拼接文本、思考、签名和工具 JSON；Chat 按索引拼接工具调用、思考扩展及最终 usage；Responses 等待完整 response.completed。收齐协议完成标志后才解析并提交动作，不能执行中途收到的半段序列。读取超时保持 120 秒，HTTP 暂时错误及读取超时/断连最多尝试三次；失败重试丢弃该次不完整回复。模型的 length/max_tokens 等输出截断仍明确停止。请求记录 stream_requested，回复记录实际是否按 SSE 返回的 stream_received；供应商忽略流式请求而返回普通 JSON 时标为 false，不冒充流式成功。

当前三份重要记录：`model_response.calls` 是原始调用，`action_executed.info.sequence_actions` 是 VM 实际执行，`tool_result.frames` 是历史帧实际返回。三者应对齐，不能只凭模型计划认定动作发生。

`model_request.request_messages` 保存本次请求的完整消息快照，图片数据在日志中替换为哈希和长度。不同请求记录会重复显示同一段历史；新的决策轮不会再新增任务说明。模型回复进入后续请求的 assistant 消息（Responses 协议使用原生输出项）；Claude 的工具结果放在 user 消息中的 tool_result 块，因此 user 不都代表任务指令。

可视化页面与已有轨迹转换命令见 [HTML 轨迹查看器](REALTIME_TRAJECTORY_VIEWER.md)。
