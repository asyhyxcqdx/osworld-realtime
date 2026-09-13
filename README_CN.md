# Realtime GUI Agent 项目入口

这是一份给中文开发者的接手说明。项目基于 OSWorld，目标是研究回合制 GUI Agent 在实时 GUI 环境中的能力边界。

如果你第一次接触仓库，建议按这个顺序阅读：

1. 本文件：项目目标、目录和当前状态。
2. `REALTIME_GUI_PROJECT.md`：实时项目中文总览。
3. `AGENT_EXPERIMENT_DESIGN.md`：实时录像、历史帧、动作序列和实验参数的技术细节。
4. `evaluation_examples/REALTIME_GUI_BENCH_INTEGRATION.md`：69 道游戏的接入、验收和评分依据。
5. `evaluation_examples/README.md`、`mm_agents/README.md`、`scripts/README.md`：任务、Agent 和运行脚本说明。
6. `SETUP_GUIDELINE_CN.md`：Docker、VM、凭据和实验运行说明。
7. `REALTIME_GUI_BENCH_PROTOCOL.md`：游戏制作 Agent 必须遵守的正式状态和评分接口。

## 当前目标

项目分三步推进：

1. 将 69 道实时网页游戏接入 OSWorld，使其可以启动、操作和评分。
2. 实现四个受控的 Agent 变体。
3. 用多个商业模型 API 做统一实验，并发布可读、可复现的代码。

当前仓库已经包含 69 道任务、评分器、实时录像和四组 Agent 的基础运行器；最终的统一 action tool-use 接口和按回合计数规则仍需继续实现与验证。

## 69 道任务

正式清单是 `evaluation_examples/test_realtime_gui_bench.json`：

| 类别 | 任务要求 | 数量 |
| --- | --- | ---: |
| A | 连续感知，不要求实时动作 | 22 |
| B | 预见性动作，不要求实时感知 | 12 |
| C | 可复现动态下的实时感知与动作耦合 | 17 |
| D | 随机动态下的实时感知与动作耦合 | 18 |

网页位于 `evaluation_examples/websites/realtime_gui_bench/games/<编号>/index.html`，OSWorld 配置位于 `evaluation_examples/examples/realtime_gui_bench/<UUID>.json`。

每个游戏在一次运行中最多允许三次尝试：

- `pass@1`：第一次尝试成功。
- `pass@3`：三次机会内至少成功一次。

因此 `pass@3` 不需要把同一个游戏单独运行三遍。

## 四个 Agent

四组共用截图、`computer_13` 动作词汇和模型接口，只改变两个能力开关：历史帧查询和单回合动作数量。

| Agent | 历史帧 `get_frames` | 单回合行动 |
| --- | --- | --- |
| Agent1 | 不开放 | 只能一个原子动作 |
| Agent2 | 不开放 | 可以提交动作序列 |
| Agent3 | 开放 | 只能一个原子动作 |
| Agent4 | 开放 | 可以提交动作序列 |

一次回合是“当前观测 → 思考和感知 → 行动”。Agent3/4 可以在提交行动前多次调用历史帧工具；这些调用不增加回合数。Agent1/3 的行动工具必须拒绝动作数组。

最终接口约定是：四组都使用原生 API tool use，并用 schema 描述感知工具和行动工具。正式配置不接受文本 JSON 作为动作输出；旧 JSON 模式只为兼容历史测试保留。

## 代码地图

- `REALTIME_AGENT_CONFIG_PROTOCOL.md`：四个 Agent 配置文件、能力边界和内嵌提示词协议。
- `mm_agents/realtime_protocol.py`：动作校验、历史帧 schema 和运行时协议。
- `mm_agents/realtime_agent.py`：模型 API 协议、工具调用循环和响应日志。
- `REALTIME_AGENT_LOOP_GUIDE.md`：Agent loop 的中文逐步导读和消息流说明。
- `lib_run_realtime.py`：任务生命周期、预算、录像和结果保存。
- `desktop_env/server/realtime.py`：虚拟机内录像、取帧和动作序列执行。
- `desktop_env/server/fmp4.py`：实时 fMP4 索引和历史帧解码。
- `desktop_env/evaluators/getters/realtime_gui.py`：读取网页 `BENCH` 状态。
- `desktop_env/evaluators/metrics/realtime_gui.py`：计算 `pass@1`/`pass@3`。
- `scripts/python/run_multienv.py`：四组 Agent 的命令行入口。
- `tests/test_realtime_*.py`：实时 Agent、评分、结果目录和录像相关测试。

## 开发前验证

推荐使用 Conda 环境 `osworld`（Python 3.12 或更高版本）。当前机器上的标准环境是 `/mnt/zhaorunsong/anaconda3/envs/osworld`；先执行 `conda activate osworld`。项目元数据要求 Python 3.12 或更高版本。

先运行最小任务清单检查：

```bash
python -m pytest -q tests/test_realtime_gui_bench_manifest.py
```

完整实时回归测试见 `AGENT_EXPERIMENT_DESIGN.md` 的“验证”部分。真实 VM 测试需要 Docker/KVM 或其他 OSWorld provider，不应把浏览器验收脚本的结果当成模型成绩。

## 实验运行约定

四组比较使用同一个 `--result_dir` 和 `--run_id`，只替换 `--agent_variant agent1|agent2|agent3|agent4`。结果目录由运行器建立为：

```text
<result_dir>/<model>/<run_id>/agent1/
<result_dir>/<model>/<run_id>/agent2/
<result_dir>/<model>/<run_id>/agent3/
<result_dir>/<model>/<run_id>/agent4/
```

修改模型、API 协议、提示词、schema 或预算后必须使用新的 `run_id`。模型密钥从环境变量读取，不写入代码或命令历史。

## 当前未完成事项

1. 审计并冻结 `computer_13` 的实时动作语义，尤其是持续时间、动作间隔、坐标系和 `WAIT` 行为。
2. 将 69 个游戏逐一迁移到正式 `REALTIME_GUI_BENCH_PROTOCOL.md`，并运行接口合规检查。
3. 完成真实 VM 单题验证，确认 native tool call、动作执行回传、录像取帧和 `trajectory.jsonl` 完整闭环。
4. 在固定配置下完成 69 道 × 4 组模型实验，并分别汇总 A/B/C/D 的 `pass@1` 和 `pass@3`。
5. 接入并验证多个商业 API，统一记录原始响应、thinking summary 和 input/output token usage。

当前 Agent loop、四份 YAML 配置和配置加载入口已经完成；扩大全量付费实验前，先完成上面的动作空间和单题 VM 验证。
