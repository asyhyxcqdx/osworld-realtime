# Realtime 模型配置

10 款模型（`configs/realtime_agents/` 下的 10 个模型名），每款都有 vanilla / anticipatory / video / combine 四份 YAML，目录合计 40 份，**system prompt 四组各一套**（同一组内 10 份完全相同）。

> 目录里的 `auditor.txt` 不是 Agent 配置，而是**轨迹判官的 system prompt**（每次任务落分前的作弊审查，模型由 `REALTIME_JUDGE_MODEL` 指定，key 用 `REALTIME_JUDGE_API_KEY`；详见 [`AGENTS.md`](../../AGENTS.md) 第 4 节）。

| model（区分大小写） | 协议 | 上下文声明 | 输出上限 | 思考 | 密钥环境变量 |
|---|---|---:|---:|---|---|
| `claude-sonnet-5` | anthropic_messages | 1000000 | 128000 | high | `PACKY_CLAUDE_SONNET_5_API_KEY` |
| `gpt-5.6-sol` | openai_responses | 1000000 | 128000 | high | `PACKY_GPT_5_6_SOL_API_KEY` |
| `gemini-3.8-flash` | openai_chat | 1000000 | **65536** | high | `PACKY_GEMINI_3_8_FLASH_API_KEY` |
| `qwen3.8-max-0902` | openai_responses | 1000000 | 128000 | **xhigh** | `DASHSCOPE_QWEN3_8_MAX_0902_API_KEY` |
| `kimi-k3` | openai_responses | 1000000 | 128000 | **max** | `DASHSCOPE_KIMI_K3_API_KEY` |
| `deepseek-flash` | anthropic_messages | 1000000 | 128000 | high | `PACKY_DEEPSEEK_FLASH_API_KEY` |
| `glm-5.3-flash` | anthropic_messages | 1000000 | 128000 | high 兼容请求 | `PACKY_GLM_5_3_FLASH_API_KEY` |
| `MiniMax-M3` | anthropic_messages | 1000000 | 128000 | **adaptive，无 effort 档位** | `PACKY_MINIMAX_M3_API_KEY` |
| `claude-fable-5` | anthropic_messages | 1000000 | 128000 | high | `PACKY_CLAUDE_FABLE_5_API_KEY` |
| `gpt-6-astra` | openai_responses | 1000000 | 128000 | high | `PACKY_GPT_6_ASTRA_API_KEY` |

2026-09-23 `qwen3.8-max-0902` / `kimi-k3` 从 Packy 的 Anthropic 入口切到 **DashScope（阿里云百炼）兼容模式**（四份 YAML 各自同步：协议 `anthropic_messages` → `openai_responses`，`key_env` → `DASHSCOPE_*`，`thinking.effort` 写成各自最高档 `xhigh` / `max`）。依据是在本机按项目自己的请求形状复测出的两条：**`kimi-k3` 在 Packy 的 `anthropic_messages` 上直接被拒**（`HTTP 400：模型 kimi-k3 不支持 messages 协议`）；`qwen3.8-max-0902` 那条路虽然带图能跑（`input_tokens=2073`，其中图片 2042，模型能正确描述画面），但两者在 DashScope `/compatible-mode/v1/responses` 上图片、原生 tools、SSE、usage 全通（1920×1080 图 `image_tokens` 2042 / 2696，去掉图后 qwen `input_tokens` 从 7211 降到 5171；`function_call` 带 `call_id`/`name`/`arguments`，`unpack()` 直接可用；第二轮命中 `cached_tokens`），`reasoning.effort` 在 Responses 上真生效（qwen `xhigh`→2469 思考 token、`low`→303；kimi `max`→104、`low`→15），`max_output_tokens: 128000` 也被接受。两个模型**必须单独成批**、命令行显式给 `--api_base_url https://dashscope.aliyuncs.com/compatible-mode/v1`（批量脚本一次只认一个网关；这个地址以 `/v1` 结尾，脚本在其后自己拼 `/responses`）。网关不是 packyapi.ai 时会自动跳过 Packy 账单接口并打印 `GATEWAY_BILLING_SKIPPED`，成本改用 `--prices`；判官不受影响，仍走 `REALTIME_JUDGE_BASE_URL` / `ANTHROPIC_BASE_URL` 的 Packy 入口。**注意**：兼容模式把思考正文放在 `reasoning` 条目的 `content: [{"type": "reasoning_text", ...}]` 里、`summary` 为空数组（OpenAI 正版相反），`response_reasoning()` 现在两种形状都读，读不到才记 `not_returned`。

