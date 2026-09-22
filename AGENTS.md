# AGENTS.md — 给 AI 编码助手的操作手册

这个仓库是 **OSWorld 的实时 GUI Agent 基准**（RealtimeGame v1.1(3)：69 个网页游戏 × 四类 Agent × 多模型）。
本文件是给 AI 助手（Codex / Claude Code / Cursor / DSH 等）看的**唯一操作规程**：接手时先读完本文件，再动手。
面向人的项目介绍见 [`README_CN.md`](README_CN.md)。

---

## 1. 当前状态（接手前必读）

| 项 | 状态 |
|---|---|
| 环境 | 69 个游戏 + 69 个任务配置已接入并通过验收 |
| 配置 | 40 份 YAML（10 款模型 × 4 组） |
| VM 镜像 | `docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2`（**不入仓库**，约 22.8 GiB / 24,493,359,104 字节） |
| 进度与成绩 | **不在仓库里**：外部同学跑 5 个模型（qwen / deepseek / kimi / glm / MiniMax），我们跑其余 5 个（sonnet / sol / gemini / fable-5 / astra），逐任务成绩与金额都写进飞书总表。不要在文档里写死"已跑/未跑" |

> 接手的主要任务是**维护与支撑**：修 Agent/配置/文档、帮执行方排错、按 [`HANDOFF_CN.md`](HANDOFF_CN.md) 交付批量实验。

---

## 2. 仓库结构（各部分是什么）

代码、配置和环境已经整理定稿：**跑实验的同学不需要改任何代码**，直接用 `HANDOFF_CN.md` 的命令即可。这一节只是让人知道东西在哪。

| 路径 | 是什么 |
|---|---|
| `evaluation_examples/websites/realtime_gui_bench/games/<id>/index.html` | 69 个游戏本体（环境） |
| `evaluation_examples/examples/realtime_gui_bench/<uuid>.json` | 69 个任务配置 |
| `evaluation_examples/test_realtime_gui_bench.json` | 任务清单（69 个 UUID） |
| `configs/realtime_agents/*.yaml` | 40 份 Agent 配置，**system prompt 与 user prompt 的唯一来源** |
| `mm_agents/realtime_*.py` | 宿主侧 Agent（协议 / 坐标 / 流式 / 配置 / `.env` 加载） |
| `lib_run_realtime.py`、`lib_realtime_trajectory.py` | 单任务运行器与轨迹渲染 |
| `desktop_env/server/realtime.py`、`fmp4.py` | VM 内部服务 |
| `desktop_env/evaluators/{getters,metrics}/realtime_gui.py` | 评测与评分 |
| `scripts/python/run_realtime_batch.py` | **批量运行 + 逐模型记账**（首选入口） |
| `scripts/python/export_realtime_results.py` | 结果目录 → 飞书总表 18 列（CSV/JSON + `lark-cli` 写入） |
| `mm_agents/realtime_env.py`、`.env.example` | `.env` 配置层（网关、key、代理、运行默认值） |
| `scripts/python/run_multienv.py` | 底层启动器（`--num_envs` 并行、自动续跑） |
| `tests/test_realtime_*.py` | 回归测试 |
| `results_*`、`docker_vm_data/` | 运行产物与镜像，不入库（`.gitignore`） |

对维护者才有意义的一条影响面：游戏 HTML、任务配置、VM 服务或评分逻辑一改，**新旧成绩就不可比**，需要重新验收、必要时重打镜像，并换新的 `run_id`；改 prompt 同样要换 `run_id`。

---

## 3. 环境准备（新机器）

