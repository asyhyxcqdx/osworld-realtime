# 实时 GUI Agent：项目与实验设计

环境是 RealtimeGame v1.1(3) 的 69 道网页游戏与最终 realtime VM 镜像。游戏接口字段、状态机与验收标准见 [BENCH 协议](REALTIME_GUI_BENCH_PROTOCOL.md)，模型与坐标协议见 [模型配置表](configs/realtime_agents/README.md)，YAML 字段与运行流程见 [Agent 配置与运行](REALTIME_AGENT_GUIDE.md)。

截图与 VM 执行坐标统一为原生 1920×1080，图片不预缩放；Gemini、MiniMax 的模型层按各自配置输出相对坐标、由代码固定转换，其余模型保持原生坐标。

## 环境与任务

| 类别 | 数量 | 研究内容 |
| --- | ---: | --- |
| A | 22 | 连续感知 |
| B | 12 | 预见性动作 |
| C | 17 | 可复现动态下的感知与动作 |
| D | 18 | 随机动态下的感知与动作 |

运行网页目录只保留 69 个 `index.html`（`evaluation_examples/websites/realtime_gui_bench/games/<小写编号>/`）；69 个任务配置在 `evaluation_examples/examples/realtime_gui_bench/<UUID>.json`，清单为 `evaluation_examples/test_realtime_gui_bench.json`。游戏源是用户交付包 `RealtimeGame_v1.1(3).zip`（SHA-256 `7a4656df0e2b6b93b0bd0bb90f1830e4316d0d0d7eeef10833e244423399507a`），网页与其内 HTML 逐字节一致。

VM 内运行 `desktop_env/server/realtime.py` 和 `fmp4.py`，宿主机运行 Agent、API 适配器和 runner。镜像内两份服务文件的 SHA-256（`realtime.py` 是 `8ffc76917c4484f078f02218032ee09cc3c54650da4e0f272427d8afd7aeeab7`，`fmp4.py` 是 `923f5ff1467620c1828c45cc4d2b6c8bbca628942286ab3660b6a6a6b4d5e4cf`）与仓库同文件一致，录制前程序会再比对一次，防止旧镜像继续运行。

## 接入与验收边界

每道任务：上传单个 HTML → 本地 HTTP 8765 → Chrome/CDP 打开对应 URL → 激活窗口 → 等 3 秒；不提前点击 Start。评分唯一来源是游戏写入的 BENCH 双指标，`result.txt` = `pass_at_3`，原始 BENCH 保存在 `result.json.raw_bench`。

- 交付方自测 69/69（ready / pass1 / pass3 / fail3 四条核心路径），属交付方结果，不是我方复跑。
- 我方独立浏览器验收：69 个页面各独立加载一次（原生 1920×1080），加载 3 秒读取 BENCH、再等 3 秒复查，全部通过；138 个 BENCH 快照经 `scripts/python/check_realtime_gui_contract.py` 批量检查无契约错误。**这不等于完整 VM 交互，也不等于模型成绩。**
- 交付包 test.mjs 曾有启动失败/中断的重复尝试，未计入上述验收。
- 已知取舍：C29 开局目标位置固定；D35 的验收策略曾有偶发失败，交付方调整测试策略而非游戏逻辑。原始报告保留在交付 ZIP 内。

**游戏 HTML、任务配置、VM 服务或评分逻辑一改，已有成绩就不可比**：必须重新验收，改 VM 服务还要 `--install_realtime_server` 或重打镜像（见 [部署与运行](SETUP_GUIDELINE_CN.md)）。

## 四组 Agent（受控变量）

四组共享原生 1920×1080 截图、完整上下文、`computer_13` 工具、模型参数和 VM 执行器。**唯一区别是历史帧是否开放、一次决策可提交 1 个还是 1–100 个动作**：