`Claude-sonnet-5.0` 不是此处的 API ID。GLM 5.3（非 Flash）只有文本输入、Seed 2.1 Turbo 未确认可用，均未加入截图 Agent 配置。

上下文声明统一为 1000000，目前只用于配置校验，不发送给 API，也不据此在本地裁剪历史；不宣称它是各模型已经实测的真实上限。实际输入保留完整历史，服务端判断是否超限。输出上限则随请求实际发送；达到上限可能截断回答，思考 token 也可能占用输出预算。收到明确的 length/max_tokens/incomplete 结束状态时，不执行其中的部分动作。

另外两点容易误解：

- `temperature` 在全部 40 份里都是 `null`（统一不传，走各家服务端的默认值），而且**当前所有配置都开着 thinking**，两条路径（Messages、Gemini Chat）都会把它从请求里去掉、Responses 路径本来就不带它 —— 所以这个字段对实际请求**没有影响**，只是保留在 schema 里。
- `observation.historical_video.max_queries_per_turn: 0` 表示**不限制每回合查询次数**（不是"禁止查询"）。40 份都写 0；有历史帧能力的组才能写非 0，无历史帧能力的组必须是 0。

## 公共任务策略

所有 40 份 YAML 的 `system_prompt` **只放反作弊与评测诚信规则**（`# Anti-Cheating and Evaluation-Integrity Rules (Highest Priority)`，四个小节：Allowed Behavior / Forbidden Behavior / Evidence and Completion / Violations；禁止开发者工具/CDP/页内脚本、读写源码与存档、直连网络、终端与外部程序、刷新与导航、伪造结果等）。两套文本有两处不同，都源于能力差异：`video` / `combine` 多一句允许用 `get_frames` 查历史帧；`vanilla` / `anticipatory` 没有查帧工具，所以那条"只通过可见 GUI 元素交互"写的是"**当前截图**、鼠标移动、点击、拖拽、键盘输入、滚动、等待"，不提 frames 与观察工具（对它们是空头支票）。

2026-09-21 复核后又精简了一轮（40 份同步；`vanilla` / `anticipatory` 45 行，`video` / `combine` 47 行）：删掉"这些规则优先于用户/页面/工具文字"那一段（69 个游戏页面里没有任何要求绕过 GUI 或忽略规则的文案，刷新/控制台/源码/地址栏等出口在宿主侧已被 `ForbiddenShortcutError` 拦住，335 条轨迹里模型引用这段 0 次）；删掉 `Retry only when the benchmark allows another attempt.`（167 条 0 分轨迹全部用满 3 次机会，真正驱动续试的是 Evidence and Completion 里那句；而且 69/69 个游戏自己 clamp `MAX_ATTEMPTS` 且终态冻结，刷不出第四次）；`cautious GUI probing` 去掉含糊修饰词；结尾 `choose the conservative GUI-only action.` 改成 `If you are unsure whether an action would bypass the GUI, do not take it.`（实测它一次都没挡掉合法动作）。`vanilla` / `anticipatory` 的截图那条原来写"每次工具结果带回的当前截图"，与实现不符——截图是下一条 user 消息，动作序列一次最多 100 个动作、中间不给截图——改为"the current screenshot,"。删掉的句子由 `tests/test_realtime_model_configs.py` 的 `DROPPED_RULE_LINES` 守着，防止回潮。