```bash
# ① Python 3.12 + 依赖
uv sync                      # 或 pip install -r requirements.txt
pip install pytest imageio-ffmpeg   # 跑测试额外需要（ffmpeg 供 test_fmp4_live）
python -m playwright install chromium   # 查看器测试需要；不装则该项被 skip（不是失败）

# ② 容器镜像（运行环境的容器，不是虚拟机镜像）
docker pull happysixd/osworld-docker

# ③ KVM 必须可用，否则 VM 极慢
ls -l /dev/kvm

# ④ 虚拟机镜像（22.8 GiB，仓库里没有，需从 HuggingFace 下）
hf download bright-star123/osworld-realtime-vm --repo-type dataset \
  Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2 --local-dir docker_vm_data
ls -l docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2   # 应为 24493359104 字节

# ⑤ 密钥与网关：复制 .env.example 为 .env（已被 git 忽略）一次配好，
#    也可以临时用环境变量。变量名按 YAML 的 api.key_env：
#      一个模型一个变量：PACKY_ + 模型名（大写，非字母数字→_）+ _API_KEY
#      例：PACKY_CLAUDE_SONNET_5_API_KEY / PACKY_GLM_5_3_FLASH_API_KEY / PACKY_MINIMAX_M3_API_KEY
#      REALTIME_API_KEY              临时共用一把时的兜底
#    换非 Packy 网关时设 REALTIME_API_BASE_URL（或按协议设 ANTHROPIC_/OPENAI_BASE_URL）

# ⑥ 网络：走代理时同时设 NO_PROXY 排除 VM 地址
export HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=$HTTPS_PROXY
export NO_PROXY=localhost,127.0.0.1,::1
```

**游戏数据集**（可选，仓库里已有同内容）：`hf download bright-star123/osworld-realtime-games --repo-type dataset --local-dir realtime-games`

---

## 4. 命令

### 跑实验（同学只需要这三步）

冒烟（一个模型、一个任务）→ 批量（默认全部 69 个任务）→ 出表写飞书。完整说明见 [`HANDOFF_CN.md`](HANDOFF_CN.md)。

```bash
python scripts/python/run_realtime_batch.py \
  --agent_variant agent4 --models gemini-3.8-flash \
  --run_id smoke_$(date +%Y%m%d) \
  --task 5169e1b0-1a7d-538b-8e59-8785c39460ce \
  --exclusive-keys-confirmed
```

密钥来源优先级：`.env` / 环境变量（模型自己的 `api.key_env`，再 `REALTIME_API_KEY`、`PACKY_API_KEY` 兜底）→ `--keys-file <0600 JSON>` → **无回显 stdin**（看到 `READY_KEYS_NO_ECHO` 后粘贴一行 `{"<model>": "sk-..."}`）。只有仍然缺 key 的模型才会走后面两种；启动时会打印每个模型用了哪种来源（`KEY_SOURCE`），不打印密钥本身。

### 维护者自测（只改代码时才需要，跑实验的同学可跳过）

```bash
REALTIME_VIEWER_CHROMIUM=<playwright chromium 路径> \
PYTHONPATH=/tmp/rt-deps \
python -m pytest -q tests/test_realtime_*.py tests/test_fmp4_live.py tests/test_recording_log_download.py
```

判据是**全绿**（不写死通过数量，用例数会随改动变化）。`test_maestro_minimax_provider.py` 缺上游依赖 `zhipuai`，与本项目无关，忽略即可；查看器用例需要 `python -m playwright install chromium`，未装时该项会 skip（不是失败）。

### 正式批量（69 任务 × 多模型）

```bash
python scripts/python/run_realtime_batch.py \
  --agent_variant agent4 \
  --run_id packy_v11_batch01 \
  --result_dir results_realtime_batches \
  --cost_dir /path/outside/repo/optional \
  --num_envs 4 \          # 每个 env 一台 VM；默认 1。高并发见下面两条说明
  --keep-going \          # 单个模型失败不中断整批
  --exclusive-keys-confirmed
```

