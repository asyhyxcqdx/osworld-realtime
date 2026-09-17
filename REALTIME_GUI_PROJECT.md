# 实时 GUI Agent 项目总览

当前环境是 RealtimeGame v1.1(3) 的 69 道游戏与最终 realtime VM 镜像；字段定义、状态机与验收清单见 [BENCH 协议](REALTIME_GUI_BENCH_PROTOCOL.md)。

截图与 VM 执行坐标统一为原生 1920×1080，图片不预缩放。Gemini、MiniMax 的模型层按各自配置输出相对坐标、由代码固定转换，其余模型保持原生坐标；协议、依据与上游对照见 [模型配置表](configs/realtime_agents/README.md)。

## 环境与任务

| 类别 | 数量 | 研究内容 |
| --- | ---: | --- |
| A | 22 | 连续感知 |
| B | 12 | 预见性动作 |
| C | 17 | 可复现动态下的感知与动作 |
| D | 18 | 随机动态下的感知与动作 |

运行网页目录只保留 69 个 `index.html`；69 个任务 JSON 与 `evaluation_examples/test_realtime_gui_bench.json` 是必要接入文件。包的来源、逐字节一致性与镜像哈希见 [接入记录](evaluation_examples/REALTIME_GUI_BENCH_INTEGRATION.md)。

VM 内运行 `desktop_env/server/realtime.py` 和 `fmp4.py`，宿主机运行 Agent、API 适配器和 runner。改 Agent 不需要重打镜像；改 VM 服务需要重新安装或构建，并在录制前核对两份服务源码哈希，防止旧镜像继续运行。

## 四组 Agent

| 变体 | 配置 ID | 历史帧 | 每次决策的动作数 |
| --- | --- | --- | --- |
| agent1 | vanilla | 无 | 1 |
| agent2 | anticipatory | 无 | 1–100 |
| agent3 | video | 有 | 1 |
| agent4 | combine | 有 | 1–100 |

8 款实验模型各有四份 YAML，加上 Fable 5、Astra 两款历史基线共 40 份。指定配置缺失或 `--model` 与配置不一致时直接报错，不回退其他模型。

四组共享同一套原生工具和 VM 执行器，区别只有历史帧是否开放、一次决策可提交 1 个还是 1–100 个动作。单动作也是长度为 1 的序列；多动作在整段完成后才返回当前截图。历史帧每次查询 1–8 个时间点，查询次数默认不限，必须先拿到结果、再在独立响应里提交动作（Agent3 每次回复只能有一个工具调用，Agent4 可在同一回复提交多个查询）。受控变量、工具与时序、录像与预算见 [实验设计](AGENT_EXPERIMENT_DESIGN.md)，逐步骤流程见 [Agent loop](REALTIME_AGENT_LOOP_GUIDE.md)。

## 评分与验证边界

评分只读取游戏的 `window.BENCH.pass_at_1` 和 `pass_at_3`，`result.txt` 为 `pass_at_3`，游戏内三次机会属于同一次页面运行。正常评估结束时，提前 DONE 或达到回合上限仍未成功记**有效 0 分**，保留游戏真实的 ready/running 状态；API、执行或评分异常**未取得有效成绩时不伪造分数**。任务怎样结束由 `termination_reason` 单独记录。项目只保存单任务结果，不生成整体或 A/B/C/D 分类汇总。

C1 历史基线与接口检查结果、各自的边界说明见 [接入记录](evaluation_examples/REALTIME_GUI_BENCH_INTEGRATION.md)；批量实验的执行方式见 [执行同学作业单](HANDOFF_CN.md)。**不能把一次 C1 成功概括为全部任务可解**，接口短测也不等于游戏成绩。

API 密钥从进程环境（或已被忽略的 `.env`）读取；游戏不需要联网。仓库不保存密钥、VM 镜像、录像或结果目录，淘汰的配置与讨论稿从 Git 历史追溯。文档索引见 [README_CN.md](README_CN.md) 与 [AGENTS.md](AGENTS.md) 第 9 节。