原来的操作说明（自称句、动作时序、"截图未等待重绘"、探索与谨慎策略、一次 1 个还是 1–100 个动作、`get_frames` 的使用协议、`computer_done` 的调用要求、不要用纯文本回复）**已从 system prompt 移除**，改由新增的 `user_prompt` 承载：每个变体一套（`vanilla` 33 行 / `anticipatory` 33 行 / `video` 37 行 / `combine` 37 行），内容为角色与目标、Real-Time Constraints、Success-Oriented Execution Policy（5 条）、Tools and Perception、Rules and Completion；`video` / `combine` 才提 `get_frames`，`anticipatory` / `combine` 才写"已知整串动作和时序时放在同一条回复里提交"。它作为对话开头的唯一一条任务 user 消息。任务配置里的 `instruction` 字段**保留**——环境核心 `desktop_env.py` 的 `_set_task_info` 要求它存在——69 份都放**当前 combine 变体的 user prompt**（占位用，与 YAML 里的 combine 文本逐字一致，只去掉结尾换行）。实际运行时 `lib_run_realtime.py` 在使用前把它覆盖成**当前变体**的 user prompt，所以环境核心、`env.instruction`、日志和模型看到的是同一段文字；`tests/test_realtime_gui_bench_manifest.py::test_task_configs_share_the_combine_user_prompt_and_evaluator` 守着"69 份相同、且等于 combine 的 user_prompt"这条不变式。**注意**：这 69 个文件属于 HF 数据集 `bright-star123/osworld-realtime-games`，改它们会让已发布仓库的 `manifest.json` 逐文件 sha256 失效，需要重新生成清单并重传。

2026-09-21 又精简了 `user_prompt`（40 份同步，行数见上）。理由与删掉的内容：执行准则 14 条压到 **5 条**——删掉"不要急于行动""跟踪进度指示""分阶段完成""优先可逆低成本动作""管理尝试次数""最后一次尝试要保守"；其中最后一条与本基准实测相反（C 类通关的 **71% 发生在第 3 次机会**），"要保守"和与之相对的"要奔着通关去"两种色彩都不保留，只留事实。"分清哪些动作不可逆"因过于抽象、而这批游戏的关键动作多为一次性（挥刀格挡、每次冒头只有一枪）而删除。实时约束 4 段压到 **2 段**：删"关键动作后重新评估"，"计时器/冷却/动画/对手"改成不列举的通用说法，并去掉"等之后的截图"这类会与实时性冲突的指令，改为只说明"这张截图没有变化不等于动作没有生效"。规则与收尾 6 段压到 **2 段**：另外 4 段（失败后继续下一次、不刷新/重载/重开/离开、只用注册工具、成功或没机会时调用 `computer_done`）在 `system_prompt` 里已有逐字相同的内容。工具段删掉"动作之间没有隐藏延迟"单句（并入时序那句）与"只使用注册的工具"单句（并入 schema 那句）。删掉的句子由 `tests/test_realtime_model_configs.py` 的 `DROPPED_USER_PROMPT_LINES` 守着，防止回潮。

2026-09-22 把三条**命令句**加回（同样 17 个 C 类任务、`deepseek-flash`、`agent4`、`num_envs 6` 的对照：`ds_c_clean_20260921` = 10/17 → `ds_c_trim_20260922` = **6/17**）。掉分的 6 个（C3、C26、C28、C32、C33、C36）全是靠精确时序定标的类型，行为上也对得上：trim 里推理字数中位 +27%、决策数 +50%（想得更多），但**同一串动作重复的最大次数中位 1 → 2**，关键任务的 `WAIT` 直接归零（C26 13→0、C35 3→0、C36 1→0）。历史 C 类 85 条里"从不重样 53% 通关、重样 2 次 5%"。原因判断：上一轮把三句看着像说明、实际是**责任划分**的话当冗余删了——(1) `no hidden delay is inserted between actions`（动作间隔归你管，要自己插 WAIT）、(2) `check whether the UI just had not updated before repeating it`（重发之前先确认）、(3) `account for elapsed real time before acting`（动手前先算流逝时间）。现在用三句更短的命令补回：实时约束第 1 段末句 `Before doing the same action a second time, confirm that it really had no effect.`、第 2 段 `…take the time that has passed into account before you act.`、工具与感知新增一句 `Use computer_wait when you need a gap, or need the world to advance a fixed amount of time.`（四份都有）。行数因此变为 `vanilla` / `anticipatory` **35 行**、`video` / `combine` **39 行**；这三句由 `BEHAVIOUR_COMMAND_LINES` 守着，不许再删。

