# 四组 Agent：实现与运行约定

游戏网页的 `window.BENCH` 状态和 `pass_at_1`/`pass_at_3` 正式定义以
`REALTIME_GUI_BENCH_PROTOCOL.md` 为准；本文只记录四组 Agent 的运行实现和实验参数。

当前四组已接入同一套运行器。它们使用相同的模型接口、原有 `computer_13` 动作空间、当前截图、历史轮数和 fMP4 录制设置；区别只有是否允许动作序列、是否向模型开放 `get_frames`。本文记录当前实现，不把设想写成已验证结果。

## 四组

| 启动参数 | 历史画面工具 | 一次回复允许多个动作 | 执行方式 |
| --- | --- | --- | --- |
| `--agent_variant agent1` | 无 | 否 | 原来的 `env.step()` |
| `--agent_variant agent2` | 无 | 是 | 虚拟机内执行完整列表，只取一次最终观测 |
| `--agent_variant agent3` | `get_frames` | 否 | 查询结束后执行一次 `env.step()` |
| `--agent_variant agent4` | `get_frames` | 是 | 查询结束后执行完整动作列表 |

内部使用 `AgentMode(sequence, frames)` 两个能力开关，启动时通过四个模式选择。四组共用提示词生成器、参数校验、模型适配层和任务运行器。Agent 1/2 不向 API 注册任何工具，也不接受意外工具调用。

## 已接入的代码

| 文件 | 作用 |
| --- | --- |
| `mm_agents/realtime_protocol.py` | 四组配置、原动作空间校验、`GetFramesArgs`、工具 Schema、提示词 |
| `mm_agents/realtime_agent.py` | 模型请求 → 工具调用/图片回传 → 再次决策 → 动作输出 |
| `desktop_env/server/fmp4.py` | 增量扫描完整 fMP4 片段、按时间选帧、受限读取和解码 |
| `desktop_env/server/realtime.py` | 虚拟机持续录制、带时间的观测、取帧和动作序列接口 |
| `desktop_env/controllers/python.py` | 主机与虚拟机接口；复用现有动作执行代码生成整组命令 |
| `desktop_env/desktop_env.py` | `step_sequence()`，整组完成后只取一次观测 |
| `lib_run_realtime.py` | 四组任务生命周期、预算、轨迹、录像和日志保存 |
| `scripts/python/run_multienv.py` | 四组启动入口，原有非实验入口继续保留 |
| `scripts/python/install_realtime_server.py` | 把扩展安装到当前 VM，备份并补入原有服务入口 |

旧提示词中 `.format()` 误解释动作 JSON 花括号的初始化问题已经改为只替换 `{CLIENT_PASSWORD}`。四组使用同一个严格动作解析器，不再从多个独立代码块中偷偷执行多个动作。

## 动作空间保持不变

继续使用 `desktop_env/actions.py` 中的定义：

```text
MOVE_TO(x, y, duration_s)
CLICK(button?, x?, y?, num_clicks?)
MOUSE_DOWN(button?)
MOUSE_UP(button?)
RIGHT_CLICK(x?, y?)
DOUBLE_CLICK(x?, y?)
DRAG_TO(x, y, duration_s)
SCROLL(dx, dy)
TYPING(text)
PRESS(key)
KEY_DOWN(key)
KEY_UP(key)
HOTKEY(keys)
WAIT(duration_s)
DONE()
```

`WAIT.duration_s` 是等待动作的必需参数，取值范围为 0–60 秒。`MOVE_TO.duration_s` 和 `DRAG_TO.duration_s` 也是必需参数，取值范围为 0–10 秒。不再使用全局 action pause、随机移动时长或其他隐含等待字段。Agent 不提供 `FAIL`；单动作组返回一个对象，序列组返回一个 JSON 数组，`DONE` 只能出现在末尾。

单动作示例：

```json
{"action_type":"PRESS","parameters":{"key":" "}}
```

序列示例：

```json
[
  {"action_type":"PRESS","parameters":{"key":" "}},
  {"action_type":"WAIT","parameters":{"duration_s":1.0}},
  {"action_type":"PRESS","parameters":{"key":" "}}
]
```