- **并发数怎么定**：`--num_envs` 决定吞吐，**但它同时是"宿主机负载"变量**，所以**做 prompt/配置的分数 A/B 时固定同一个 `--num_envs`**（我们的 C 类对照一律用 6），只有追吞吐时才开大。**实测 16 并发并没有把 VM 拖慢**（录像 30.00 fps、动作派发中位 0.216 s、`WAIT` 误差 0.1%、帧时刻偏差中位 6 ms，与 6 并发同量级）——但同一配置换一批 run，C 类过/不过仍能翻 4 题（详见"单任务结果不可复现"那条），所以固定 `--num_envs` 是为了让 A/B 的噪声保持一致，不是因为高并发会系统性压分。
- **高并发（≥10）会偶发 `/realtime/start` 失败**，两种错误码各有原因，且**都会自己好**：`409 Timed out waiting for the first complete fragment`（ffmpeg 15 秒内没出首个片段）和 `400 Set the VM screen to 1920x1080 before starting the experiment`（VM 的屏幕还没变成 1920x1080）。实测 `num_envs=16` 的两批：一批 8/16 台吃 409、全部重试成功；另一批 3 台吃 400，**67 秒的重试预算不够**——同一台 VM 的下一个任务在约 2 分钟后正常开始，说明屏幕是要等约两分钟才对。所以宿主侧现在**自动重试 7 次**（等 2/5/10/20/30/30/30 秒，共约 157 秒；日志里会打 `start_realtime_recording attempt i/n failed, retrying in Ns: …`，遇到 400 还会顺带打 `(VM screen is (W, H))`，方便知道它当时是什么分辨率），另外 `--env_start_stagger_s`（默认 2 秒）把各台 VM 错开开机。重试是幂等的：VM 侧 `Recorder.start()` 出错时会自己 `stop()`，不留 ffmpeg、不留半开会话。**遇到失败仍先续跑补齐**（无成绩的任务会被清目录重跑）。
- **高并发下同一台 VM 的 Chrome 也可能答不动开跑前那次身份探针**：实测 `num_envs=16` 时 17 题里有 **2 题在 `lib_run_realtime.py:82` 的 `realtime_page_identity` 处 `TimeoutError: timed out in 10.0s`**，异常被 worker 兜底吃掉、任务**连目录都没有**（也就没有 `result.txt`，必须续跑）。现在这个只读探针也会重试 3 次（等 2/5/10 秒）；"页面丢失/重复"和 JS 报错仍然立刻上报不重试。`num_envs=16` 的现场数据：16 台里 8 台第一次 `/start` 就吃 409、全部重试成功，0 题因 `/start` 作废；录像仍是精确 30.00 fps，动作派发中位 0.216 s、`WAIT` 误差 0.1%、帧时刻偏差中位 6 ms —— **16 并发的时序没有可测退化**，同一批分数与 6 并发的差异来自任务本身的随机性（见下面一条）。
- 不传 `--task/--meta` 时默认跑全部 69 个任务。
- 结果：`<result_dir>/<model>/<run_id>/<agent>/<action_space>/<observation_type>/<domain>/<uuid>/`
- 账单与汇总：`--cost_dir`（默认 `<result_dir>/_cost/<run_id>`），含 `cost_report.json`、`<model>_summary.json`、账单快照与网关明细。
- **换非 Packy 网关**：加 `--skip-billing --prices prices.json`。程序检测到网关不是 packyapi.ai 时会自动跳过那套账单接口（打印 `GATEWAY_BILLING_SKIPPED`），成本改由每条轨迹里的 token 用量 × 单价算出（写进 `computed_charge_usd`）。`cost_report.json` 的总额优先用网关值，没有才用算出来的值。
- 网关地址解析顺序：`--api_base_url` → `$REALTIME_API_BASE_URL` → `$PACKY_API_BASE_URL` → 单个指向 Packy 的 `$ANTHROPIC_BASE_URL`/`$OPENAI_BASE_URL` → 内置 Packy 默认。其它网关**不强行下发** `--api_base_url`，交给 Agent 按协议各自解析，避免把 OpenAI 协议的模型打到 Anthropic 地址上。

### 导出结果并写入飞书总表

```bash
python scripts/python/export_realtime_results.py \
  --result_dir results_realtime_batches --run_id <run_id> --prices prices.json \
  --lark-base-token DVwrbns4LaLi8oswq9XcTdlHnGf \
  --lark-table-id tblhBdTpZMEqX5Qh [--lark-dry-run]
```