2026-09-22 追加两道防线，起因是**模型在三次机会用完后用键盘去找"隐藏的重开按钮"**：C30 的最后一个决策是 `PRESS(tab)|PRESS(enter)` 连按三组，Tab 把焦点送出页面、Enter 落在地址栏等于重新导航 → 环境侧页面身份检查失败（`Realtime page was reloaded, navigated, or replaced`）→ `run_error` 且**不写 `result.txt`**，任务作废并被续跑。同样的形态在 C39、C28、C26 都出现过，其中 trim 批的 C39 靠续跑从 0 变 1（等于白拿一次重掷），contract 批的 C30 直接作废。两道防线：(1) **宿主侧禁用焦点键**——`tab` / `enter` / `return` / `esc` / `escape` 加进 `realtime_protocol.py` 的 `_BLOCKED_SINGLE_KEYS`，模型收到的是说明真实原因的纠正（`_blocked_key_message`），而不是"刷新被禁"；依据是实测**三种编码写法（`e.key` / `e.code` / `keyCode`）下 69 个游戏都没有把这些键当游戏键**，全部游戏里唯一的按键字面量是 `"Space"`（81 次），所以禁用不影响玩法。(2) **system prompt 的 Evidence 段把终态事实写清**——`When the game shows success or no attempts remain, the page freezes: there is no hidden restart and nothing left to discover.` 这句是掐动机：user prompt 有一条"很多机制没写在规则里、只能靠观察发现"，模型在无路可走时把这条推到极端去找**隐藏的重开入口**，而两版 prompt 原来都没有一句说"终态之后没有可发现的东西"（协议 §3 原文是"进入 passed/failed 就冻结、无第四次入口"）。system prompt 行数因此变为 `vanilla` / `anticipatory` **46 行**、`video` / `combine` **48 行**。

2026-09-22 把执行准则第 5 条的后半句从"回看之前几次尝试搞清楚它们怎么失败的"换成**可操作的测量指令**（40 份 YAML + 69 个任务 JSON 同步，行数不变）：`5. After a failed attempt, work out what happened: what you did, how long after the start you did it, and how the game responded. Let that decide the next attempt's timing instead of resending the same actions unchanged.` 依据是 4 个批次、63 个 C 类任务实例的逐决策复盘：旧措辞模型是**满足的**——49/49 个任务都叙述了之前几次尝试——但它只叙述、不测量。三个实测症状：(1) **尝试常常是被"看"掉的，不是被"试"掉的**——首次非 `DONE` 动作是单个动作的有 **62/63**（其中 58 个是孤零零一个"点开始"），是动作序列的只有 1 个、含 `WAIT` 的 **0** 个；而纯观察的模型回合 **357** 次 vs 提交动作的 **253** 次，一轮就是一次完整 API 往返（游戏里 10~40 秒），C34 的模型自己写下"点 Start → 第一次尝试开始 → **游戏在我读屏的时候一直在跑** → 第一次尝试失败"。(2) **每次重新猜常数**：`c36` 真实窗口是起跳后 0.429 s ±100 ms，两批分别猜 `WAIT(0.35)` 和 `WAIT(0.20)`；`c3` 猜过 1.0/1.12/1.5；`c26` 猜过 0.75/0.8/1.4。(3) 第三次尝试确实变成了多动作序列（**35/49**），但形态是"喷"——C34 13 个动作、C26 27 个、C30 41 个；而 `c34` 写明 `if (pressedAt === null) pressedAt = t; // 只记录第一次按 R`、`c36` 的 `pressRelease` 第一句就是"按过就返回"，喷出去的只有第一个进判定。新句子由 `tests/test_realtime_model_configs.py` 的 `ATTEMPT_REVIEW_LINES` 守着。

