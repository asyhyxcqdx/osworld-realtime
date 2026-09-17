# Realtime GUI Agent 实验设计

环境与文件来源见 [项目总览](REALTIME_GUI_PROJECT.md)，运行模板见 [部署与运行](SETUP_GUIDELINE_CN.md)，配置字段见 [配置协议](REALTIME_AGENT_CONFIG_PROTOCOL.md)。

## 受控变量

同一模型的四组共享原生 1920×1080 截图、完整上下文、`computer_13` 工具、模型参数和 VM 执行器。**唯一区别是历史帧是否开放、一次决策可提交 1 个还是 1–100 个动作**：

| 变体 | 历史帧 | 一次决策的动作数 | action.mode |
| --- | --- | --- | --- |
| agent1 vanilla | 无 | 1 | atomic |
| agent2 anticipatory | 无 | 1–100 | sequence |
| agent3 video | 有 | 1 | atomic |
| agent4 combine | 有 | 1–100 | sequence |

8 款实验模型各有四组配置，另保留 Fable5、Astra 历史基线；名单、协议与密钥变量见 [模型配置表](configs/realtime_agents/README.md)。**模型网关（Packy 或自建网关）不影响实验设计，只影响 `.env` 配置。**

## 每回合时序

当前截图 → 模型请求 → 可重复的 `get_frames` 查询 → 动作工具回复 → VM 连续执行 → 一个新截图（紧跟执行结束取得，不额外等待界面重绘）。历史查询不增加动作决策数；Agent3 每次回复只能含一个工具调用，拿到取帧结果后仍可继续查询；Agent4 可在同一回复提交多个查询，但不能与动作混在同一响应。逐步骤流程与日志字段见 [Agent loop](REALTIME_AGENT_LOOP_GUIDE.md)。

## 工具与时序

共享动作：MOVE_TO、CLICK、MOUSE_DOWN、MOUSE_UP、RIGHT_CLICK、DOUBLE_CLICK、DRAG_TO、SCROLL、TYPING、PRESS、KEY_DOWN、KEY_UP、HOTKEY，另加 WAIT、DONE，共 15 个。名称沿用 `computer_13`。

- MOVE_TO / DRAG_TO：执行端 `x/y` 为原生像素，`duration_s` 必填、0–10 秒。Gemini、MiniMax 的模型工具使用各自配置的相对坐标，提交 VM 前固定转换；坐标协议与上游对照见 [模型配置表](configs/realtime_agents/README.md#协议和坐标适配)。
- WAIT：`duration_s` 必填、0–60 秒，在 VM 内执行；PRESS / KEY_DOWN / KEY_UP 的 `key` 用注册枚举（空格写 `space`，字面空格不合法）。
- KEY_DOWN 跨回合保持，直到 KEY_UP 或录制结束；模型思考期间游戏不会暂停。
- DONE 是唯一终止动作、必须最后提交，只指示 runner 读分，本身不代表通关。

VM 的 `PyAutoGUI.PAUSE=0`。整段序列只发一次 HTTP 请求，动作之间不取当前截图、不隐藏等待；每个动作返回 `started_s`、`finished_s`、`duration_s`。执行失败的序列**不自动重放**，避免重复已执行的前缀。刷新/导航快捷键在宿主控制器发送前校验，完整按键表见 [快捷键策略](REALTIME_GUI_BENCH_SHORTCUT_POLICY.md)。

## 录像与时间轴

VM 目标 30 FPS、1920×1080 fMP4，目标片段 100 ms；时间原点为首次 X11 录制帧，截图带 `task_time_s`，动作时长另用单调时钟测量。30 FPS 是目标，负载下不保证每 33.3 ms 都有一帧。

`get_frames` 接受 1–8 个秒数（可小数）。对已完成片段返回最接近时间的真实记录帧，相等时取早帧、不插帧；返回 `requested_time_s`、`actual_time_s`、`status`、`available_until_s`，`not_ready` 没有图片。框架不会自动猜时间点、自动补查或把整段录像塞给模型。

## 预算与协议

正式 YAML 使用完整历史；`max_steps` 默认 100，即每个“模型 × Agent × 游戏”有 100 个动作决策回合，游戏内三次机会共用，`get_frames` 不限次数；单次动作序列最多 100 个原子动作。输出上限、thinking 档位等按模型配置，见 [模型配置表](configs/realtime_agents/README.md)。供应商返回的 `latency_s` 包含模型计算、网络与排队，**不等于纯思考时间**。

三种协议（Messages / Responses / Chat）都收齐完整流式响应后才执行，不执行半段调用；协议细节与参数见 [配置协议](REALTIME_AGENT_CONFIG_PROTOCOL.md)。非法参数、文字代替工具、混合查询与动作都会回传错误，最多纠正两次，混合响应中任何调用都不执行。图片按原字节发送，日志用哈希代替 base64，原截图单独保存。

## 结果

目录为 `<result_dir>/<model>/<run_id>/<agent4>/computer_13/screenshot/realtime_gui_bench/<UUID>/`，含 `system_prompt.txt`、`experiment.json`、`trajectory.jsonl`、当前截图、查询帧、`recording.mp4`、索引、录屏日志与指标。评分为游戏写入的 `pass_at_1`/`pass_at_3`，标量 `result.txt` = `pass_at_3`；口径见 [项目总览](REALTIME_GUI_PROJECT.md) 与 [AGENTS.md](AGENTS.md)。成绩与金额由 `scripts/python/export_realtime_results.py` 导出后写入飞书总表。

## 验证

```bash
python -m pytest -q tests/test_realtime_*.py tests/test_fmp4_live.py tests/test_recording_log_download.py
```

live fMP4 用例需要 FFmpeg 或 `imageio-ffmpeg`，查看器用例需要 Playwright Chromium（未装则跳过）。真实 VM 链路自检用 `scripts/python/verify_realtime_runtime.py`（模拟回复，不调付费 API）；改过 VM 服务时需要重新安装或重打镜像，两份 VM 源文件没变则不用。