| 变体 | 配置 ID | 历史帧 | 一次决策的动作数 | action.mode |
| --- | --- | --- | --- | --- |
| agent1 | vanilla | 无 | 1 | atomic |
| agent2 | anticipatory | 无 | 1–100 | sequence |
| agent3 | video | 有 | 1 | atomic |
| agent4 | combine | 有 | 1–100 | sequence |

10 款模型各有四组配置，共 40 份 YAML；名单、协议与密钥变量见 [模型配置表](configs/realtime_agents/README.md)。指定配置缺失或 `--model` 与配置不一致时直接报错，不回退其他模型。**模型网关（Packy 或自建网关）不影响实验设计，只影响 `.env` 配置。**

两段 prompt 都在 YAML 里：`system_prompt` = 反作弊与评测诚信规则（代码再追加 `api.coordinate_system` 对应的坐标约定），`user_prompt` = 该变体的任务说明（角色、实时约束、执行策略、工具与感知、规则与收尾），后者就是对话开头的唯一一条 user 消息；四组共用一套规则，`video`/`combine` 的规则与任务说明里才有 `get_frames` 相关条目。任务配置里的 `instruction` 是环境核心（`desktop_env.py::_set_task_info`）要求的字段，运行时会被覆盖成当前变体的 `user_prompt`。

单动作也是长度为 1 的序列；多动作在整段完成后才返回当前截图。历史帧每次查询 1–8 个时间点，查询次数默认不限，必须先拿到结果、再在独立响应里提交动作（Agent3 每次回复只能有一个工具调用，Agent4 可在同一回复提交多个查询）。

## 每回合时序

当前截图 → 模型请求 → 可重复的 `get_frames` 查询 → 动作工具回复 → VM 连续执行 → 一个新截图（紧跟执行结束取得，不额外等待界面重绘，因此可能仍显示动作前或动作中的画面）。历史查询不增加动作决策数。逐步骤流程与日志字段见 [Agent 配置与运行](REALTIME_AGENT_GUIDE.md)。

## 工具与时序

共享动作：MOVE_TO、CLICK、MOUSE_DOWN、MOUSE_UP、RIGHT_CLICK、DOUBLE_CLICK、DRAG_TO、SCROLL、TYPING、PRESS、KEY_DOWN、KEY_UP、HOTKEY，另加 WAIT、DONE，共 15 个，名称沿用 `computer_13`。

- MOVE_TO / DRAG_TO：执行端 `x/y` 为原生像素，`duration_s` 必填、0–10 秒；相对坐标模型在提交 VM 前固定转换。
- WAIT：`duration_s` 必填、0–60 秒，在 VM 内执行；PRESS / KEY_DOWN / KEY_UP 的 `key` 用注册枚举（空格写 `space`）。
- KEY_DOWN 跨回合保持，直到 KEY_UP 或录制结束；模型思考期间游戏不会暂停。
- DONE 是唯一终止动作、必须最后提交，只指示 runner 读分，本身不代表通关。

