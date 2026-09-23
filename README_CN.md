# Realtime GUI 项目入口

基于 OSWorld 的实时 GUI Agent 基准：**69 个网页游戏 × 4 组 Agent × 多模型**，游戏接口协议 `realtime-gui-bench/1.1`，最终环境 RealtimeGame v1.1(5)。

- 要**跑批量实验**（69 任务 × 多模型、写飞书总表）→ [执行同学作业单](HANDOFF_CN.md)（外部同学跑 5 个模型，`claude-sonnet-5` / `gpt-5.6-sol` / `gemini-3.8-flash` 由我们跑）
- 是 **AI 助手**，或要改代码 → [AGENTS.md](AGENTS.md)（操作规程：仓库地图、硬性约定、已知坑）
- 要**部署、调参数、重建镜像、排错** → [部署与运行](SETUP_GUIDELINE_CN.md)

## 快速开始

```bash
uv sync && pip install pytest imageio-ffmpeg && python -m playwright install chromium
docker pull happysixd/osworld-docker
hf download bright-star123/osworld-realtime-vm --repo-type dataset \
  Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2 --local-dir docker_vm_data
cp .env.example .env                     # 填网关地址与 key（.env 不入库）

# 冒烟：一个模型一个任务
python scripts/python/run_realtime_batch.py \
  --agent_variant agent4 --models gemini-3.8-flash \
  --run_id smoke_$(date +%Y%m%d) \
  --task 5169e1b0-1a7d-538b-8e59-8785c39460ce \
  --exclusive-keys-confirmed
```

正式批量去掉 `--task`，加上 `--num_envs 4 --keep-going`；**续跑就是同一条命令、同样的 `--run_id` 再跑一次**（已有成绩的任务自动跳过）。结果用 `scripts/python/export_realtime_results.py` 导出成飞书总表的 16 列。

## 当前状态

- **环境**：69 个游戏 + 69 个任务配置已接入并通过验收（A 22 / B 12 / C 17 / D 18）；最终镜像 `docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2`。
- **Agent**：agent1 vanilla（单动作、无历史帧）、agent2 anticipatory（动作序列、无历史帧）、agent3 video（单动作、有历史帧）、agent4 combine（动作序列 + 历史帧）。能力矩阵、工具与时序见 [项目与实验设计](REALTIME_PROJECT_DESIGN.md)。
- **模型**：10 款模型各 4 份配置，共 40 份 YAML；坐标协议与密钥变量见 [模型配置表](configs/realtime_agents/README.md)。
- **进度与成绩**：正式全量实验由外部同学执行，逐任务成绩与逐模型金额记录在飞书总表；仓库只保留代码、配置和文档。

## 约定

- 游戏 HTML、任务配置、VM 服务（`desktop_env/server/realtime*.py`、`fmp4.py`）会影响新旧成绩可比性，改动需重新验收/重打镜像。
- `system_prompt`（反作弊与评测诚信规则）和 `user_prompt`（每个变体的任务说明，即对话第一条 user 消息）只存在于 `configs/realtime_agents/*.yaml`，代码里没有默认 prompt；任务配置里的 `instruction` 字段只是环境核心要求的占位（运行时会被覆盖成当前变体的 `user_prompt`）；改任一段 prompt 都必须换新的 `run_id`。
- 评分只取游戏写入的 `pass_at_1` / `pass_at_3`，`result.txt` = `pass_at_3`；未取得有效成绩（接口/执行/评分异常）时留空，**不记 0 分**。
- **落分前还有一道 LLM 判官**（`mm_agents/realtime_auditor.py`，prompt = `configs/realtime_agents/auditor.txt`）：判 `CHEAT` 就把最终分写成 0，`CHEAT_ATTEMPT`/`UNCERTAIN`/`NOT_CHEAT` 保留原分；判官自己没跑成则不写 `result.txt`（那条任务续跑重跑）。`REALTIME_JUDGE_API_KEY` 必填，缺了开跑前就报错。
- 密钥、截图、录像、轨迹、VM 镜像和结果目录都不入库（见 `.gitignore`）。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [`HANDOFF_CN.md`](HANDOFF_CN.md) | **执行同学作业单**：跑 69 任务 × 5 个模型并填飞书总表 |
| [`AGENTS.md`](AGENTS.md) | **AI 助手操作规程**（仓库地图、命令、硬性约定、已知坑） |
| [`SETUP_GUIDELINE_CN.md`](SETUP_GUIDELINE_CN.md) | 部署与运行：环境、镜像、命令、镜像构建、排错 |
| [`REALTIME_PROJECT_DESIGN.md`](REALTIME_PROJECT_DESIGN.md) | 项目与实验设计：环境与任务、接入与验收边界、四组 Agent、工具与时序、录像、预算、评分口径 |
| [`REALTIME_AGENT_GUIDE.md`](REALTIME_AGENT_GUIDE.md) | Agent 配置与运行：YAML 字段协议、程序流程、三种 API 协议、日志字段、轨迹查看器 |
| [`configs/realtime_agents/README.md`](configs/realtime_agents/README.md) | 模型↔协议↔密钥映射表、坐标适配与官方依据 |
| [`REALTIME_GUI_BENCH_PROTOCOL.md`](REALTIME_GUI_BENCH_PROTOCOL.md) | 游戏接口协议：`window.BENCH` 字段、状态机、写入规则、禁用快捷键、验收清单 |