这里空格字符 `" "` 是当前 `KEYBOARD_KEYS` 中的空格键表示。不要把前面的概念示例 `SPACE` 当成已存在的键名。

## env.step、序列和等待

实时模式的 `env.step(action)` 和 `env.step_sequence(actions)` 包含：

```text
控制器执行动作 → 取观测 → 返回 observation/reward/done/info
```

当前 `reward` 固定初始化为 0；`done` 默认 False，`DONE` 把它设为 True；`info` 保存结束标记。真实任务评分仍在任务结束时由 `evaluate()` 完成，并非每个动作都会获得一次游戏评分。

序列组先在主机上用现有控制器生成完整命令列表，然后只向虚拟机发送一次：

```text
动作 1 → 动作 2 → 动作 3 → 返回整组执行记录 → 取一次最终观测
```

动作之间没有截图或网络往返，也不自动插入间隔。`MOVE_TO`、`DRAG_TO` 和 `WAIT` 的持续时间由各自动作参数明确给出，并由 VM 执行、计入录屏时间轴；普通动作之间没有隐含等待。

序列执行记录每个动作的开始/结束任务时间，不再使用绝对定时动作计划，也没有模型指定的精确动作开始时刻。网络超时或部分执行错误不自动重发整组动作，避免重复点击/按键；错误中记录已执行的部分。

这里区分两个计数：一个 `PRESS`、一个 `WAIT` 或一个 `CLICK` 各算一个原子动作；一次模型从当前观测到提交动作的过程算一个决策回合。一次序列回复可以包含多个原子动作，但只计一个决策回合。`get_frames` 查询和模型思考不增加决策回合数，但会分别记入模型请求数和工具调用数。

## 时间零点与持续录像

正常任务流程：

```text
环境 reset 和页面配置
→ 60 秒准备等待
→ 启动持续 fMP4 录制
→ 第一帧实际采集时间作为任务 0 秒
→ 采集初始模型截图，附其任务时间
→ 请求模型；录制始终继续
→ 执行动作；录制始终继续
→ 任务评分与收尾
→ 停止录像并保存全部视频和索引
```

实现用第一帧实际采集时刻统一录像时间与虚拟机任务时钟。初始截图通常略晚于 0 秒，并如实标注时间，不将启动录像的通信耗时算成“同一瞬间”。截图还记录采集开始/结束区间；模型看到的是该区间中点对应的任务时间。60 秒准备不进入任务相对时间。

模型能看到截图的任务时间、取出的图片时间、查询完成时间。它不能在一次推理请求进行到一半时自动接收新画面。游戏不会因模型推理或查询而暂停。

录像输入 PTS 使用虚拟机墙钟；观测和查询的任务相对时间也使用同一来源，动作耗时另外用单调时钟测量。这样不会把两种时钟的漂移混到图片时间里。

共同录像设置：

- 目标 30 FPS，1920×1080；记录实际 PTS，不按帧号假造均匀时间。
- 单个持续写入的 **fMP4**；普通 MP4 不用于实时历史查询。
- 目标每约 100 ms 封装一个片段；采集间隔仍约 33.3 ms。片段可短于/长于目标，不保证最大读取延迟为 100 ms。
- 保留输入采集时间的微秒时间基，禁用 B 帧和恒定帧率补帧；实际采集速度以日志和索引为准。
- 每约 30 帧一个关键帧；查询从前一个可独立解码片段开始恢复目标图片。
- 四组采用相同录像设置；只有 Agent 3/4 对模型开放历史帧工具。
- 每个任务有独立的录像会话 ID；运行中不删除历史片段。

## get_frames

```python
get_frames(times_s=[3.0, 8.0])
```

表示要看任务第 3 秒和第 8 秒附近实际采集的两帧。只返回模型指定的画面，不把整个时间区间或后台录像自动塞进上下文。

查询规则：

