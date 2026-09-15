# Realtime GUI 项目入口

项目基于 OSWorld，当前接入最终 RealtimeGame v1.1(3) 的 69 道网页游戏和四类 Agent。

- 新增八款模型各四组配置，保留 `claude-fable-5`、`gpt-6-astra` 基线；使用 Packy 和系统 HTTPS 代理。
- 所有模型接收原始 1920×1080 截图，返回坐标原样执行。
- 最终镜像：`docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2`，已验证源码和实际 WAIT。
- Fable5、Astra 的 combine 均已实际通过 C1；新增模型配置与接口验证不等同于完整游戏成绩，完整模型矩阵尚未运行。

| Agent | 历史录像 | 一次提交动作 |
| --- | --- | --- |
| agent1 / vanilla | 无 | 1 |
| agent2 / anticipatory | 无 | 1–100 |
| agent3 / video | 有 | 1 |
| agent4 / combine | 有 | 1–100 |

Agent3 每次模型回复最多一个工具调用（get_frames 或动作）；取帧结果返回后可继续查询，单次 get_frames 仍可取 1–8 张帧。Agent4 可以同一回复提交多个查询。

从 [项目总览](REALTIME_GUI_PROJECT.md) 和 [运行说明](SETUP_GUIDELINE_CN.md) 开始阅读。实现细节见 [配置协议](REALTIME_AGENT_CONFIG_PROTOCOL.md)、[Agent loop](REALTIME_AGENT_LOOP_GUIDE.md) 和 [实验设计](AGENT_EXPERIMENT_DESIGN.md)。

环境目录只含 69 个 HTML；任务 JSON、任务清单、评分器是必要运行文件。交付方自测与 OSWorld 独立验收分别记录于 [接入记录](evaluation_examples/REALTIME_GUI_BENCH_INTEGRATION.md)。

开发和运行命令在仓库根目录执行。密钥、截图、录像、缓存、VM 镜像和私有实验输出不提交到 Git。本次核查和清理结果见 [review 报告](REALTIME_REVIEW_REPORT.md)。

新增八款 Packy 模型的四类 Agent 配置、Gemini Chat/high 通路和三组密钥启动方式见 [模型配置目录](configs/realtime_agents/README.md)。Gemini 输出上限 65536；MiniMax M3 只开 adaptive thinking，不设 high。