2026-09-22 再补两条，**按变体分别加**（行数：`vanilla` **35** 不变、`anticipatory` **37**、`video` **41**、`combine` **43**）：(1) **A 段——重试对话框意味着游戏暂停，且点 Next 必须与 `computer_wait` 和按键放在同一条回复里**（`The retry dialog ("Attempt N / 3 — Next") means the game is paused, so study the attempts you have already played with get_frames before you go on. Clicking Next restarts the clock, and your next response arrives many seconds later, so never send that click on its own: it must be the first action of the response that also contains the computer_wait and the key actions. Inside one response the relative timing is exact, so a computer_wait placed after the click is the wait the game sees.`）。依据：`pass@1` 在所有 C 类批次里都是 **0**（第一次尝试永远被孤零零一个"点开始"吃掉，而点完这一轮就开始跑，两次回复之间隔着 10~40 秒游戏时间）；measure 批 6 个失败里 **3 个死在合成上**——C39 自己写 `I intended to do the click + wait + press in ONE response, but I only sent the click`、C1 把 `key_down d` 跨回复按住不松、C12 在 Next 生效前就把 32 个按键喷完（`ds_c_clean` 那版 prompt 里本来有一句把"复盘"和"Next 与整段序列同发"绑在对话框上的话，精简时被删、第 5 条只补回了"测量"那一半）。**只加给 sequence 变体**：`vanilla` / `video` 一次只能发一个动作，"点 Next 和按键同发"对它们在结构上不可能，所以这两类**必须没有**这句。复盘那半句 4 个变体都做得到（`context.history_policy=full` + `include_screenshots=True`，历史里带着之前每一轮的截图；非 `get_frames` 变体只有**回合边界那一张**，间隔 10~40 秒），所以 `anticipatory` 也保留 `study the attempts you have already played before you go on`；只有点名 `get_frames` 的写法属于 `combine`（它是唯一能按任意时刻取帧的变体，`video` 虽能取帧但每轮只允许一次调用）。**注意 `vanilla` 连 `get_frames` 工具都没有**（`self.frames` = `observation.historical_video.enabled`），C 类要的那个瞬间（起跳后 0.429 s、落地前 130 ms）根本不在它手上的任何一张截图里。守卫：`RETRY_SEQUENCE_LINES` / `RETRY_STUDY_LINES` / `RETRY_FRAME_LINES`（按变体断言，含"atomic 变体必须没有"）。(2) **分辨率句——关键瞬间要查更密的帧**（`When you need to pin down a short moment, ask get_frames for frames closer together around it instead of spreading them over the whole attempt.`，只给 `video` / `combine`）。用 `c32` 自己的碰撞公式（球 `x=60+400t`、棒长 78 以 easeOut 扫 180 ms、球心到棒线段距离 < 12）做数值仿真：命中窗 **0.875~1.045 s（170 ms）**；而模型量的时候帧间隔是 **150 ms**，误差上限（间隔的一半）≈ 窗口的一半 → 量出来的数永远在窗口边缘打转，同一题在 `ds_c_clean_20260921`/`ds_c_origctl_20260922` 过、在 `ds_c_measure`/`ds_c_measure6` 不过。过关的 C36/C37 用的是 **0.05 s** 间隔。句子**故意不写死数字**：每次 `get_frames` 最多 8 个时刻，固定 0.05 s 只覆盖 0.35 s，万一时刻不在这个窗口里就什么都看不到。守卫：`FRAME_RESOLUTION_LINES`。**顺带结论**：两类 atomic 变体（`vanilla` / `video`）的 C 类窄窗题结构上无解（点 Next→等→按键至少 3 轮，一轮就是 10~40 秒游戏时间），以后不能拿它们的 C 类分数当模型能力比。

YAML 的 `system_prompt` 仍是唯一 prompt 来源：`RealtimeAgent` 必须收到它，缺失或为空时直接报错，代码不再内置默认 prompt；**坐标说明不在 YAML 里**，由 `api.coordinate_system` 在运行时追加在整段 prompt 之后（`realtime_agent.py` 的 `self.system = system_prompt_text + "\n" + coordinates.guidance`）。四处操作说明的原文可在旧提交里取回：`git show HEAD~1:configs/realtime_agents/vanilla-deepseek-flash.yaml`。