- 输出 `<result_dir>/export_<run_id>.csv`（utf-8-sig，Excel 可直接开）与 `.json`；加 `--lark-*` 后用 `lark-cli base +record-batch-create` 每 200 行一批写入。表格链接与 18 列口径见 [`HANDOFF_CN.md`](HANDOFF_CN.md) 第 9 节。
- 写入前需要 `npm install -g @larksuite/cli` + `lark-cli auth login`（需要 `base:record:create`、`base:record:read`、`wiki:node:retrieve` 权限）；不想用 CLI 就只产 CSV，用飞书表自带的「导入」。
- `--charges <cost_dir>/<model>_per_task_charge.json` 用**逐任务**账单原值填成本（每行填自己的金额）；只给 `cost_report.json`（每模型一个总额）时无法拆到任务，成本列留空并打印 `CHARGES_TOTAL_ONLY`。不给 `--prices/--charges` 则成本列留空。**同一次 run 只导一次**，写入是新增不是覆盖。

### 轨迹判官（作弊审查，跑实验时自动执行）

每条任务**落分之前**，`lib_run_realtime.py` 会调一次判官（`mm_agents/realtime_auditor.py`；system prompt = `configs/realtime_agents/auditor.txt`；判官模型 = `REALTIME_JUDGE_MODEL`，默认 `deepseek-flash`）：输入就是这条轨迹自己的原始 JSONL —— **从最后一条 `model_request` 那一行到文件末尾**，一行不改、不重排、不改字段名（事件字段、三种协议的消息、工具调用参数、工具结果、思考字段全在里面；图片字节在**写日志时**就已经被换成哈希和长度了）；每次判完都把这份输入原样存到任务目录的 `audit_input.txt`。返回 `NOT_CHEAT` / `CHEAT` / `CHEAT_ATTEMPT` / `UNCERTAIN`。

- `CHEAT` → `result` 和 `result.txt` 写 **0**；`CHEAT_ATTEMPT`、`UNCERTAIN`、`NOT_CHEAT` → **保留原分**；
- **判官没跑成功 → 不写 `result.txt`**：这条任务算未完成，用同样的命令续跑会重跑它（并重新判）。所以分数**只写一次**，且一定是判过之后的最终分；
- 缺 `REALTIME_JUDGE_API_KEY` → **开跑前直接报错**（见 `.env.example` 第六节）；
- 判官花费走它自己那把 key；**建议给判官单独一把 key**，否则它的调用会被算进同款被测模型的账单。

单独（补）判已经跑完的结果目录：

```bash
python -m mm_agents.realtime_auditor <任务目录>                          # 一条，打印结论
python -m mm_agents.realtime_auditor --result_dir <dir> --run_id <id>    # 批量：只补没判过/判失败的
python -m mm_agents.realtime_auditor --result_dir <dir> --force          # 全量重判（换判官模型/改 prompt 时）
python -m mm_agents.realtime_auditor <任务目录> --dry-run                 # 只写出 audit_input.txt，不调判官（正常判完也会存这份）
```

判据是 `result.txt` 存在（有有效成绩才判）。

### 续跑（补齐没成绩的任务）

**同一条命令、同样的 `--result_dir` / `--run_id` 再跑一次即可**，不需要额外参数：
`run_multienv.py` 会自动跳过已有 `result.txt` 的任务，并**清空**没有 `result.txt` 的目录后重跑（因为 `trajectory.jsonl` 是追加模式，不清会新旧混写）。

### 看轨迹

每个任务目录有 `trajectory.html`（离线单文件，含每轮截图、模型回复、动作、历史帧、耗时），浏览器直接打开即可。

---

## 5. 硬性约定（违反会破坏实验数据）