1. 用 Pydantic 校验 1～8 个有限、非负时间值。
2. 增量扫描 fMP4，确认完整的 `moof` 和 `mdat`；正在写入的尾部不发布为可读帧。
3. 请求超出已完成帧的时间范围时返回 `not_ready`，不拿更早画面冒充目标时刻。
4. 在可读范围内选取时间最近的真实帧；距离相同时优先较早帧。
5. 只读初始化段和恢复该帧所需的已完成片段，解码成 PNG，随元数据返回模型。

成功时，每张图片前都有对应文字元数据：

```json
{"requested_time_s":3.0,"actual_time_s":3.013742,"status":"ok","available_until_s":4.201853}
```

随后是实际 PNG 的多模态图片块。JSON 元数据会序列化进协议的文字内容块；图片单独放进图片内容块，不让模型把 Base64 字符串当正文读。

尚不可读时没有图片：

```json
{"requested_time_s":3.0,"status":"not_ready","available_until_s":2.8}
```

没有任何已完成帧时 `available_until_s` 为 `null`。解码失败返回 `status=error`。片段字节偏移、封装结构和内部完成记录不交给模型。不会全局去重，也不会删除 A→B→A 中重复出现的 A。

本地永久保存的是完整 fMP4 和时间索引，不是每一张采集帧的 PNG；只有每轮观测和实际查询出的图片额外落盘，方便复查模型看到了什么。

`moof` 和 `mdat` 是 fMP4 内部的两类数据块：`moof` 保存这一小段的时间、样本数量和字节位置等目录信息，`mdat` 保存真正压缩后的视频数据。只有同一片段的 `moof` 和完整 `mdat` 都写完，程序才把它加入可查询索引。

例如 `get_frames(times_s=[3.0, 8.0])` 成功时，工具结果中会有两组内容：

```text
文字：requested=3.0, actual=3.013, status=ok
图片：第 3 秒附近的 PNG
文字：requested=8.0, actual=7.987, status=ok
图片：第 8 秒附近的 PNG
```

Anthropic 接口把这些内容放在一个 `tool_result` 的内容数组里；Chat 接口先回工具文字结果，再附上对应的图片消息。图片不是 JSON 里的可读字符串，而是协议的图片内容块。

## 工具格式和决策循环

默认 `--tool_format native`：

- `--api_format anthropic_messages`：`/v1/messages`，返回 `tool_use`，回传包含文字和图片的 `tool_result`。
- `--api_format openai_chat`：`/v1/chat/completions`，保留 `message.tool_calls`；先回齐所有工具结果文字，再发送对应调用的图片消息。
- `--api_format openai_responses`：`/v1/responses`，保留 `function_call` 和推理项，回传 `function_call_output`。
- `auto` 仅按 Claude 名称选择 Anthropic，否则选择 Chat；不根据一次错误响应擅自改变协议。Packy 的 GPT 组若使用 Responses，必须显式配置，通道权限仍由中转站决定。

实时 Agent 只接受 `--tool_format native`。旧的 content JSON tool protocol 不属于当前实验接口；`GetFramesArgs.model_json_schema()` 仅用于生成原生工具参数说明，不负责执行 Python 函数。

Agent 3/4 的循环：

```text
当前截图 → 模型查询 get_frames → 程序取帧并回传图片
→ 模型可继续查询 → 模型提交动作 → 执行 → 下一张当前截图
```

按 2026-09-10 的最新要求，默认每次动作决策**不限查询次数**，每次仍可请求 1～8 个时间点。Agent3/4 可以反复查询，再提交动作；模型请求循环也没有依附于默认查询预算的隐藏次数上限。查询次数与实际动作次数分开记录。

`--max_frame_queries 0`（默认）表示不限次数。若为复现实验显式传正数，则保留原来的有限预算行为：超过预算返回“Frame query budget exhausted. Return actions now.”，后续关闭工具选项；该模式的模型请求数上限仍为查询预算加 4。格式纠正最多两次、单次 API 超时和任务原子动作上限仍有效；它们不表示取帧查询次数。

提示词说明目标 30 FPS，即名义约每 33.3 ms 一帧；时间参数可用小数。返回离散采集帧，不插值，不保证实际采样始终均匀。100 ms 是默认封装目标，不能当成采样间隔或最大查询延迟。以 `actual_time_s` 和 `available_until_s` 为准。