在 `user_prompt` 的 Success-Oriented Execution Policy 里统一要求：解析任务与成功条件；随时看清页面显示的状态（剩余尝试次数、提示行、目标文字、按钮）；通过观察发现规则里没写的机制；行动前先计划；以及失败后先诊断、不要原样重发同一串动作。不再规定"最后一次尝试要保守"或"要激进"，两种色彩都不放。

这段策略只放在 `user_prompt`（`system_prompt` 只放反作弊与评测诚信规则），开头的 Task 说明保持原样。观察和试探限于各 Agent 已有工具，不增加强制等待、强制查询次数或新的执行条件；四组的单动作/动作序列与历史帧能力保持原有区别。新运行自动使用更新后的 prompt，并保存完整实际文字到 `system_prompt.txt`；已有轨迹保留当时的 prompt。

## 协议和坐标适配

所有模型原样接收 1920×1080 PNG，VM 统一执行原生像素坐标。`api.coordinate_system` 同时控制追加的坐标 prompt、模型可见工具 schema、回复校验与动作转换：

| 模型 | 配置值 | 模型输出 x/y | 交给 VM |
|---|---|---|---|
| Gemini 3.8 Flash | `normalized_0_1000` | 0–1000 整数 | `floor(x*1920/1000)`、`floor(y*1080/1000)`；合法端点 1000 落到最后一个屏幕像素 |
| MiniMax M3 | `normalized_0_1000_unclipped` | 0–1000 整数 | 按上游 `int(x*1920/1000)`、`int(y*1080/1000)` 直接换算；1000 分别得到 1920、1080，不额外截边 |
| Claude Sonnet 5、Sol、Qwen、Kimi、DeepSeek、GLM，以及 Fable/Astra | `native_pixels`（缺省值） | 原生像素 | 原样传递 |

Gemini 和 MiniMax 的四组 YAML 均显式选择对应协议；两者输出范围相同，但端点处理分别跟随各自上游实现。Qwen 按本项目原生坐标实测结果保持现状。没有图片预缩放，也不根据某次返回值大小猜测坐标单位。相对协议不接受超范围、非整数的 x/y，沿用现有整段拒绝与工具错误纠正流程，不执行有效前缀。键盘、WAIT、滚动量、动作数量与历史查询规则不变。`tool_format: native` 指 API 原生工具调用，与坐标单位是两回事。

实际 prompt 保存于 `system_prompt.txt`，协议保存于 `experiment.json` 和 `model_request.coordinate_system`。原始回复/工具参数保留在 `model_response`，转换后的提交和执行动作保留在 `action_submitted` / `action_executed`；不会把像素值写回 assistant 历史。正常执行后的模型工具反馈只包含该调用的动作类型、是否执行、`reward`/`done` 和 `last_in_decision`，不回显 VM 坐标、不带任何时间字段（`started_s`/`finished_s`/`duration_s` 只进 `action_executed`）、不附整段 `sequence_actions`，因此不再添加 `execution_coordinate_system`。完整 VM 参数及原精度时间仍保留在 `action_executed.info.sequence_actions`。HTML 在原生图片上按记录的协议绘制模型坐标，工具参数仍展示原始值。

- Messages：保留完整 thinking/tool_use/tool_result。MiniMax M3 只发送 `thinking: {type: adaptive}`，不发送 `output_config.effort` 或 Claude 的 `thinking.display`。
- Responses：`reasoning: {effort: high, summary: auto}`，保留 reasoning 内容，收齐完成响应才解析动作。
- Gemini Chat：通过 `extra_body.google.thinking_config` 发送 `thinking_level: high`、`include_thoughts: true`，不再同时发送 `reasoning_effort`。正式配置校验和 ModelWire 均支持该路径。保留完整 assistant 消息、reasoning_content 和供应商返回的扩展字段。
- 三种协议统一发送 `stream: true`；Chat 另外请求 `stream_options.include_usage: true`。收齐完成标志后再执行动作，保留分片工具参数、思考签名和累计 usage。读取超时仍为 120 秒，最多尝试三次；不改思考档位、输出上限或工具动作顺序。`model_response.stream_received` 标明实际响应是否为 SSE。
- 若 Chat 中转流对不同工具重复使用同一 index，按不同调用 ID 分开保存；索引冲突后缺少 ID 的分片无法确定归属时明确报错，不猜测或把两个动作拼成一个。
- 四组的截图、历史、工具、动作预算规则相同于既有基线。Agent3 每次回复只能提交一个 get_frames 或一个动作；Agent4 仍允许多个查询或动作序列，但不能混合查询与动作。

