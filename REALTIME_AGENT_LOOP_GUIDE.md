# 实时 Agent loop

本文件描述实际程序流程；四组配置见 [配置协议](REALTIME_AGENT_CONFIG_PROTOCOL.md)，运行命令见 [部署说明](SETUP_GUIDELINE_CN.md)。

1. `run_multienv.py` 按模型和变体加载 YAML，校验 ID、感知与动作能力，再使用统一 `agent_kwargs()` 创建 Agent。
2. `run_realtime_example()` 执行任务 reset；必要时安装服务；核对 VM 两份 realtime 源码哈希，开始 30 FPS 目标录像。
3. 对话开头固定一条任务说明 user 消息，每个决策轮只追加截图时间和当前原始 1920×1080 截图。每次 API 请求包含这条任务说明、完整历史回复及工具结果和本轮观测，单次请求内任务说明只有一份。YAML system prompt 后统一追加原生坐标约定。工具 schema 通过 API tools 字段发送。
4. 如模型调用 get_frames，VM 按所给秒数读取已完成视频片段，返回图片及实际时间；宿主保存查询图片并把结果回填 API。可重复，不增加决策数。Agent3 一次回复至多一个 get_frames，收到结果后才能再次查询；Agent4 允许一次回复多个 get_frames。
5. 如模型提交动作，先确认回复未因输出上限截断，再由 Agent1/3 校验恰好一个，Agent2/4 校验 1–100 个；注册键名、参数范围和 DONE 位置都需合法。显式 length/max_tokens/incomplete 响应保留原始记录并停止，不执行半段序列。
6. VM 按顺序执行整段动作，之后只返回一个当前截图。KEY_DOWN 持续到 KEY_UP；WAIT 的 duration_s 在 VM 内睡眠，普通动作没有隐含 pause。
7. 实际开始/结束/耗时、截图和执行结果写入 trajectory.jsonl，并按相同调用 ID 回传模型。新截图引出下一次动作决策。
8. 模型调用 `computer_done({})`，运行时转换为内部 DONE 动作并结束循环，读取游戏 BENCH，保存 result.json/result.txt 和汇总；录制在 finally 中停止，下载 MP4、索引和日志，然后自动生成只读 `trajectory.html`；失败的运行也展示已有事件，HTML 导出失败不改变实验结果。

正式游戏在 reset 后记录页面身份，在每次动作后及评分前比较标签页、URL 和加载时间。重载、换页或复制页面时写 run_error 并保持未评分。该检查发生在环境侧，不提供给模型，也不读取 __dbg。

示例：`computer_press({"key":"space"})` 转为 `{"action_type":"PRESS","parameters":{"key":"space"}}`。空格键不是字面空格。模型未提交的动作不会从文字计划中自动补出。

Agent3 同一回复多个 get_frames、混合 get_frames 与动作、非法参数或文字代替工具时，不执行该回复的动作；先回传每个调用的错误结果，再请求纠正，最多两次。执行阶段发生错误则不重放序列，以免重复已执行的前缀。

模型知道截图时间与之前动作的实际时间，不接收持续更新的视频流。它自己选择历史查询时间点与数量；get_frames 单次 1–8 张，not_ready 没有图片，框架不会自动重查。模型生成回复期间，游戏和录屏仍在继续。

当前三份重要记录：`model_response.calls` 是原始调用，`action_executed.info.sequence_actions` 是 VM 实际执行，`tool_result.frames` 是历史帧实际返回。三者应对齐，不能只凭模型计划认定动作发生。

`model_request.request_messages` 保存本次请求的完整消息快照，图片数据在日志中替换为哈希和长度。不同请求记录会重复显示同一段历史；新的决策轮不会再新增任务说明。模型回复进入后续请求的 assistant 消息（Responses 协议使用原生输出项）；Claude 的工具结果放在 user 消息中的 tool_result 块，因此 user 不都代表任务指令。

可视化页面与已有轨迹转换命令见 [HTML 轨迹查看器](REALTIME_TRAJECTORY_VIEWER.md)。