1. **密钥绝不写进文件**：只从环境变量（或已被 git 忽略的 `.env`）读；不写进 YAML、源码、文档、示例或结果。`.env` 与 `.env.example` 是两个不同的文件，只有后者能提交。提交流前自查 `git diff`。
2. **结果不入库**：`results_*`、`*.mp4`、`trajectory.jsonl`、`*_billing_*.json`、`docker_vm_data/` 都不提交。成绩与金额记在仓库外的结果表。
3. **YAML 是 system prompt 与 user prompt 的唯一来源**：不存在代码内置的默认 prompt；`RealtimeAgent` 缺 `system_prompt_text` 会直接报错，配置里缺 `user_prompt` 会被拒绝。改 prompt 必须**新建 run_id**，不能与旧批次混。
4. **不要改游戏 HTML 和 VM 服务**：会导致旧成绩不可比；确实要改就得重打镜像并重新验收。
5. **改了 `desktop_env/` / `lib_run_*.py` 的评分或环境行为**：同步更新文档与测试，并在报告里说明"新旧成绩不可比"。
6. **分数只在判官跑完之后写一次**：不要手工改 `result.json` / `result.txt`。换判官模型或改了 `configs/realtime_agents/auditor.txt` 之后，用 `python -m mm_agents.realtime_auditor --result_dir <dir> --force` 重判，而不是手改文件。

---

## 6. 已知坑（真实的血泪教训）