## 预算、日志和结果

- `--max_steps`：四组都按决策回合数限制，默认 100；一次 sequence 中的多个原子动作只计一个决策回合。
- `--max_sequence_actions`：仅限制单个决策回合内的原子动作数量，默认 100；不改变决策回合预算。
- 单个多动作序列不能超过 `max_sequence_actions`，也不会被程序静默截断；序列中的动作不会按数量扣减 `--max_steps` 的决策回合预算。
- `--max_sequence_actions` 默认 100；`--max_frame_queries` 默认 0（不限查询次数）。
- 每次模型请求、每次工具调用、返回图片数量、动作数、动作决策次数分别记录。
- 格式错误最多纠正两次；纠正请求也计入模型调用数，错误输出不执行。
- 结果自动按模型、实验批次和 agent 隔离到 `result_dir/<model>/<run_id>/agent1`～`agent4`，恢复运行时不会串用另一组成绩。

每个任务保存 `experiment.json`、`agent_metrics.json`、`trajectory.jsonl`、当前/查询图片、`recording.mp4`、`recording_ffmpeg.log`、`recording_index.json` 和原任务评分文件。配置与日志中不保存模型密钥。

### 模型调用复盘日志（2026-09-10 更新）

每个新运行任务另外保存实际的 `system_prompt.txt`。`experiment.json` 保存 instruction、工具定义、是否请求思考摘要、是否不限查询次数；`model_log_version=3` 标识统一时间线格式。

`trajectory.jsonl` 每个事件立即追加写盘，不再等整个动作决策结束。`model_log_version=3` 标识这一统一时间线格式；它是实时任务的唯一时间线，事件包括：

- `model_request`：请求编号、当前截图文件/时间/SHA-256、实际保留的历史截图引用、是否开放工具。
- `model_response`：保留原来的 `text`、`calls`、`usage`、耗时；增加 `provider_response`（接口原始 JSON 响应）及 `reasoning`（接口实际返回的思考文字/摘要数组）。
- `reasoning_status`：`returned` 表示有可读思考内容，`opaque` 表示只有空思考块/密文/被隐藏的块，`not_returned` 表示没有返回思考字段。后二者不能解释成“模型没有思考”。
- `frame_query_artifacts`：查询时间与实际取出图片的文件路径、采集时间；它是复盘日志，不发给模型。
- `tool_result`、`format_error`、`model_error`：工具反馈、格式纠正和接口错误，均与请求编号关联（图片文件事件按顺序紧随对应请求）。

思考内容单独记录，不送入动作 JSON 解析器；工具继续轮询时保留接口原始 thinking/signature 等上下文。日志不保存 HTTP 认证请求头。已经完成的历史记录不回写，也无法事后补出未保存的思考内容。

在兼容的 Anthropic Messages 通道中，加 `--thinking_summary` 会请求 `thinking={"type":"adaptive","display":"summarized"}`，并省略 temperature 字段；token 上限保持命令传入值。不开启时不改变模型原有 thinking 配置，仍会记录接口实际返回的思考字段。这个选项不是完整内部思考的导出功能；Claude 返回的是摘要，且某些请求可能没有可读摘要。中转站是否支持需要单独验证，失败不自动换模型/协议或重试成另一种 thinking 配置。依据：[Claude Thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)。

修改查询预算或启用 thinking_summary 后，用新的 `run_id` 保存实验；复现旧 Sonnet 5 试跑时应显式传 `--max_frame_queries 4` 并保留原思考配置。四组 prompt 原文和本次 A31 复盘见 [PROMPTS_AND_LOGGING_2026-09-10.md](/mnt/zhaorunsong/yhyx/OSWorld/validation/realtime_agents/PROMPTS_AND_LOGGING_2026-09-10.md)。

如果命令使用 `--result_dir results_four_agents`，正常实验结果的目录形如：