兼容接口接受某个参数，并不证明所有供应商内部具有完全相同的推理预算。Qwen/GLM 通过 Packy Messages 的 high 兼容请求沿用已验证路径，其底层映射由中转站实现。

## 密钥与启动

真实密钥不写进 YAML、源码、示例或报告。`api.key_env` 只保存环境变量名；Agent 层只读这一个变量，缺失即报错。**批量入口会多兜一层**：`run_realtime_batch.py` 按 `api.key_env` → `REALTIME_API_KEY` → `PACKY_API_KEY` 取第一把非空 key，再把它注入到该模型自己的变量名里，所以临时共用一把 key 时也能跑。

**推荐做法**：在仓库根目录 `cp .env.example .env`（`.env` 已被 git 忽略），把上面表格里的变量名和 key 填进去。批量脚本和 `run_multienv.py` 会自动加载它，之后不需要再传任何 key 参数；所有模型共用一把 key 时只填 `REALTIME_API_KEY` 即可兜底。换非 Packy 网关时在同一个文件里设 `REALTIME_API_BASE_URL`（或按协议设 `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL`）。

也可以临时在终端里按需输入对应组密钥（输入不回显）：

```bash
# 变量名逐个对应模型；只想跑哪几个就 export 哪几个。
read -r -s -p 'claude-sonnet-5 key: ' PACKY_CLAUDE_SONNET_5_API_KEY; export PACKY_CLAUDE_SONNET_5_API_KEY
read -r -s -p 'gpt-5.6-sol key: ' PACKY_GPT_5_6_SOL_API_KEY; export PACKY_GPT_5_6_SOL_API_KEY
read -r -s -p 'gemini-3.8-flash key: ' PACKY_GEMINI_3_8_FLASH_API_KEY; export PACKY_GEMINI_3_8_FLASH_API_KEY
read -r -s -p 'qwen3.8-max-0902 key: ' DASHSCOPE_QWEN3_8_MAX_0902_API_KEY; export DASHSCOPE_QWEN3_8_MAX_0902_API_KEY
read -r -s -p 'kimi-k3 key: ' DASHSCOPE_KIMI_K3_API_KEY; export DASHSCOPE_KIMI_K3_API_KEY
read -r -s -p 'deepseek-flash key: ' PACKY_DEEPSEEK_FLASH_API_KEY; export PACKY_DEEPSEEK_FLASH_API_KEY
read -r -s -p 'glm-5.3-flash key: ' PACKY_GLM_5_3_FLASH_API_KEY; export PACKY_GLM_5_3_FLASH_API_KEY
read -r -s -p 'MiniMax-M3 key: ' PACKY_MINIMAX_M3_API_KEY; export PACKY_MINIMAX_M3_API_KEY
read -r -s -p 'claude-fable-5 key: ' PACKY_CLAUDE_FABLE_5_API_KEY; export PACKY_CLAUDE_FABLE_5_API_KEY
read -r -s -p 'gpt-6-astra key: ' PACKY_GPT_6_ASTRA_API_KEY; export PACKY_GPT_6_ASTRA_API_KEY
```

只运行其中一个模型时，只需设置它对应的变量。保留本机已配置的 HTTPS_PROXY/HTTP_PROXY 和访问 VM 所需的 NO_PROXY。

从仓库根目录启动，例如 Gemini Agent3：

```bash
python scripts/python/run_multienv.py \
  --agent_variant agent3 \
  --model gemini-3.8-flash \
  --run_id packy_v11_gemini_batch01 \
  --action_space computer_13 \
  --observation_type screenshot \
  --provider_name docker \
  --path_to_vm docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2 \
  --headless \
  --api_base_url https://www.packyapi.ai \
  --test_all_meta_path evaluation_examples/test_realtime_gui_bench.json \
  --max_steps 100 \
  --sleep_after_execution 0 \
  --environment_ready_wait_s 3 \
  --evaluation_settle_s 3 \
  --num_envs 1 \
  --result_dir results_four_agents
```

