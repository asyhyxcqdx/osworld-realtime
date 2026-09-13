# 中文部署与运行说明

这份说明只覆盖当前实时 GUI 项目。OSWorld 的 Google OAuth、代理和公共评测平台等通用内容仍保留在英文上游文档 `SETUP_GUIDELINE.md` 中。

## 环境要求

- 当前项目元数据要求 Python 3.12 或更高版本。
- 运行实时网页游戏需要 Docker，并建议主机支持 KVM。
- 模型 API 凭据通过环境变量提供，例如 `PACKY_API_KEY`、`ANTHROPIC_API_KEY` 或 `OPENAI_API_KEY`。
- VM 镜像位于本地 `docker_vm_data/`，不提交到 Git。

实时游戏全部在本地网页中运行，通常不需要 Google 账号或外网代理。其他 OSWorld 任务是否需要这些配置，要看具体任务。

## 先做最小检查

在仓库根目录执行：

```bash
python -m pytest -q tests/test_realtime_gui_bench_manifest.py
```

该测试确认正式清单、69 个任务配置和 69 个游戏网页数量一致。修改任务接入、评分或 Agent 后，再运行 `AGENT_EXPERIMENT_DESIGN.md` 中列出的实时回归测试。

## 运行一组 Agent

四组 Agent 都使用 `scripts/python/run_multienv.py`。四次启动共享同一个 `--result_dir` 和 `--run_id`，只替换 `--agent_variant`：

```bash
python scripts/python/run_multienv.py \
  --agent_variant agent1 \
  --run_id exp001 \
  --action_space computer_13 \
  --observation_type screenshot \
  --provider_name docker \
  --path_to_vm docker_vm_data/Ubuntu-realtime-gui.qcow2 \
  --headless \
  --model YOUR_MODEL \
  --api_format anthropic_messages \
  --api_base_url YOUR_API_BASE_URL \
  --test_all_meta_path evaluation_examples/test_realtime_gui_bench.json \
  --max_steps 15 \
  --sleep_after_execution 0 \
  --num_envs 1 \
  --install_realtime_server \
  --client_password "$OSWORLD_VM_PASSWORD" \
  --result_dir results_four_agents
```

把 `agent1` 换成 `agent2`、`agent3` 或 `agent4`。完整参数含义、录像服务安装和断点续跑规则见 `AGENT_EXPERIMENT_DESIGN.md`。

## 结果位置

运行器会按模型、批次和 Agent 隔离结果：

```text
results_four_agents/<model>/<run_id>/agent1/
results_four_agents/<model>/<run_id>/agent2/
results_four_agents/<model>/<run_id>/agent3/
results_four_agents/<model>/<run_id>/agent4/
```

修改模型、API 协议、提示词、schema 或预算时必须新建 `run_id`。不要把未完成任务、接口错误或中断运行当作游戏失败。

## 常见问题

- **VM 服务不认识实时接口**：保留 `--install_realtime_server`，它会在任务 reset 后安装录像和历史帧服务。
- **结果目录混在一起**：检查四次启动是否使用相同的 `--run_id`，不同实验是否使用了不同编号。
- **模型输出无法执行**：先检查 action tool-use schema 和 Agent1/3 的单动作限制，不要直接放宽解析器。
- **录像或结果很大**：结果、日志、缓存和 VM 镜像是本地产物，不应提交到 Git。