```text
results_four_agents/
└── claude-sonnet-4-6/
    └── exp001/                      # 同一批四组对比的编号，不代表时间
        ├── experiment_manifest.json # 批次设置和四组清单
        ├── launches.jsonl           # 四组所有实际启动/续跑时间
        ├── agent1/
        ├── agent2/
        ├── agent3/
        └── agent4/
            ├── launches.jsonl       # 该 agent 自己的启动/续跑明细
            ├── summary/
            │   ├── results.json
            │   └── realtime_gui_bench_metrics.json
            └── computer_13/screenshot/
                ├── args.json
                └── <domain>/<example_id>/
                    ├── experiment.json
                    ├── agent_metrics.json
                    ├── recording.mp4
                    ├── recording_index.json
                    ├── query_*.png
                    └── trajectory.jsonl
```

每个模型可以有多批实验，每批下面都有自己的四个 Agent。每组独立保存任务文件和 `summary/`，断点续跑只读取该模型、该批次、该组的完成记录。`realtime_gui_bench_metrics.json` 在评估对应 benchmark 时生成。模型名只出现一次，`--result_dir` 仍传公共根目录，程序自动添加模型、批次和 Agent 层级。不指定 `--agent_variant` 的普通 OSWorld 运行沿用原目录结构。

**推荐将实验编号和实际时间分开记录。** `--run_id exp001` 只标识“这四组属于同一次对比”，不表示四组同时开始。也可以用简短、易读的名字，例如 `realtime_v1`。Agent1 可以 17:30 启动，Agent2 可以 18:10 启动；它们使用同一个 `run_id`，各自的实际启动时间另存为带时区的 `agent_started_at`。

- 不指定 `--run_id`：每次启动自动生成一个新编号，含微秒和本机时区，创建新批次。
- 同批运行四组：给四条命令传相同的 `--run_id`，无需同时启动。
- 修改实验设置或做新一批实验时使用新编号，例如 `exp002`；不要用新配置覆盖同批同组的既有结果。
- 断点续跑：传原来的 `--run_id` 和 `--agent_variant`，跳过已经完成的任务。不要省略编号，否则会创建新批次。
- 启动记录追加到每组的 `launches.jsonl`；续跑会新增一行，保留之前的启动时间。`args.json` 保存本次启动的配置。
- 批次根目录的 `experiment_manifest.json` 保存批次设置和四组清单，`launches.jsonl` 集中列出每个 Agent 每次启动/续跑的实际时间；每个 Agent 目录也保留自己的明细。每个任务的 `experiment.json` 记录 `run_id`、`agent_started_at`、`task_started_at`；`agent_metrics.json` 另记录 `task_finished_at`。任务开始/结束的日历时间使用带 `+00:00` 的 UTC 时间。`task_started_at` 是主机开始处理该任务的时刻，包含后续 reset 和准备阶段；它不是录像的第 0 秒。
- 截图和 `get_frames` 仍使用前文规定的 VM 任务相对时间，以录像第一帧为 0 秒。目录编号和日历日志不会改变图片时间。

同一任务运行时，虚拟机内也会使用独立的 `/tmp/osworld-realtime/<session_id>/` 目录；结束时视频、索引和日志下载到上面的主机任务目录。`validation/realtime_agents/` 只保存接入冒烟测试证据，不是正式 benchmark 成绩目录。

## 运行

在 OSWorld 目录使用现有 `osworld-yhyx` 环境。模型凭据继续从 `.env` 或环境变量读取（如 `ANTHROPIC_AUTH_TOKEN` 或 `PACKY_API_KEY`），不写进命令或代码。

```bash
# 这一批四组共用一个编号；各组的真实启动时间由程序分别记录。
EXPERIMENT_ID=exp001
python scripts/python/run_multienv.py \
  --agent_variant agent1 \
  --run_id "$EXPERIMENT_ID" \
  --action_space computer_13 \
  --observation_type screenshot \
  --provider_name docker \
  --path_to_vm docker_vm_data/Ubuntu-realtime-gui.qcow2 \
  --headless \
  --model claude-sonnet-4-6 \
  --api_format anthropic_messages \
  --api_base_url https://www.packyapi.ai \
  --test_all_meta_path evaluation_examples/test_realtime_gui_bench.json \
  --max_steps 100 \
  --sleep_after_execution 0 \
  --num_envs 1 \
  --install_realtime_server \
  --client_password "$OSWORLD_VM_PASSWORD" \
  --result_dir results_four_agents
```

