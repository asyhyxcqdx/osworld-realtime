# Realtime GUI Agent 实验设计

当前状态与文件来源见 [项目总览](REALTIME_GUI_PROJECT.md) 和 [环境接入记录](evaluation_examples/REALTIME_GUI_BENCH_INTEGRATION.md)。运行模板见 [部署说明](SETUP_GUIDELINE_CN.md)。

## 受控变量

四组共享原生 1920×1080 截图、完整上下文、computer_13 工具、模型参数和 VM 执行器。区别仅为历史帧是否开放、一次决策可提交 1 个还是 1–100 个动作。正式模型为 Fable5、Astra；每模型分别比较四组，不因模型名猜测坐标比例。

每回合：当前截图 → 模型请求 → 可重复的 get_frames 查询 → 动作工具回复 → VM 连续执行 → 一个新截图。历史查询不增加动作决策数；Agent3 每次模型回复只能含一个 get_frames 或一个动作工具；拿到取帧结果后仍可继续查帧。Agent4 可在同一次回复提交多个历史查询，不能与动作混在同一响应。每个 get_frames 仍可请求 1–8 张帧。

## 工具与时序

共享动作：MOVE_TO、CLICK、MOUSE_DOWN、MOUSE_UP、RIGHT_CLICK、DOUBLE_CLICK、DRAG_TO、SCROLL、TYPING、PRESS、KEY_DOWN、KEY_UP、HOTKEY，另加 WAIT、DONE。名称沿用 computer_13，但 realtime 实际注册 15 个动作工具。

- MOVE_TO / DRAG_TO：`x/y` 原生像素，`duration_s` 必填，0–10 秒。
- WAIT：`duration_s` 必填，0–60 秒，在 VM 内执行。
- PRESS / KEY_DOWN / KEY_UP：`key` 使用注册枚举，空格为 `space`，字面空格不合法。
- KEY_DOWN 跨回合保持，直到 KEY_UP 或录制结束；模型思考期间游戏不会暂停。
- DONE 是唯一终止动作，必须最后提交。它指示 runner 读分，本身不代表通关。

VM 的 PyAutoGUI.PAUSE=0。发送序列只用一次 HTTP 请求，不在动作之间取当前截图、不隐藏等待。每个动作返回 started_s、finished_s、duration_s；系统调度可能造成小幅误差。禁止自动重放执行失败的序列，避免重复已执行前缀。

刷新/导航快捷键在宿主控制器发送前校验，包括在多个原子回合中保持修饰键的组合。正式 Agent 无 Python、CDP、文件或源码工具。协议要求及限制见 [快捷键策略](REALTIME_GUI_BENCH_SHORTCUT_POLICY.md)。

## 录像与时间轴

VM 目标录制 30 FPS、1920×1080 fMP4，目标片段长度 100 ms。时间原点为首次 X11 录制帧，截图带 task_time_s；动作时长另用单调时钟测量。30 FPS 是目标，不保证负载下每 33.3 ms 都能得到一帧。

get_frames 接受 1–8 个秒数，可小数。对已完成片段，返回最接近时间的真实记录帧，相等时取早帧；不插帧。返回 requested_time_s、actual_time_s、status、available_until_s。not_ready 没有图，模型可继续查询。框架不会自动猜时间点、自动补查或把全部录像塞给模型。

## 预算与协议

正式 YAML 固定 full history、128000 最大输出、high thinking、summary 开启。max_steps 默认 100，计动作决策；get_frames 默认不限次数。供应商耗时包含模型计算、网络与排队，不能把整个 latency_s 当成纯模型思考时间。

Anthropic 使用 Messages 原生 tool_use/tool_result；Astra 使用 Responses，收齐 response.completed 才执行，不提前执行流中的半段调用。sequence 显式允许多调用；atomic 每次模型回复最多调用一个工具。OpenAI 使用 parallel_tool_calls=false，Anthropic 使用 tool_choice={type: auto, disable_parallel_tool_use: true}；本地校验也拒绝 Agent3 同一回复中的多个 get_frames。

非法参数、文字代替工具、混合查询与动作都会回传错误后最多纠正两次；混合响应中任何调用均不执行。图片原字节发送；日志使用哈希代替 base64，原截图单独保存。模型提供的摘要仅按实际返回记录，不补写隐藏推理。

## 结果

目录为 `<result_dir>/<model>/<run_id>/<agent>/computer_13/screenshot/<domain>/<UUID>/`。包含 system_prompt.txt、experiment.json、trajectory.jsonl、当前截图、查询帧、recording.mp4、索引、录屏日志及指标。

评分读取游戏写入的 pass_at_1/pass_at_3，标量 result.txt=pass_at_3。passed/failed 才纳入终态均值；未完成和接口中断单列。原始 provider_response 和实际动作时长是核查依据。当前 C1 两模型成功记录见 [试跑报告](PACKY_C1_FINAL_TRIAL_REPORT.md)，不能视为全量成功率。

## 验证

```bash
python -m pytest -q \
  tests/test_realtime_agents.py tests/test_realtime_contract.py \
  tests/test_realtime_runner.py tests/test_realtime_result_layout.py \
  tests/test_realtime_gui_bench_manifest.py tests/test_realtime_gui_getter.py \
  tests/test_realtime_gui_metric.py tests/test_realtime_bench_scoring.py \
  tests/test_fmp4_live.py tests/test_recording_log_download.py
```

本地 live fMP4 测试需要 FFmpeg 或 imageio-ffmpeg。真实 VM 验证使用 `scripts/python/verify_realtime_runtime.py`，源码哈希在录制前检查；镜像构建后再启动测 WAIT。两份 VM 源文件未变时无需因宿主代码修改重打镜像。
