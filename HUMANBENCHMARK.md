# Human Benchmark 反应时间

在 1920×1080 Docker VM 中运行 [Reaction Time](https://humanbenchmark.com/tests/reactiontime)。每组测 3 次有效成绩，保存网站显示的毫秒数及算术平均值；抢跑单独记录。

## 运行

在 `.env` 中按 YAML 的 `api.key_env` 配置模型 key，并设置 API 地址。运行 DeepSeek TRACE：

```bash
conda activate osworld
python scripts/python/run_humanbenchmark_reactiontime.py \
  --config configs/humanbenchmark_reactiontime/combine-deepseek-flash.yaml \
  --run_id reactiontime_deepseek_r1
```

顺序运行 8 个 TRACE 模型：

```bash
python scripts/python/run_humanbenchmark_reactiontime.py \
  --plan configs/humanbenchmark_reactiontime/trace_models.json \
  --run_id reactiontime_trace_r1
```

批量命令可加 `--model deepseek-flash` 选择模型。DeepSeek 其他三组使用同目录的 `vanilla-`、`anticipatory-` 和 `video-` YAML。

模型参数和完整 prompt 在 YAML 中，要求看到绿色后再点击。

可选网络配置：`--browser-proxy <URL>` 用于 Chrome；`--api-proxy <URL>` 用于模型，填 `direct` 则直连。

## 结果

```text
results_humanbenchmark_reactiontime/<run_id>/<model>/<agent>/
  result.json          三次成绩和平均值
  agent_config.yaml    本次模型与 prompt 配置
  task_config.json     网页初始化配置
  trajectory.jsonl     模型回复、动作、收分记录
  trajectory.html      离线查看器
  recording.mp4        全程录像
```

批次汇总在 `<run_id>/summary.csv` 和 `summary.json`。重复命令跳过已完成组，另测一轮换新的 `run_id`。
