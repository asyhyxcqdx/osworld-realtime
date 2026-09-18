# 实时 GUI 部署与运行

面向部署方和维护者。只负责跑批量实验的同学请看 [执行同学作业单](HANDOFF_CN.md)；AI 助手看 [AGENTS.md](AGENTS.md)。
以下命令都从仓库根目录执行。

## 1. 环境

- Python 3.12+，依赖用 uv 或 pip：`uv sync`（或 `pip install -r requirements.txt`）。
- 测试与查看器额外需要：`pip install pytest imageio-ffmpeg`、`python -m playwright install chromium`。
- `docker pull happysixd/osworld-docker`。
- `/dev/kvm` 必须可用（`ls -l /dev/kvm`），否则 VM 慢到不可用。

## 2. 取 VM 镜像（不入仓库）

```bash
hf download bright-star123/osworld-realtime-vm --repo-type dataset \
  Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2 --local-dir docker_vm_data
ls -l docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2   # 应为 24493359104 字节
```

国内网络可以设 `HF_ENDPOINT=https://hf-mirror.com` 走镜像站（HuggingFace CLI 与仓库内下载器都认这个变量）；Docker provider 的等待与超时可用 `OSWORLD_DOCKER_LOCK_TIMEOUT_S` / `OSWORLD_DOCKER_API_TIMEOUT_S` 调整。这些可选变量都列在 `.env.example` 第五节。

镜像与仓库里的 `desktop_env/server/realtime.py`、`fmp4.py` 必须配套：录制前程序会比对两份源码哈希，不一致时先用 `--install_realtime_server` 安装当前服务，或重新构建镜像（见 §6）。

## 3. 配置（一次）

```bash
cp .env.example .env && chmod 600 .env
```

`.env`（已被 `.gitignore` 忽略）里填网关地址、key 和可选代理；批量脚本与 `run_multienv.py` 会自动加载，之后不需要再传 key 参数。变量名与 YAML 的 `api.key_env` 对应：

**一个模型一个变量**，变量名 = `PACKY_` + 模型名（全大写，非字母数字换成 `_`）+ `_API_KEY`：

```text
PACKY_CLAUDE_SONNET_5_API_KEY   claude-sonnet-5
PACKY_GPT_5_6_SOL_API_KEY       gpt-5.6-sol
PACKY_GEMINI_3_8_FLASH_API_KEY  gemini-3.8-flash
PACKY_QWEN3_8_MAX_0902_API_KEY  qwen3.8-max-0902
PACKY_KIMI_K3_API_KEY           kimi-k3
PACKY_DEEPSEEK_FLASH_API_KEY    deepseek-flash
PACKY_GLM_5_3_FLASH_API_KEY     glm-5.3-flash
PACKY_MINIMAX_M3_API_KEY        MiniMax-M3
PACKY_CLAUDE_FABLE_5_API_KEY    claude-fable-5
PACKY_GPT_6_ASTRA_API_KEY       gpt-6-astra
```

- `REALTIME_API_KEY`：所有模型临时共用一把 key 时的兜底；再低一层还有 `PACKY_API_KEY` 兜底

也支持直接设环境变量、`--keys-file <0600 JSON>`（`{"*": "sk-..."}` 表示全部模型）和无回显 stdin，只有仍缺 key 的模型才会走到后两种。

**换非 Packy 网关**：设 `REALTIME_API_BASE_URL`（一个地址作用于所有协议），或按协议设 `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL`。地址只写到网关根，程序自动补 `/v1/messages`、`/v1/chat/completions`。网关不是 packyapi.ai 时 Packy 专用账单接口自动跳过，成本用 `--skip-billing --prices prices.json` 从轨迹 token 计算。

走代理时保留 `HTTPS_PROXY` / `HTTP_PROXY`，并把 VM 地址放进 `NO_PROXY`（否则连不上虚拟机）。

## 4. 运行

跑实验的完整命令、参数、续跑与出表见 [执行同学作业单](HANDOFF_CN.md)；入口是 `scripts/python/run_realtime_batch.py`（逐模型记账、可续跑），它内部按模型顺序调用底层启动器 `scripts/python/run_multienv.py`（完整参数示例见 §6）。

- 结果落在 `<result_dir>/<model>/<run_id>/<agent>/computer_13/screenshot/realtime_gui_bench/<UUID>/`。
- 账单与汇总落在 `--cost_dir`（默认 `<result_dir>/_cost/<run_id>`）：`cost_report.json`、`<model>_summary.json`、账单快照与网关明细；成本列由导出脚本计算：`--prices`（token × 单价）或 `--charges`（网关账单原值）。
- 每个模型顺序执行，单个模型内失败的任务会继续下一个；**跨模型继续需要 `--keep-going`**。
- `--num_envs N` 每个模型同时开 N 台 VM（默认 1，批量建议 4–8）。
- 续跑用同一条命令、同样的 `--result_dir` / `--run_id` 再跑一次；改 prompt 或配置时必须换新的 `run_id`。
- 结果导出成飞书总表的 16 列用 `scripts/python/export_realtime_results.py`，字段口径见 [执行同学作业单](HANDOFF_CN.md) 第 9 节。

## 5. 镜像与服务

只更新宿主 Agent 不需要重打镜像。VM 服务源码变化后：

```bash
python scripts/python/install_realtime_server.py \
  --server_url http://<vm_ip>:5000        # 只上传两份服务源码并重启服务（--server_url 必填）
python scripts/python/build_realtime_vm_image.py \
  --source docker_vm_data/Ubuntu-realtime-gui.qcow2 \
  --output docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2
```

构建器会重启新镜像，检查源码、WAIT 实际时长和 100 动作序列，并写 `.verification.json`；输出已存在时默认拒绝覆盖，有意重建才用 `--force`。

## 6. 直接使用底层启动器

批量脚本内部调用 `run_multienv.py`。需要单模型单变体的完整控制时：

```bash
python scripts/python/run_multienv.py \
  --agent_variant agent4 \
  --model claude-sonnet-5 \
  --run_id packy_v11_exp001 \
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

- `--agent_variant` + `--model` 自动选择对应 YAML；不传 `--api_format`，协议来自配置。配置缺失或模型名冲突时报错，不换模型兜底。
- 实时入口只接受 1920×1080，其他尺寸在启动 VM 前报错。
- 全部模型与变体都用 `--max_steps 100`（默认值）：每个“模型 × Agent × 游戏”有 100 个动作决策回合，游戏内三次机会共用；`get_frames` 不增加动作决策数，所以模型请求次数可能更多。单次动作序列最多 100 个原子动作。
- HTTP 读取超时 120 秒、最多重试三次；执行失败的动作序列不自动重放。
- 环境重置、初始页面检查或录制启动阶段报错时可能只有运行日志，没有 `agent_metrics.json`：这种情况**没有有效成绩，不记作 0 分**。

## 7. 验证和排错

- 回归测试：`python -m pytest -q tests/test_realtime_*.py tests/test_fmp4_live.py tests/test_recording_log_download.py`（需要 ffmpeg；查看器用例需要 Chromium，可用 `REALTIME_VIEWER_CHROMIUM` 指定已有浏览器）。
- 真实 VM 链路自检（用模拟回复，不调付费 API、不产生成绩）：`python scripts/python/verify_realtime_runtime.py --artifacts /tmp/realtime-runtime-review`。
- 动作时序核对：普通动作不插 PAUSE，需要间隔时由模型显式调用 WAIT；检查 `info.sequence_actions[].duration_s`，不要只看请求参数。
- 游戏内部 `status=ready/running` 是游戏状态，不代表程序还在跑；结束原因看 `termination_reason`。