| 现象 | 原因与对策 |
|---|---|
| 同一个任务重跑后 `trajectory.jsonl` 里事件翻倍 | 它是追加模式；必须先清目录（内置续跑会自动清，手工重跑要自己清） |
| 某个模型"没有成绩" | `result.txt` 不存在 = **无有效成绩**（基础设施故障：API/网络/VM/评分异常，**或判官没跑成功**），**不记 0 分**，重跑补齐；`result.txt=0.0` = **有效 0 分**（可能是游戏没过、模型违反动作协议，或判官判了 `CHEAT`）。其中**模型违反动作协议**（连续 3 轮不回工具调用、越权快捷键、坐标越界等）会记 `termination_reason: run_error`（`result.json` 字段与正常局完全一致，具体原因在 `trajectory.jsonl` 的 `run_error` 事件里）—— 这是模型失败，不能当"无成绩"忽略；但**若违规后连页面都读不到**（评测抛异常），按基础设施故障处理：不写分、续跑重跑（页面里可能已记着通过）。判官判了作弊的，看 `result.json` 的 `judge.label` |
| 某条任务在 `model_error: JSONDecodeError … column N` 处整条作废（`result.txt` 都没有） | **模型把工具参数写成了非法 JSON**：实测 deepseek-flash 会把唯一的数组参数写成 `{"times_s": 6.0, 6.5, 7.0}`（漏掉 `[`），同一条请求重放 **24%** 复现；789 次请求里 1.1%，69 条任务里死 13%。**旧行为**：anthropic 协议在流式收尾处就 `json.loads`、失败即抛裸 `ValueError`，绕过了 `predict()` 里的纠错重试，于是整条任务被当成"基础设施故障"并靠续跑反复重跑（等于重掷骰子抬分）。**现行为**（`mm_agents/realtime_stream.py`）：解析失败时保留原始字符串往下传 —— 动作调用走 `format_error` 纠错（同一决策内 ≤2 次），仍改不掉才记 `run_error` + **有效 0 分**（模型失败）；`get_frames` 调用变成一条 error 工具结果让模型当场重查，不占纠错次数。真断流（没有 `message_stop`/块没收完）仍抛 `IncompleteStreamError`（照旧无成绩、续跑重试） |
| 单轮花掉约 $20 | 模型可能退化（重复刷屏）直到撞上 `max_output_tokens`（128k），网关返回 `response.incomplete`。我们的行为是**停止且不执行半段**，但那一轮照样计费。日志显示 `Responses stream response.incomplete: None` 时，去网关明细看该轮的 `completion_tokens` 是否等于上限 |
| 整批突然中断 | 本机代理瞬断（`ProxyError: Connection refused`）会打断模型请求。批量脚本已把**账单抓取失败**降级为记录 `billing_error` 不中断；模型请求失败仍会让该任务变成"无有效成绩" |
| 零星 `Model API HTTP 402 … insufficient balance (1008)` 打死任务 | 网关的**上游渠道**临时没钱（不是你的 key 额度超了；key 额度看 `hard_limit_usd`）。网关在多渠道间轮询，所以是间歇性的 —— `mm_agents/realtime_agent.py` 现在把 402 当可重试：退避 1/2/4/8 秒共 **5 次**（其它 429/5xx 仍是 3 次）；仍失败才按"无有效成绩"杀掉该任务，续跑补齐。撞上时先去 Packy 后台看该模型的**渠道余额**，光重试救不了渠道级长时间欠费 |
| 批量 40 秒就"跑完"、`0/69 tasks have result.txt` | 任务配置缺了 `instruction` 字段：环境核心 `desktop_env.py::_set_task_info` 必需它，每个任务都在 `env.reset()` 抛 `KeyError`。这个字段**不能删**；实时 Agent 的任务消息来自 YAML 的 `user_prompt`，运行时（`lib_run_realtime.py`）会把它覆盖成当前变体，所以文件里放哪套都行（现在放的是 combine 那套） |
| 模型"来不及操作" | 环境是**实时**的：模型思考期间游戏继续运行，prompt 里也已写明"思考与回复期间时间在真实流逝"。需要精确时序时必须把"按住键 + 等待 + 松开"放进**同一条回复**，动作之间只用 `WAIT` 控时（否则一次思考 10–40 秒，角色早已走出平台） |
| `get_frames` 返回 `not_ready` | 请求的时间比"已录完的片段"新。重查同一时间即可，不是错误 |
| 线程/并发 | `--num_envs N` 会起 N 个进程 + N 台 VM，共用同一份 qcow2（容器内是**只读挂载**，不会互相写坏）。瓶颈是 CPU/内存与 API 限流，不是镜像 |
| 网关明细的 `model_name` | 与配置里的模型名**逐字一致**（`qwen3.8-max-0902`、`glm-5.3-flash`），据此按模型归集金额 |
| 飞书行数翻倍 | `export_realtime_results.py` 写入是**新增记录**，不是覆盖；同一次 run 只导一次，重导前先在飞书删旧行 |
| 轨迹里的 token 比网关少 | 流中断/未完成的请求没有 `model_response` 事件，token 统计不到；金额仍以网关值为准（`--charges`） |
| 换网关后脚本报账单接口错误 | 账单接口是 Packy 专用的；非 packyapi.ai 会自动跳过，也可显式加 `--skip-billing` |
| 单任务结果不可复现：同一个任务、同一份配置，换一批 run 过/不过会翻面 | C 类 17 题跑了 7 个批次（4 组配置）实测：**44 次过关里 43 次发生在第 2/3 次尝试**（`pass@1` 在 7 个批次里 6 个是 0），C29 6/7、C27 5/6 属于"会做"，C12/C31/C34/C35 是 0/7"不会做"，中间 7 题只有 1~3/7 —— 翻面全在中间这档。对源码核对：`c36` 要求起跳后 0.429 s **±100 ms** 按 J、`c37` 松键窗口 **±200 ms**、`c3` 闪现 500 ms + 起跑延迟 100 ms，而模型给的等待常数是**猜的**（同一题两批分别猜 `WAIT(0.35)` 和 `WAIT(0.20)`），猜进窗口就过、猜偏就不过。**所以：单任务分数不能用来比较 prompt/并发；要看批次总分（C 类实测在 3~10 之间摆动）或多批平均**；失败后它还会去按 `enter` 找"隐藏重开"（判官记 `CHEAT_ATTEMPT`，分数不受影响） |
| 某个任务突然"无成绩"，`agent_metrics.json` 里是 `run_error: Realtime page was reloaded, navigated, or replaced` | **模型自己把页面弄重载了**：三次机会用完后它想找"隐藏的重开按钮"，就用键盘遍历焦点（`PRESS(tab)` 把焦点送出页面 → `PRESS(enter)` 在地址栏等于重新导航）。没有 `result.txt` 就不算分、还会被续跑（等于白拿一次重掷）。两道防线已加：`tab`/`enter`/`return`/`esc`/`escape` 被宿主侧禁用并给出说明性纠正（69 个游戏用 `e.key`/`e.code`/`keyCode` 三种写法都不使用这三个键，禁用不影响玩法），system prompt 的 Evidence 段写明"成功或没有剩余尝试后页面冻结、没有隐藏重开入口、也没有可发现的东西"。历史实例：trim 批 C39（续跑后从 0 变 1）、contract 批 C30（作废） |