替换 `--model`、`--agent_variant` 即自动选择对应 YAML。依次运行各组，一次一个 VM。不要用 CLI 的 `--max_tokens` 改正式预算：Realtime 运行值来自 YAML。

40 份配置都显式写了 `api.key_env`，而且是**一个模型一个变量**：变量名 = `PACKY_` + 模型名（全大写，非字母数字换成 `_`）+ `_API_KEY`。10 款模型对应 10 个变量，没有两款模型默认共用同一把 key；某个模型自己的变量没值时，才回退 `REALTIME_API_KEY` → `PACKY_API_KEY`。

## 验证范围

配置回归测试检查实际请求字段、四组能力隔离、查帧回图、历史和 reasoning 保留、密钥选择、原图字节不变及各坐标协议的执行结果。**接口短测不等于完整游戏成绩。**

参考：[Gemini 思考参数](https://ai.google.dev/gemini-api/docs/openai)、[Gemini 3.8 Flash 上限](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)、[Kimi Messages](https://platform.kimi.ai/docs/api/messages)、[MiniMax Messages](https://platform.minimax.io/docs/api-reference/text-chat-anthropic)、[DeepSeek Anthropic 兼容](https://api-docs.deepseek.com/guides/anthropic_api)。

流式协议参考：[OpenAI 工具调用分片](https://developers.openai.com/api/docs/guides/function-calling)、[OpenAI Responses 流式事件](https://developers.openai.com/api/docs/guides/streaming-responses)、[Claude Messages 流式事件](https://platform.claude.com/docs/en/build-with-claude/streaming)。

历史接口检查（2026-09-15/16）：八款模型各用正式配置完成过一次真实的流式查帧/回图/动作往返，均实际按 SSE 返回并带 usage；这是静态图片的接口检查，没有执行 VM 动作，不作为游戏成绩。

## 官方依据（坐标协议）

[Gemini 官方 Computer Use 文档](https://ai.google.dev/gemini-api/docs/computer-use) 的动作参数表写 `x/y: int (0-999)`，示例转换为 `int(x/1000*screen_width)`，说明段落用的是 0–1000。原 OSWorld 工具描述也是 0–999。本项目对照 OSWorld-V2 后采用与其 prompt、工具描述、解析器一致的 **0–1000**，分母固定 1000；[MiniMax 官方 M3 报告](https://www.minimax.io/blog/minimax-m3) 的 OSWorld-Verified 方法写明 “relative coordinates 0–1000, image resolution 1920×1080”。两个模型都保持原始 PNG 字节，不转 JPEG。

OSWorld-V2 源码对照（固定提交 `627a1d691fbd0fd93b6161ecafa81530d50f6138`）：

- Gemini：[工具 x/y 描述](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/gemini_agent.py#L54)、[system prompt](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/gemini_agent.py#L274)、[坐标校验与换算](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/gemini_action_parser.py#L65)：0–1000 除以 1000，端点限定到 `width-1` / `height-1`。
- MiniMax：[坐标 prompt 与工具格式](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/m3/prompts.py#L49)、[坐标换算](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/m3/parser.py#L284)、[图片编码](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/m3/agent.py#L105)：0–1000 相对整数乘原图尺寸，**不加端点截边**，`(1000,1000)` 传到执行器即 `(1920,1080)`。本项目的换算与上游 `scaled_xy` 在 x/y 两轴逐整数比对一致。

借鉴的只有坐标说明、工具坐标语义和确定性换算。上游 Gemini 用 Google SDK、M3 用 XML 文本工具和 PyAutoGUI 代码，还带历史图片裁剪和桌面任务规则；本项目继续使用 Packy 已验证的 Chat / Messages 注册工具调用、完整历史和 `computer_13` 执行器，不引入上游的 shell、网页导航、FAIL 或额外等待，也不继承上游对数字字符串、非整数、损坏动作名的宽松修复。坐标单位由配置显式声明，不按返回值猜；这部分只改宿主模型层，不需要重打镜像。
