# 实时 GUI Agent 项目总览

本仓库基于 OSWorld，加入实时 GUI 游戏基准和四个受控的 Agent 变体。研究问题是：历史视频帧和单回合多动作是否能弥补回合制 Agent 与人类连续感知—行动循环之间的差距。

## 基准任务

当前基准包含 69 个自包含网页游戏：

| 类别 | 能力要求 | 数量 |
| --- | --- | ---: |
| A | 连续感知，不要求实时动作 | 22 |
| B | 预见性动作，不要求实时感知 | 12 |
| C | 可复现动态下的实时感知与动作耦合 | 17 |
| D | 随机动态下的实时感知与动作耦合 | 18 |

任务清单是 `evaluation_examples/test_realtime_gui_bench.json`。网页位于
`evaluation_examples/websites/realtime_gui_bench/games/`，对应的 OSWorld
任务配置位于 `evaluation_examples/examples/realtime_gui_bench/`。

每个游戏在一次环境运行中最多有三次机会。评分器读取网页的
`BENCH.passed` 和 `BENCH.attempts`：

- `pass@1`：第一次尝试成功，即 `passed == true` 且 `attempts == 0`。
- `pass@3`：三次机会内至少成功一次，即 `passed == true`。

因此，`pass@3` 不需要把同一个游戏单独运行三遍。缺失或中断的结果应记为未评分，不能转换成游戏失败。

## Agent 矩阵

四组共用截图观察、`computer_13` 动作词汇和模型接口。实验变量只有历史帧工具和单回合行动数量：

| Agent | 历史帧工具 | 每回合行动 |
| --- | --- | --- |
| Agent1 | 不开放 | 只能一个原子动作 |
| Agent2 | 不开放 | 可以提交一个动作序列 |
| Agent3 | 开放 | 只能一个原子动作 |
| Agent4 | 开放 | 可以提交一个动作序列 |

一个回合定义为“当前观测 → 思考和感知 → 行动”。Agent3/4 可以在提交行动前多次调用历史帧工具，工具调用不增加回合数。Agent2/4 的一段动作提交会连续执行多个原子动作；Agent1/3 必须拒绝动作数组。

最终接口约定是所有 Agent 都使用原生 API tool use，并用 schema 描述感知工具和行动工具。当前代码已经有历史帧协议和动作校验，但动作结果仍有一部分通过文本 JSON 解析，统一行动工具接口尚未完成迁移。

## 运行架构

- `mm_agents/realtime_protocol.py`：四组模式、动作词汇校验、历史帧 schema 和提示词生成。
- `mm_agents/realtime_agent.py`：模型 API 协议、工具调用循环、历史帧处理和日志。
- `lib_run_realtime.py`：任务生命周期、预算、录像和详细结果保存。
- `desktop_env/server/realtime.py`：虚拟机内持续录像、取帧和动作序列执行。
- `desktop_env/server/fmp4.py`：增量 fMP4 索引和历史帧解码。
- `scripts/python/run_multienv.py`：四组 Agent 的命令行入口。
- `desktop_env/evaluators/getters/realtime_gui.py`：读取网页 `BENCH` 状态。
- `desktop_env/evaluators/metrics/realtime_gui.py`：计算 `pass@1` 和 `pass@3`。

## 结果和实验状态

四组比较使用同一个 `--result_dir` 和 `--run_id`。运行器会建立：

```text
<result_dir>/<model>/<run_id>/agent1/
<result_dir>/<model>/<run_id>/agent2/
<result_dir>/<model>/<run_id>/agent3/
<result_dir>/<model>/<run_id>/agent4/
```

完整实验规模是 69 道任务 × 4 个 Agent。旧的 Sonnet 小规模试跑只是诊断批次，不能当作最终 4×4 结果。修改模型、API 协议、提示词、schema 或预算后必须使用新的 `run_id`。

69 道网页任务和评分器已经完成接入验收；四组 Agent 的基础运行器已有本地和 API 冒烟测试。这些检查证明基准可以运行和评分，不等于最终模型比较已经完成。

## 开源开发约定

1. 修改运行行为前，先阅读本文件、`AGENT_EXPERIMENT_DESIGN.md` 和相关测试。
2. 任务网页、OSWorld 配置、评分器、Agent 和 VM 服务尽量分开修改。
3. `.env` 只用于本地凭据；不要提交 API 密钥、VM 镜像、录像、缓存或私有结果。
4. 修改动作 schema、回合定义、时间控制、评分或结果目录时，先更新文档，再启动新实验。