---

## 7. 结果与产物

**单任务目录**（`<result_dir>/<model>/<run_id>/<agent>/computer_13/screenshot/realtime_gui_bench/<uuid>/`）：

```
system_prompt.txt      本次实际发送的 system prompt（YAML 的 system_prompt + 坐标约定，可逐字核对；任务 user 消息来自 YAML 的 user_prompt，见轨迹首条 user 消息）
experiment.json        协议、坐标协议、模型参数
trajectory.jsonl       逐事件原始记录（含模型原始回复、动作、执行回执）
trajectory.html        离线查看器
result.json/result.txt 评分。`pass_at_1`/`pass_at_3` 是**游戏原值**；`result` 与 `result.txt` 是**最终分**（判官判 `CHEAT` 时为 0，否则等于 `pass_at_3`），`result.json` 另有 `judge` 块（`label`/`confidence`/`evidence`/`reasoning`/`model`/`judged_at`）。模型违反动作协议而中止时同样写 `result.txt=0.0`：评分走正常读法（页面是 `running` 也算有效 0，`status`/`attempts_completed`/`raw_bench` 就是页面原样）；**页面完全读不到则不写分**（那是基础设施故障，页面里可能已记着前几次 attempt 的通过，伪造 0 会抹掉真实成绩）：只留 `run_error` 事件与 `agent_metrics.json` 作为"模型违规"的证据，这条任务算未完成、续跑重跑。**判官没跑成功反而不写 `result.txt`**（那条任务算未完成，会重跑）。判读：`result.txt=0` + `termination_reason: run_error` = 模型失败；`judge.label` = 判官的结论；无 `result.txt` = 基础设施故障或判官没跑成（都要重跑）
agent_metrics.json     请求数、动作决策数、帧查询数、termination_reason
audit_input.txt        判官这次实际读到的输入（判官跑过才有，便于回看判罚依据）
initial_state.png / step_*.png / query_*.png
recording.mp4 + recording_index.json + recording_ffmpeg.log
```

**运行级文件**（`<result_dir>/<model>/<run_id>/`）：`experiment_manifest.json`、`.experiment.lock`、`launches.jsonl`（本批启动记录）、`<agent>/launches.jsonl`（启动记录）、`<agent>/summary/results.json`。最后这个是运行器顺带写的扁平汇总（逐任务追加 `task_id`/`score`/`status`），**没有任何下游依赖，判分不要读它**——里面的 `status: "success"` 只表示评估正常跑完，分数仍可能是 0。

**逐模型金额**（`cost_dir`）：`cost_report.json`（总表，每模型一行）、`<model>_summary.json`、`<model>_charges.json`（每次调用的账单记录，见下）、`<model>_per_task_charge.json`（逐任务金额，见下）、`<model>_billing_before/after/checks.json`、`<model>_gateway_logs.json`、`<model>.log`。
网关只有"累计计价器"，所以**每次调用只能测出自己那一段的花费**；脚本会把每次调用的金额追加进 `<model>_charges.json`，summary 里的 `actual_charge_usd` / `gateway_log_charge_usd` / `gateway_charge_count` 等就是这些记录的**累加值**，所以同一 `run_id` 下的子集续跑不会把整段金额冲掉。某次调用的账单查询失败（如断网）会记进 `billing_errors` 并且那一段金额缺失，需要用只读的网关明细补算（本轮 Gemini 的第 1 段就是这样补的）。
金额口径：**以网关消费明细为准**（`gateway_log_charge_usd`），即时账单差额只作交叉核对；两者不一致都记录，不把"即时查询为 0"当免费。

