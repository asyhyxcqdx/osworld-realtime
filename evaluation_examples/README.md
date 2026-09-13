# 评测任务说明

本目录存放 OSWorld 的任务配置和基准清单。每个任务通常包含唯一 ID、初始环境、自然语言指令、初始化配置、相关应用和评分器。

## 实时 GUI 基准

本项目的正式实时任务清单是：

```text
evaluation_examples/test_realtime_gui_bench.json
```

对应文件位于：

- 任务配置：`evaluation_examples/examples/realtime_gui_bench/`
- 游戏网页：`evaluation_examples/websites/realtime_gui_bench/games/`
- 评分读取：`desktop_env/evaluators/getters/realtime_gui.py`
- 评分指标：`desktop_env/evaluators/metrics/realtime_gui.py`

实时基准共有 69 道题，分为 A/B/C/D 四类。完整接入记录见
`evaluation_examples/REALTIME_GUI_BENCH_INTEGRATION.md`。

不要把历史清单、浏览器验收脚本或模型结果目录当作正式任务定义。新增或修改任务后，应同步检查清单、配置、网页路径和评分测试。
