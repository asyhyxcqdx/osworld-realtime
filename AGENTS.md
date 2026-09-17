# AGENTS.md — 给 AI 编码助手的操作手册

这个仓库是 **OSWorld 的实时 GUI Agent 基准**（RealtimeGame v1.1(3)：69 个网页游戏 × 四类 Agent × 多模型）。
本文件是给 AI 助手（Codex / Claude Code / Cursor / DSH 等）看的**唯一操作规程**：接手时先读完本文件，再动手。
面向人的项目介绍见 [`README_CN.md`](README_CN.md)。

---

## 1. 当前状态（接手前必读）

| 项 | 状态 |
|---|---|
| 环境 | 69 个游戏 + 69 个任务配置已接入并通过验收 |
| 配置 | 40 份 YAML（8 款实验模型 × 4 组 + Fable5/Astra 8 份历史基线） |
| VM 镜像 | `docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2`（**不入仓库**，约 22.8 GiB / 24,493,359,104 字节） |
| 进度与成绩 | **不在仓库里**：批量实验由外部同学执行，逐任务成绩与逐模型金额写进飞书总表（表标识见 [`HANDOFF_CN.md`](HANDOFF_CN.md)）。不要在文档里写死"已跑/未跑" |

> 接手的主要任务是**维护与支撑**：修 Agent/配置/文档、帮执行方排错、按 [`HANDOFF_CN.md`](HANDOFF_CN.md) 交付批量实验。

---

## 2. 仓库结构（各部分是什么）

代码、配置和环境已经整理定稿：**跑实验的同学不需要改任何代码**，直接用 `HANDOFF_CN.md` 的命令即可。这一节只是让人知道东西在哪。

| 路径 | 是什么 |
|---|---|
| `evaluation_examples/websites/realtime_gui_bench/games/<id>/index.html` | 69 个游戏本体（环境） |
| `evaluation_examples/examples/realtime_gui_bench/<uuid>.json` | 69 个任务配置 |
| `evaluation_examples/test_realtime_gui_bench.json` | 任务清单（69 个 UUID） |
| `configs/realtime_agents/*.yaml` | 40 份 Agent 配置，**system prompt 的唯一来源** |
| `mm_agents/realtime_*.py` | 宿主侧 Agent（协议 / 坐标 / 流式 / 配置 / `.env` 加载） |
| `lib_run_realtime.py`、`lib_realtime_trajectory.py` | 单任务运行器与轨迹渲染 |
| `desktop_env/server/realtime.py`、`fmp4.py` | VM 内部服务 |
| `desktop_env/evaluators/{getters,metrics}/realtime_gui.py` | 评测与评分 |
| `scripts/python/run_realtime_batch.py` | **批量运行 + 逐模型记账**（首选入口） |
| `scripts/python/export_realtime_results.py` | 结果目录 → 飞书总表 16 列（CSV/JSON + `lark-cli` 写入） |
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
hf download bright-star123/osworld-realtime-vm \
  Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2 --local-dir docker_vm_data
ls -l docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2   # 应为 24493359104 字节

# ⑤ 密钥与网关：复制 .env.example 为 .env（已被 git 忽略）一次配好，
#    也可以临时用环境变量。变量名按 YAML 的 api.key_env：
#      PACKY_COMMON_API_KEY          sonnet-5 / sol / gemini / qwen / deepseek
#      PACKY_KIMI_API_KEY            kimi-k3
#      PACKY_GLM_MINIMAX_API_KEY     glm-5.3-flash / MiniMax-M3
#      REALTIME_API_KEY              所有模型共用一把时的兜底
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
  --num_envs 4 \          # 每个 env 一台 VM；默认 1，批量建议 4–8（按 CPU/内存与网关限流定）
  --keep-going \          # 单个模型失败不中断整批
  --exclusive-keys-confirmed