把 `agent1` 换成 `agent2`、`agent3`、`agent4`，沿用同一个 `EXPERIMENT_ID`，其余实验配置保持一致。如果隔天或换终端继续运行，直接传 `--run_id exp001`。这个编号保持不变，启动时间仍会如实记录新时刻。`OSWORLD_VM_PASSWORD` 是实验 VM 的 sudo 密码，和模型 API 密钥不同。

`--install_realtime_server` 表示：**任务运行前，自动给当前虚拟机安装这次新增的功能。无需先重新打包镜像。**

游戏在虚拟机里运行，原镜像里的 OSWorld 服务还不认识“边录边查历史帧”和“整串执行动作”这些新请求。只修改主机上的 agent 代码，虚拟机里的旧服务不会自动更新，所以需要把服务代码传进去。

启用后，每个任务在 reset 后、60 秒环境准备等待前执行：上传 `fmp4.py`、`realtime.py` → 在 VM 的 `main.py` 中接入新功能并保留原文件备份 → 重启 OSWorld 服务以加载新代码 → 检查新接口可用。重启的是服务程序，不是整台虚拟机；安装发生在正式计时和录制开始之前，不是每个动作回合都安装。不会上传模型密钥。

如果 reset 会恢复原始镜像，之前临时装入的代码也可能被恢复掉，因此目前保留这个参数，让程序在每个任务 reset 后自动安装。以后也可以把扩展预装进镜像，并保存为 reset 实际使用的镜像/快照；确认它已经包含正确版本后，就能省略这个参数。临时安装不会自动替你制作一份更新后的发布镜像。

要制作预装镜像，执行：

```bash
python scripts/python/build_realtime_vm_image.py \
  --source docker_vm_data/Ubuntu-realtime-gui.qcow2 \
  --output docker_vm_data/Ubuntu-realtime-gui-fmp4.qcow2
```

脚本会启动临时 VM，使用同一套安装逻辑写入临时 overlay，关闭 VM 后用 Docker 镜像内的 `qemu-img` 生成新的 qcow2。原镜像只读且不会被修改；需要替换输出时显式加 `--force`。VM 要求 sudo 密码时从 `OSWORLD_VM_PASSWORD` 读取。生成成功后，把四组命令的 `--path_to_vm` 换成新文件，并省略 `--install_realtime_server`。

原动作空间不是自由 Python 代码，因此四组入口要求显式选择 `computer_13`。

## 验证

自动化检查：

```bash
python -m pytest -q \
  tests/test_realtime_agents.py tests/test_fmp4_live.py \
  tests/test_realtime_runner.py tests/test_recording_log_download.py \
  tests/test_realtime_gui_bench_results.py tests/test_realtime_result_layout.py
```

实时视频测试需要 FFmpeg；测试主机没有 FFmpeg 时可安装 `imageio-ffmpeg`，VM 使用自己的 FFmpeg。测试覆盖四组、三种消息协议、JSON 工具请求、多次查询、动作预算、只截一次图、未完成片段、乱序时间查询、非均匀时间戳和正在写入/结束后独立解码一致性。

真实 VM 与中转站的本次联调证据位于 `validation/realtime_agents/`。联调只检查接入链路，不是四组的完整 benchmark 成绩。Chat/Responses 的协议测试不代表当前 Packy 密钥已获相应模型通道权限。

本轮最终结果：46 项自动化检查通过；四组均通过真实 Claude 接口检查；实时 fMP4 帧与结束后读取的结果一致，帧时间与采集 PTS 逐帧对齐。详细证据与失败重试记录见 `validation/realtime_agents/VALIDATION.md`。

协议和容器依据：[OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)、[Claude 工具定义](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)、[FFmpeg Fragmentation](https://ffmpeg.org/ffmpeg-formats.html#Fragmentation)。