**逐任务金额**：跑完一段会顺手把网关明细按**请求**归到任务上，写进 `<model>_per_task_charge.json`（`per_task` = 每个任务累计金额，`total_usd` = 它们的和，`unattributed_usd` = 没能归到任何任务的部分）。归集方式是逐条对账：先按 token 数配对，再取时间最接近的那条轨迹记录 —— 所以 `--num_envs` 并发跑也不会串。轨迹缺失（被清空重跑）或流中断（没记 usage）的请求**归不进任何任务，就留在 `unattributed_usd` 里，不硬塞给别的任务**。它同样按调用累加，子集续跑不会把已有金额冲掉。**口径**：只有最终留下 `result.txt` 的那条轨迹的花费才计入 `per_task`（飞书 `成本` 列就是它，合计即"有效成绩的花费"）；调试、被清空重跑的请求留在 `unattributed_usd`，**只用于对账，不计入任何统计或汇报**。某一段的账单抓取失败时（如代理瞬断），该段金额不在记账里，要用只读的网关明细（`<model>_gateway_logs.json`）按上面的口径补算。

模型行的口径：计数（`requests/responses/decisions/frame_queries`）与 token 是**全部任务求和**；`pass_at_1`/`pass_at_3`/`pass_at_3_mean` 是**对全部任务取均值**（`tasks_total` 为分母，无成绩的任务按 0 计入），同时给出 `tasks_scored`。`status` 在模型行恒为 `null`（它只对单个任务有意义）。只补跑一部分任务（`--task`/`--meta`）时，新行会与 `<model>_summary.json` 里已有的逐任务行**按 `task_dir` 合并**（重跑的那条以新值为准），模型行再按合并后的全集重新聚合 —— 所以子集续跑**不会**把整段记账缩小。`<model>_summary.json` 里的 `tasks` 数组始终保留每个任务一行。

---

## 8. 已发布的制品（HuggingFace）

| 仓库 | 内容 |
|---|---|
| `bright-star123/osworld-realtime-games` | 69 个游戏 + 69 任务配置 + `manifest.json`（逐文件 sha256） |
| `bright-star123/osworld-realtime-vm` | VM 镜像（22.8 GiB） |
| Collection `bright-star123/osworld-realtime-bench` | 上面两个的统一入口 |

---

## 9. 文档索引

| 文档 | 内容 |
|---|---|
| [`HANDOFF_CN.md`](HANDOFF_CN.md) | **执行同学作业单**：跑 69 任务 × 5 个模型并填飞书总表 |
| [`README_CN.md`](README_CN.md) | 项目入口（给人看） |
| [`SETUP_GUIDELINE_CN.md`](SETUP_GUIDELINE_CN.md) | 部署与运行：环境、镜像、命令、镜像构建、排错 |
| [`REALTIME_PROJECT_DESIGN.md`](REALTIME_PROJECT_DESIGN.md) | 项目与实验设计：环境与任务、接入与验收边界、四组 Agent、工具与时序、录像、预算、评分口径 |
| [`REALTIME_AGENT_GUIDE.md`](REALTIME_AGENT_GUIDE.md) | Agent 配置与运行：YAML 字段协议、程序流程、三种 API 协议、日志字段、轨迹查看器 |
| [`configs/realtime_agents/README.md`](configs/realtime_agents/README.md) | **模型↔协议↔密钥映射表、坐标适配（唯一权威）** |
| [`REALTIME_GUI_BENCH_PROTOCOL.md`](REALTIME_GUI_BENCH_PROTOCOL.md) | 游戏接口协议：`window.BENCH` 字段、状态机、写入规则、禁用快捷键、验收清单 |