```

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

- 输出 `<result_dir>/export_<run_id>.csv`（utf-8-sig，Excel 可直接开）与 `.json`；加 `--lark-*` 后用 `lark-cli base +record-batch-create` 每 200 行一批写入。表格链接与 16 列口径见 [`HANDOFF_CN.md`](HANDOFF_CN.md) 第 9 节。
- 写入前需要 `npm install -g @larksuite/cli` + `lark-cli auth login`（需要 `base:record:create`、`base:record:read`、`wiki:node:retrieve` 权限）；不想用 CLI 就只产 CSV，用飞书表自带的「导入」。
- `--charges <cost_report.json>` 用账单原值填成本；不给 `--prices/--charges` 则成本列留空。**同一次 run 只导一次**，写入是新增不是覆盖。

### 续跑（补齐没成绩的任务）

**同一条命令、同样的 `--result_dir` / `--run_id` 再跑一次即可**，不需要额外参数：
`run_multienv.py` 会自动跳过已有 `result.txt` 的任务，并**清空**没有 `result.txt` 的目录后重跑（因为 `trajectory.jsonl` 是追加模式，不清会新旧混写）。

### 看轨迹

每个任务目录有 `trajectory.html`（离线单文件，含每轮截图、模型回复、动作、历史帧、耗时），浏览器直接打开即可。

---

## 5. 硬性约定（违反会破坏实验数据）

1. **密钥绝不写进文件**：只从环境变量（或已被 git 忽略的 `.env`）读；不写进 YAML、源码、文档、示例或结果。`.env` 与 `.env.example` 是两个不同的文件，只有后者能提交。提交流前自查 `git diff`。
2. **结果不入库**：`results_*`、`*.mp4`、`trajectory.jsonl`、`*_billing_*.json`、`docker_vm_data/` 都不提交。成绩与金额记在仓库外的结果表。
3. **YAML 是 system prompt 的唯一来源**：不存在代码内置的默认 prompt；`RealtimeAgent` 缺 `system_prompt_text` 会直接报错。改 prompt 必须**新建 run_id**，不能与旧批次混。
4. **不要改游戏 HTML 和 VM 服务**：会导致旧成绩不可比；确实要改就得重打镜像并重新验收。
5. **改了 `desktop_env/` / `lib_run_*.py` 的评分或环境行为**：同步更新文档与测试，并在报告里说明"新旧成绩不可比"。

---

## 6. 已知坑（真实的血泪教训）

| 现象 | 原因与对策 |
|---|---|
| 同一个任务重跑后 `trajectory.jsonl` 里事件翻倍 | 它是追加模式；必须先清目录（内置续跑会自动清，手工重跑要自己清） |
| 某个模型"没有成绩" | `result.txt` 不存在 = **无有效成绩**（API/执行/评分异常），**不记 0 分**；只有 `result.txt=0.0` 才是"有效 0 分（正常失败）"。判分时两者必须区分 |
| 单轮花掉约 $20 | 模型可能退化（重复刷屏）直到撞上 `max_output_tokens`（128k），网关返回 `response.incomplete`。我们的行为是**停止且不执行半段**，但那一轮照样计费。日志显示 `Responses stream response.incomplete: None` 时，去网关明细看该轮的 `completion_tokens` 是否等于上限 |
| 整批突然中断 | 本机代理瞬断（`ProxyError: Connection refused`）会打断模型请求。批量脚本已把**账单抓取失败**降级为记录 `billing_error` 不中断；模型请求失败仍会让该任务变成"无有效成绩" |
| 模型"来不及操作" | 环境是**实时**的：模型思考期间游戏继续运行。需要精确时序时必须把"按住键 + 等待 + 松开"放进**同一条回复**，动作之间只用 `WAIT` 控时（否则一次思考 10–40 秒，角色早已走出平台） |
| `get_frames` 返回 `not_ready` | 请求的时间比"已录完的片段"新。重查同一时间即可，不是错误 |
| 线程/并发 | `--num_envs N` 会起 N 个进程 + N 台 VM，共用同一份 qcow2（容器内是**只读挂载**，不会互相写坏）。瓶颈是 CPU/内存与 API 限流，不是镜像 |
| 网关明细的 `model_name` | 与配置里的模型名**逐字一致**（`qwen3.8-max-0902`、`glm-5.3-flash`），据此按模型归集金额 |
| 飞书行数翻倍 | `export_realtime_results.py` 写入是**新增记录**，不是覆盖；同一次 run 只导一次，重导前先在飞书删旧行 |
| 轨迹里的 token 比网关少 | 流中断/未完成的请求没有 `model_response` 事件，token 统计不到；金额仍以网关值为准（`--charges`） |
| 换网关后脚本报账单接口错误 | 账单接口是 Packy 专用的；非 packyapi.ai 会自动跳过，也可显式加 `--skip-billing` |

---

## 7. 结果与产物

**单任务目录**（`<result_dir>/<model>/<run_id>/<agent>/computer_13/screenshot/realtime_gui_bench/<uuid>/`）：

```
system_prompt.txt      本次实际发送的完整 prompt（可逐字核对）
experiment.json        协议、坐标协议、模型参数
trajectory.jsonl       逐事件原始记录（含模型原始回复、动作、执行回执）
trajectory.html        离线查看器
result.json/result.txt 评分（pass_at_1 / pass_at_3，标量 = pass_at_3）
agent_metrics.json     请求数、动作决策数、帧查询数、termination_reason
initial_state.png / step_*.png / query_*.png
recording.mp4 + recording_index.json + recording_ffmpeg.log
```

**逐模型金额**（`cost_dir`）：`cost_report.json`（总表，每模型一行）、`<model>_summary.json`、`<model>_billing_before/after/checks.json`、`<model>_gateway_logs.json`、`<model>.log`。
金额口径：**以网关消费明细为准**（`gateway_log_charge_usd`），即时账单差额只作交叉核对；两者不一致都记录，不把"即时查询为 0"当免费。

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
| [`HANDOFF_CN.md`](HANDOFF_CN.md) | **执行同学作业单**：跑 69 任务 × 八模型并填飞书总表 |
| [`README_CN.md`](README_CN.md) | 项目入口（给人看） |
| [`SETUP_GUIDELINE_CN.md`](SETUP_GUIDELINE_CN.md) | 部署与运行：环境、镜像、命令、镜像构建、排错 |
| [`REALTIME_PROJECT_DESIGN.md`](REALTIME_PROJECT_DESIGN.md) | 项目与实验设计：环境与任务、接入与验收边界、四组 Agent、工具与时序、录像、预算、评分口径 |
| [`REALTIME_AGENT_GUIDE.md`](REALTIME_AGENT_GUIDE.md) | Agent 配置与运行：YAML 字段协议、程序流程、三种 API 协议、日志字段、轨迹查看器 |
| [`configs/realtime_agents/README.md`](configs/realtime_agents/README.md) | **模型↔协议↔密钥映射表、坐标适配（唯一权威）** |
| [`REALTIME_GUI_BENCH_PROTOCOL.md`](REALTIME_GUI_BENCH_PROTOCOL.md) | 游戏接口协议：`window.BENCH` 字段、状态机、写入规则、禁用快捷键、验收清单 |
