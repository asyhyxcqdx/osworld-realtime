# 实时 GUI Agent 项目总览

当前基线：RealtimeGame v1.1(3) 的 69 道游戏，最终 realtime VM 镜像，Packy 代理上的 `claude-fable-5` 和 `gpt-6-astra`。截图和动作统一使用原生 1920×1080，不预缩放图片、不自动转换坐标。

## 环境与任务

| 类别 | 数量 | 研究内容 |
| --- | ---: | --- |
| A | 22 | 连续感知 |
| B | 12 | 预见性动作 |
| C | 17 | 可复现动态下的感知与动作 |
| D | 18 | 随机动态下的感知与动作 |

运行网页目录只保留 69 个 `index.html`；69 个任务 JSON 与 `evaluation_examples/test_realtime_gui_bench.json` 是必要接入文件。最新 ZIP 的 HTML 与运行树逐字节一致。来源和验收边界见 [接入记录](evaluation_examples/REALTIME_GUI_BENCH_INTEGRATION.md)。

使用 `docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2`。VM 内运行 `desktop_env/server/realtime.py` 和 `fmp4.py`，宿主机运行 Agent、API 适配器和 runner。修改 Agent 不需要重新打包镜像；修改 VM 服务后需要重新安装或构建镜像。录制前核对两份服务源码哈希，防止旧镜像悄悄继续运行。

## 四组 Agent

| 变体 | 配置 ID | 历史帧 | 每次决策的动作数 |
| --- | --- | --- | --- |
| agent1 | vanilla | 无 | 1 |
| agent2 | anticipatory | 无 | 1–100 |
| agent3 | video | 有 | 1 |
| agent4 | combine | 有 | 1–100 |

两模型分别拥有四份正式 YAML，共 8 份配置。找不到指定模型配置或 `--model` 与配置不一致时直接报错，不回退其他模型。完整模型历史和截图保留；历史帧每次查询 1–8 个时间点，默认查询次数不限。历史查询可以重复，但必须先获得结果，再在独立响应中提交动作。

所有组使用同一套原生工具和 VM 执行器。单动作也是长度为 1 的序列；多动作只在整段完成后返回当前截图。WAIT 在 VM 中按 `duration_s` 执行；推理期间游戏与录像持续进行。具体流程见 [Agent loop](REALTIME_AGENT_LOOP_GUIDE.md)。

## 评分与验证边界

评分只读取游戏的 `window.BENCH.pass_at_1` 和 `pass_at_3`。三次机会属于同一次页面运行，不是三次独立实验。`passed`/`failed` 是可评分终态；当前汇总将 ready/running 或接口中断单列为未评分，不伪造失败终态。

已确认的真实模型 C1 结果：Fable5 第二次成功，Astra 第三次成功，均提交 DONE；见 [原始试跑报告](PACKY_C1_FINAL_TRIAL_REPORT.md)。本次整理另以固定模拟回复验证两协议的四组 Agent 与真实 VM，共 8 组通过；这不是 8 条模型成绩。

仍未完成：69 道 × 4 Agent × 2 模型的大规模正式实验和成功率统计。不能把 C1 的两次通过概括为全部 C 类或全部任务都可解。

## 文档入口

- [部署和运行](SETUP_GUIDELINE_CN.md)
- [动作、录像与实验设计](AGENT_EXPERIMENT_DESIGN.md)
- [配置字段](REALTIME_AGENT_CONFIG_PROTOCOL.md)
- [环境接入与证据](evaluation_examples/REALTIME_GUI_BENCH_INTEGRATION.md)
- [BENCH 协议](REALTIME_GUI_BENCH_PROTOCOL.md)
- [本次 review](REALTIME_REVIEW_REPORT.md)

API 密钥从进程环境读取。游戏不需要联网；模型请求通过已配置的 HTTPS 代理访问 Packy。仓库不保存密钥、VM 镜像或录像，旧原始轨迹保留在本地结果目录。淘汰的配置、讨论稿可从 Git 历史追溯。
