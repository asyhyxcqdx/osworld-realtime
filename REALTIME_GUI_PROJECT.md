# 实时 GUI Agent 项目总览

当前环境：RealtimeGame v1.1(3) 的 69 道游戏与最终 realtime VM 镜像。当前实验使用 Packy 八模型，另保留 Fable 5、Astra 的历史基线配置；详见 [模型配置表](configs/realtime_agents/README.md)。截图与 VM 执行坐标统一为原生 1920×1080，图片不预缩放。Gemini、MiniMax 的模型层按各自配置输出相对坐标并固定转换，其余模型保持原生坐标。

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

新增八模型分别拥有四份 YAML；加上两款原有基线，共 40 份配置。找不到指定模型配置或 `--model` 与配置不一致时直接报错，不回退其他模型。完整模型历史和截图保留；历史帧每次查询 1–8 个时间点，默认查询次数不限。历史查询可以重复，但必须先获得结果，再在独立响应中提交动作。Agent3 每次模型回复只有一个工具调用（一个 get_frames 或一个动作）；Agent4 可以同一回复提交多个查询。每个 get_frames 的 1–8 张帧上限相同。

所有组使用同一套原生工具和 VM 执行器。单动作也是长度为 1 的序列；多动作只在整段完成后返回当前截图。WAIT 在 VM 中按 `duration_s` 执行；推理期间游戏与录像持续进行。具体流程见 [Agent loop](REALTIME_AGENT_LOOP_GUIDE.md)。

所有模型和四组的 system prompt 均要求先观察、探索机制、谨慎试探并珍惜 attempt，尤其在最后一次尝试前获取足够信息。探索只使用各组已有工具。每个“模型 × Agent × 游戏”默认最多 100 个动作决策，游戏内三次机会共用该预算。

## 评分与验证边界

评分只读取游戏的 `window.BENCH.pass_at_1` 和 `pass_at_3`，`result.txt` 为 pass_at_3。三次机会属于同一次页面运行。正常评估结束时，提前 DONE 或达到回合上限仍未成功记有效 0 分，保留游戏真实的 ready/running 状态；API、执行或评分异常未取得有效成绩时不伪造分数。`termination_reason` 单独记录任务怎样结束。项目保存单任务结果，不再生成整体和 A/B/C/D 分类汇总。

历史基线 C1 结果：Fable5 第二次成功，Astra 第三次成功，均提交 DONE；见 [原始试跑报告](PACKY_C1_FINAL_TRIAL_REPORT.md)。既有两协议 × 四组真实 VM 核验使用固定模拟回复，共 8 组通过；不作为真实模型成绩。新增八模型已有接口短测与旧版 C1 试跑记录，不能重记为当前 prompt 和坐标配置的实验成绩。

接下来运行 combine × 八模型 × 69 任务 × 一次，共 552 次独立任务实验；四组配置均保留供后续对照。Gemini/MiniMax 当前相对坐标配置的真实 C1、新 prompt 下的统一能力实验仍需实跑。不能把已有 C1 成功概括为全部 C 类或全部任务都可解。

批量任务异常后继续下一个任务；同一 run_id 续跑时跳过已有 result.txt 的任务，清空无 result.txt 的旧任务记录后重跑。初始化早期错误可能仅有运行日志。以上行为按当前实验要求保留，详见 [运行说明](SETUP_GUIDELINE_CN.md)。实际美元账单核对与结果总表统计在项目外完成。

## 文档入口

- [部署和运行](SETUP_GUIDELINE_CN.md)
- [动作、录像与实验设计](AGENT_EXPERIMENT_DESIGN.md)
- [配置字段](REALTIME_AGENT_CONFIG_PROTOCOL.md)
- [环境接入与证据](evaluation_examples/REALTIME_GUI_BENCH_INTEGRATION.md)
- [BENCH 协议](REALTIME_GUI_BENCH_PROTOCOL.md)
- [本次 review](REALTIME_REVIEW_REPORT.md)

API 密钥从进程环境读取。游戏不需要联网；模型请求通过已配置的 HTTPS 代理访问 Packy。仓库不保存密钥、VM 镜像或录像，旧原始轨迹保留在本地结果目录。淘汰的配置、讨论稿可从 Git 历史追溯。
