# 实时 GUI 部署与运行

## 环境

使用 Python 3.12+、Docker/KVM 和本地最终镜像 `docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2`。本机 Python 为 `/mnt/zhaorunsong/anaconda3/envs/osworld/bin/python`。从 OSWorld 仓库根目录执行命令。

最终中转站是 `https://www.packyapi.ai`。游戏网页在 VM 本地运行；模型请求需要保留已配置的 `HTTPS_PROXY` / `HTTP_PROXY`，本机 VM 地址保留在 `NO_PROXY`。本会话直连曾返回 region_restricted，代理请求通过。

新增八款模型按 YAML 的 api.key_env 选择 `PACKY_COMMON_API_KEY`、`PACKY_KIMI_API_KEY` 或 `PACKY_GLM_MINIMAX_API_KEY`，缺少对应变量会在启动 VM 前报错，不回退其他密钥。完整映射和无回显输入方式见 [模型配置说明](configs/realtime_agents/README.md)。Fable/Astra 旧配置仍使用 `PACKY_API_KEY`，或供应商变量兜底。真实密钥不写入示例、YAML 或结果。

## 启动一组实验

```bash
python scripts/python/run_multienv.py \
  --agent_variant agent4 \
  --model claude-fable-5 \
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

`agent1/2/3/4` 自动选择对应 YAML。Astra 使用 `--model gpt-6-astra` 和对应 key。三种协议均来自配置，无需另传 `--api_format`；Gemini 使用正式 Chat/high 通路。正式配置缺失或模型名与配置冲突时报错；不会换模型或协议兜底。

实时入口只接受 1920×1080 屏幕尺寸，其他宽高会在启动 VM 前报错；不会自动缩放来适配。

默认顺序运行，每次一个 VM。一个模型同一对照批次共用 run_id；结果仍按模型隔离：`<result_dir>/<model>/<run_id>/<agent>/computer_13/screenshot/realtime_gui_bench/<UUID>/`。复现实验用新的 run_id；原批次续跑只跳过已落盘结果的任务。

## 镜像与服务

录制前读取 VM 内 `realtime.py`、`fmp4.py` 哈希，与宿主机对比并记录。如果不一致，先使用 `--install_realtime_server` 安装当前服务，或重新构建镜像。只更新宿主机 Agent 不需要重打镜像。

```bash
python scripts/python/build_realtime_vm_image.py \
  --source docker_vm_data/Ubuntu-realtime-gui.qcow2 \
  --output docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2
```

输出文件已存在时默认拒绝覆盖；有意重建时才使用 `--force`。安装器只把两份服务源码上传 VM 并重启服务。构建器重启新镜像，检查源码、WAIT 实际时长及 100 动作序列，并写 `.verification.json`。原始构建源保留，废弃的 `Ubuntu-realtime-gui-fmp4.qcow2` 已移除。

## 验证和排错

- 运行 [实验设计](AGENT_EXPERIMENT_DESIGN.md) 中列出的回归测试。
- `python scripts/python/verify_realtime_runtime.py --artifacts /tmp/realtime-runtime-review` 可顺序验证两协议 × 四组的真实 VM 链路，使用模拟回复，不调用付费 API，不产生模型评分。
- 普通动作不插入 PAUSE；模型需要间隔时显式调用 WAIT。检查 `info.sequence_actions[].duration_s`，不要只看请求参数。
- 余额不足报错中的“需要预扣费额度”不是实际扣费。Gemini 输出上限为 65536，其余现用配置为 128000；完整截图历史也会影响请求额度；实际扣费看供应商账单，不擅自削减实验预算。
- HTTP/API 异常不是游戏失败。只有合法 BENCH 终态才能作为完成成绩；源码哈希匹配也不代表全部游戏都已完成模型测试。
