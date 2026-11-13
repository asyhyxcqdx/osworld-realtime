# Realtime GUI 项目入口

基于 OSWorld 的实时 GUI Agent 基准：69 个网页游戏（A 22 / B 12 / C 17 / D 18），游戏在模型思考期间继续运行。

四组 Agent：agent1 单动作；agent2 动作序列；agent3 单动作与历史帧；agent4（TRACE）动作序列与历史帧。配置和 prompt 在 `configs/realtime_agents/`。

## 快速开始

```bash
uv sync
source .venv/bin/activate
uv pip install imageio-ffmpeg
docker pull happysixd/osworld-docker
hf download bright-star123/osworld-realtime-vm --repo-type dataset \
  Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2 --local-dir docker_vm_data
cp .env.example .env                     # 填 API 地址与 key
```

在 `.env` 中填写模型和审计 API 配置，然后运行 DeepSeek TRACE 全部 69 题，12 并发：

```bash
python scripts/python/run_realtime_batch.py \
  --agent_variant agent4 --models deepseek-flash \
  --run_id deepseek_trace_r1 --result_dir results_realtime_batches \
  --num_envs 12 --exclusive-keys-confirmed
```

修改 `--agent_variant` 选择 agent1–agent4，`--models` 选择模型；`--task <uuid>` 选择单题，`--meta <json>` 选择任务清单。新一轮实验使用新的 `run_id`。

## 补充实验

- [Human Benchmark 反应时间](HUMANBENCHMARK.md)：每组 3 次有效测试，保存逐次成绩和平均值。
- [TRACE 后台观察器](REALTIME_OBSERVERS.md)：Jev 和 Python 两种独立模式，在 D 类 18 题上运行。

## 结果与续跑

单题目录：`<result_dir>/<model>/<run_id>/<agent>/computer_13/screenshot/realtime_gui_bench/<uuid>/`。

`game_result.json` 保存游戏原分，`result.json` 保存详情和审计状态，`result.txt` 保存审计后的最终分。`trajectory.html` 可离线查看，原始记录在 `trajectory.jsonl`，录像为 `recording.mp4`。

使用同一命令续跑：已有最终分的任务跳过，已完成游戏但审计失败的任务只补审计。审计判为 `CHEAT` 时最终分为 0，其余保留游戏原分。

结果导出使用 `scripts/python/export_realtime_results.py`，详细命令见 [批量运行](HANDOFF_CN.md)。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [批量运行](HANDOFF_CN.md) | 运行、汇总与导出 |
| [部署说明](SETUP_GUIDELINE_CN.md) | 环境、镜像与排错 |
| [实验设计](REALTIME_PROJECT_DESIGN.md) | 任务、Agent 与评分 |
| [Agent 指南](REALTIME_AGENT_GUIDE.md) | 配置、协议与日志 |
| [模型配置](configs/realtime_agents/README.md) | 模型、请求格式与坐标 |
| [游戏协议](REALTIME_GUI_BENCH_PROTOCOL.md) | `window.BENCH` 接口 |
| [维护说明](AGENTS.md) | 仓库结构与开发约定 |