VM 的 `PyAutoGUI.PAUSE=0`；整段序列只发一次 HTTP 请求，动作之间不取当前截图、不隐藏等待。VM 为每个动作回一条执行回执（`started_s`、`finished_s`、`duration_s`），**这份回执只进轨迹日志**（`action_executed` 事件）；发给模型的动作回执**不带任何时间**，只报 `action_type`、`executed`、`reward`、`done`、`last_in_decision`。原因是 `started_s` 标的是宿主注入事件的时刻，而不是世界收到它的时刻，两者之间隔着不可测的 δ——把它和帧时间戳相减会把 δ 算进去。模型侧的时间锚只有两个：每轮截图的 `task_time_s` 和 `get_frames` 每帧的 `actual_time_s`（同一时间原点）。执行失败的序列**不自动重放**，避免重复已执行的前缀。刷新/导航快捷键在宿主控制器发送前校验，禁用清单见 [BENCH 协议](REALTIME_GUI_BENCH_PROTOCOL.md#9-禁用快捷键)。

## 录像与时间轴

VM 目标 30 FPS、1920×1080 fMP4，目标片段 100 ms；时间原点为首次 X11 录制帧，截图带 `task_time_s`，动作时长另用单调时钟测量。30 FPS 是目标，负载下不保证每 33.3 ms 都有一帧。

`get_frames` 接受 1–8 个秒数（可小数），对已完成片段返回最接近时间的真实记录帧，相等时取早帧、不插帧；返回 `actual_time_s`、`status`、`available_until_s`，`not_ready` 没有图片。请求的那几个秒数不回传：帧落在录屏自己的时间栅格上（30 FPS，相邻帧约 33.3 ms），`actual_time_s` 才是唯一可做差的时间戳。

## 预算

`max_steps` 默认 100：每个“模型 × Agent × 游戏”有 100 个动作决策回合，游戏内三次机会共用，`get_frames` 不限次数；单次动作序列最多 100 个原子动作。输出上限、thinking 档位等按模型配置。`latency_s` 包含模型计算、网络与排队，**不等于纯思考时间**。

## 评分与验证边界

评分只读取游戏的 `window.BENCH.pass_at_1` 和 `pass_at_3`，`result.txt` 为 `pass_at_3`，三次机会属于同一次页面运行。正常评估结束时，提前 DONE 或达到回合上限仍未成功记**有效 0 分**，保留游戏真实的 ready/running 状态；API、执行或评分异常**未取得有效成绩时不伪造分数**。结束原因由 `termination_reason` 单独记录。

**落分前还有一道 LLM 判官**：每条任务结束、写 `result.txt` 之前，`lib_run_realtime.py` 会用判官模型（`REALTIME_JUDGE_MODEL`，默认 `deepseek-flash`；system prompt 见 `configs/realtime_agents/auditor.txt`）读一遍这条轨迹的原始 JSONL，判断它是否靠 GUI 之外的手段（开发者工具/CDP/页内脚本、读写源码或存档、直连网络、终端与外部程序、刷新导航、伪造结果等）拿分。四种结论里 **`CHEAT` 把最终分写成 0**，`CHEAT_ATTEMPT`（违规动作被环境拦下）、`UNCERTAIN`、`NOT_CHEAT` **保留游戏原分**；`pass_at_1`/`pass_at_3` 始终是游戏原值，判官结论另存 `result.json` 的 `judge` 块（含它自己的 token 用量，导出时算成"判官成本"一列）。判官调用失败**不写 `result.txt`**（那条任务算未完成、续跑会重跑并重判），所以成绩只写一次且一定是判过之后的；判官 key（`REALTIME_JUDGE_API_KEY`）缺失会在开跑前直接报错。这样设计的原因是：环境（VM）里不可能物理禁掉所有绕过路径（例如终端），把"是否越权"交给判官判，比给快捷键打补丁更稳。

项目**不做 A/B/C/D 分类或整体统计表**：成绩与出表只读单任务 `result.json` / `result.txt`（见 [执行同学作业单](HANDOFF_CN.md)）。运行器会顺带写一个扁平的 `<result_dir>/<model>/<run_id>/<agent>/summary/results.json`（逐任务追加 `task_id` / `score` / `status`），它**没有任何下游依赖**，且其中的 `status: "success"` 只表示"评估正常跑完"，分数仍可能是 0，判分时不要用它。

**不能把一次 C1 成功概括为全部任务可解**，接口短测也不等于游戏成绩。批量实验的执行方式见 [执行同学作业单](HANDOFF_CN.md)。

密钥从进程环境或仓库根目录的 `.env` 读取（见 [部署与运行](SETUP_GUIDELINE_CN.md)）；游戏不需要联网。仓库不保存密钥、VM 镜像、录像或结果目录，淘汰的配置与讨论稿从 Git 历史追溯。维护者的自检入口见 [AGENTS.md](AGENTS.md) 第 4 节。
