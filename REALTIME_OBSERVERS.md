# TRACE 后台观察器

Agent4（TRACE）有两种独立观察器模式：

| 模式 | `create_observer` 参数 | 工作方式 |
| --- | --- | --- |
| Jev | `name`、`cases` | Jev 根据当前截图选择视觉条件，运行器执行对应动作。 |
| Python | `name`、`code` | 模型编写完整脚本，在游戏 VM 中观察和操作。 |

两组都用 `stop_observer(name)` 停止。主模型思考或 WAIT 时，观察器继续运行。启动错误直接返回，后台错误随下一次反馈返回；停止时释放按键和鼠标，任务结束时停止全部观察器。

## 运行 D 类 18 题

配置主模型和审计 API。Jev 另需 `JEV_API_KEY`，服务地址通过 `JEV_BASE_URL` 设置；其请求使用 TypeSafe choice 格式。

```bash
conda activate osworld

python scripts/python/run_realtime_batch.py \
  --agent_variant agent4 --models deepseek-flash --observer_mode jev \
  --meta evaluation_examples/test_realtime_gui_bench_d.json \
  --run_id deepseek_trace_jev_D_r1 --num_envs 12 \
  --exclusive-keys-confirmed

python scripts/python/run_realtime_batch.py \
  --agent_variant agent4 --models deepseek-flash --observer_mode script \
  --meta evaluation_examples/test_realtime_gui_bench_d.json \
  --run_id deepseek_trace_script_D_r1 --num_envs 12 \
  --exclusive-keys-confirmed
```

配置和完整 prompt 在 `configs/realtime_agents/observers/jev/`、`script/`。不同模式和轮次使用独立 `run_id`。普通 TRACE 使用 `--observer_mode none`。

## 工具

**Jev：** `cases` 将视觉条件说明映射到动作列表。Jev 根据当前截图选择条件；返回 `NONE` 或选中空列表时不操作。每轮匹配都会执行对应动作，参数和坐标沿用主模型的 GUI 工具约定。

**Python：** `code` 是完整脚本，使用 PyAutoGUI 截图和操作，可导入 Pillow、NumPy。判断、动作、循环和退出条件由模型编写；坐标为 1920×1080 原生像素，原点在左上角。

## 记录与审计

在主实验的任务结果目录中新增：

```text
observer_summary.json               各观察器最终状态
observers/<id>/cases.json           Jev 条件和动作
observers/<id>/code.py               模型提交的 Python
observers/<id>/*.png                 观察器截图
observers/<id>/stdout.log、stderr.log
```

轨迹记录后台事件和 PyAutoGUI 操作，审计使用对应模式的 prompt。续跑跳过已完成任务，待审计的任务只补审计。输入过长时可去掉 JSON 排版空格后重审：

```bash
python -m mm_agents.realtime_auditor <任务目录> --compact-json
```
