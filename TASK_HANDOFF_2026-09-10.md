# 四组 Agent 任务交接（2026-09-10）

从原任务「梳理4个Agent方案」（`01a081d2-3b05-7801-a1cc-bc19ed66e2be`）接到当前对话。本文根据原始会话、当前代码和磁盘结果整理；优先保留用户最后确认的要求，不沿用讨论中已被纠正的说法。

后续更新：用户随后要求不限取帧查询次数，并补充模型思考复盘日志；新代码和 prompt 说明见 [PROMPTS_AND_LOGGING_2026-09-10.md](/mnt/zhaorunsong/yhyx/OSWorld/validation/realtime_agents/PROMPTS_AND_LOGGING_2026-09-10.md)。下文的查询预算 4 是交接时及旧试跑的配置快照。

## 对话恢复情况

- 应用任务读取接口返回 `systemError`，可见记录停在“模型优先保存结果、解释临时安装实时服务”的中断回合。
- 本地原始会话仍有后续记录，最后用户消息为北京时间 **2026-09-10 13:57:06**：“那就是没啥问题了呗 之前是模型自己输出有问题呗”。13:36:36 也有相同追问；这两条在原记录中没有最终答复。
- 已导出末尾可见回合及其后的 **53 条用户消息/助手最终答复**，密钥已隐去：[后段对话恢复记录](/mnt/zhaorunsong/yhyx/OSWorld/validation/realtime_agents/recovered_conversation_20260910.md)。这份导出保留历史错误和后续纠正；当前结论以本文为准。
- 原始日志位置：`/mnt/zhaorunsong/.codex/sessions/2026/09/09/rollout-2026-09-09T00-20-33-01a081d2-3b05-7801-a1cc-bc19ed66e2be.jsonl`。原日志和应用数据未修改；本次只完成恢复、核对和记录，没有修复应用内的消息展示。

## 项目目标和最终约定

在已有 OSWorld 游戏环境上，用同一个模型做四组能力对比，共用实现。

| Agent | 多动作序列 | 主动查询历史画面 |
| --- | --- | --- |
| Agent1 | 否 | 否 |
| Agent2 | 是 | 否 |
| Agent3 | 否 | 是，`get_frames` |
| Agent4 | 是 | 是，`get_frames` |

- 保持已有 `computer_13` 动作空间，不新增 `WAIT.duration_s`、`delay_after_s` 或固定序列间隔。
- 单动作走 `env.step()`；序列整组传给 VM 连续执行，中间不逐动作截图，结束后取得最终观测。
- `pause` 来自 `--sleep_after_execution`，是运行器参数。单动作保留旧行为：普通动作后等待一次，旧 `WAIT` 等两次；序列中的 `WAIT` 只等一次。当前试跑 `pause=0`。
- `MOVE_TO` 保持原有随机 0.5～1 秒耗时，`DRAG_TO` 保持 1 秒；不针对某一组擅改。
- 四组统一持续录制 **30 FPS、1920×1080 fMP4**，目标约 100 ms 封装片段。100 ms 不是截图间隔，也不是最大读取延迟保证。
- reset、页面配置、60 秒准备等待之后开始录像；**第一张实际录制帧**定义任务时间 0。初始模型截图通常稍晚，按实际时间记录。早期“第一张模型截图就是 0 秒”的说法已被这个实现替代。
- `get_frames(times_s=[...])` 每次查询 1～8 个有限、非负时间点；返回最近的已采集帧及实际时间。单个时间点尚不可读只使该项成为 `not_ready`，不阻止其他可读图片返回。
- 只读已完整写入的 fMP4 片段；任务中保留历史录像。历史图片由模型主动查询，不自动推送整段视频、不把所有后台采集帧塞进下一轮。
- 游戏在模型推理和取帧期间继续运行。默认每次动作决策最多查询 4 次；一次序列最多 16 个动作，且受剩余动作预算限制。
- `max_steps` 按原子动作计数（含 `WAIT`/`DONE`/`FAIL`）。模型请求数、工具调用数、取帧数、图片数、动作数和动作决策数分别记录。
- 格式错误最多两次纠正；无合法动作时不执行。没有“只回复我再想想就无限追加推理”的机制。

## 最后讨论的 action / tool 问题

当前代码核对位置：[realtime_protocol.py](/mnt/zhaorunsong/yhyx/OSWorld/mm_agents/realtime_protocol.py:37)、[load_output](/mnt/zhaorunsong/yhyx/OSWorld/mm_agents/realtime_protocol.py:50)、[realtime_agent.py](/mnt/zhaorunsong/yhyx/OSWorld/mm_agents/realtime_agent.py:387)。

1. 仅 Agent3/4 注册 `get_frames` 工具。键鼠动作通过普通回复里的动作 JSON 输出，例如 `{"action_type":"CLICK","parameters":{"x":500,"y":300}}`。
2. 当前解析器支持纯 JSON，也支持“解释文字 + 唯一一个 JSON 代码块”。有代码块时提取块内内容执行，外面的解释不作为动作执行。因此当前提示词允许先解释是有对应解析逻辑的，**不需要为了这一点删掉解释或修改代码**。
3. “解释文字 + 没有代码块包裹的裸 JSON”不能整体作为合法 JSON 解析；多个独立代码块也会被拒绝。序列应放在同一个 JSON 数组中。
4. 当前提示词要求一次回复选择查询工具或提交动作。一次动作决策可以经历多次模型请求：查图 → 返回图片 → 再请求模型 → 最终动作；不应混淆模型内部推理、API 请求次数和实际动作次数。
5. 原试跑收到过 `tool_use(name="CLICK")`。`CLICK` 没有被注册成工具，这个响应违反当前约定；当前程序返回未知工具错误，不会自动把它变成点击。
6. 用户明确要求：“别乱改啊 有些是他自己问题我们就不需要改”。原助手曾临时加入容错转换，之后按要求停止实验并撤回；当前代码仍只接受 `get_frames` 工具。
7. 对最后追问的准确答复：**收到的接口输出确实有格式问题；“先解释再给动作代码块”本身没有问题。但仅凭现有记录不能断定错误一定由底层模型产生，也不能保证整个项目再无问题，中转站转发/转换仍是未排除因素。**

## 已完成的环境与实现

- 当前游戏任务集为 **69 题**：A 22、B 12、C 17、D 18；不是早期 107 题。A/B/C/D 是任务类别，不能当成严格难度排序。
- 游戏接入说明：[REALTIME_GUI_BENCH_INTEGRATION.md](/mnt/zhaorunsong/yhyx/OSWorld/evaluation_examples/REALTIME_GUI_BENCH_INTEGRATION.md)。该文件里早期结果目录说明由后来的四组设计文档替代。
- D1 的分类已确认；仅按已授权要求移除底部重复次数小字，勿重新改变网页玩法或提示。
- 游戏/评分的脚本验收与模型正式成绩分开。`result.txt` 为 `pass_at_3`；详细 `result.json` 另有 `pass_at_1`。没有结果文件不等于 0 分。
- 四组运行器、工具回传、fMP4 边录边查、动作序列、时间记录和结果分层已实现。
- 结果层级为 `result_dir/<model>/<run_id>/<agent>/computer_13/screenshot/<domain>/<uuid>/`。四组同一批用同一 `run_id`；真实启动、续跑和任务时间另记，编号不代表同时开始。
- 新批次换 `run_id`；原批次续跑沿用模型、配置、编号和 Agent，跳过有成绩的任务。批次/各组各有 `launches.jsonl`，汇总在各 Agent 自己的 `summary/`。
- 已制作固定镜像：[Ubuntu-realtime-gui-fmp4.qcow2](/mnt/zhaorunsong/yhyx/OSWorld/docker_vm_data/Ubuntu-realtime-gui-fmp4.qcow2)。当前文件存在，大小 24,493,359,104 字节；原始镜像另行保留。
- [fixed_image.json](/mnt/zhaorunsong/yhyx/OSWorld/validation/realtime_agents/fixed_image.json) 记录全新 VM 不用安装参数即返回 `fmp4=true`、`sequence=true`。跑实验直接使用新镜像，可省略 `--install_realtime_server`。镜像制作脚本只在制作/更新镜像时使用。
- 旧镜像可通过 `--install_realtime_server` 在每次 reset 后临时安装扩展；这不会自动生成新版发布镜像。
- 开发阶段历史记录：最初 46 项检查通过，后续目录调整检查 25 项，最后相关组合检查报告 **57 项通过**；这些检查有重叠，不相加。[VALIDATION.md](/mnt/zhaorunsong/yhyx/OSWorld/validation/realtime_agents/VALIDATION.md) 尚主要保留早期分阶段数字。
- 历史真实联调包含 Claude Sonnet 4.6、四组接口和 fMP4 时间对齐。GPT Chat/Responses 只有协议层测试，不能据此认定 Packy 实际 GPT 通道可用。
- 本次交接未重新跑测试、启动 VM 或调用付费模型；检查结论来自代码、原记录和现存产物。

## 最新 Sonnet 5 小规模实验

用户已选用 Packy 中转模型标识 `claude-sonnet-5`，授权先按 A/B/C/D 各选一题，用四组试跑。最终清单为 **A31、B1、C36、D1 × 四组 = 16 个任务**。这是项目记录中的中转模型标识，不据此推断底层模型身份。历史价格比较不作为当前价格承诺。

批次：`sonnet5_smoke_20260910`。

结果：[本批实验目录](/mnt/zhaorunsong/yhyx/OSWorld/results_realtime_gui_sonnet5_smoke/claude-sonnet-5/sonnet5_smoke_20260910)。本次逐一读取 `result.txt`、`result.json`、`agent_metrics.json` 核实：

| Agent | A31 | B1 | C36 | D1 |
| --- | --- | --- | --- | --- |
| Agent1 | 1，首次成功 | 0，三次失败 | 0，三次失败 | 0，三次失败 |
| Agent2 | 1，首次成功 | 无分数 | 0，三次失败 | 无分数 |
| Agent3 | 无分数 | 无分数 | 无分数 | 无分数 |
| Agent4 | 无分数 | 无分数 | 无分数 | 无分数 |

- 总共只有 **6/16 个有效成绩**。Agent1 为 1/4；Agent2 已评分两题中 1/2，不能把未完成题目当失败或声称四组比较结束。
- Agent2 首次运行在 B1 准备阶段被停止；之后已经实际续跑过 B1、D1，二者各请求一次模型、执行动作数为 0，未产生评分。原日志记录接口 403 拒绝。不要继续沿用“D1 从未启动”的早期状态。
- 原对话记录 Agent3/4 取帧成功后收到非法 `CLICK` 工具调用并被停止。本次磁盘核实二者没有有效成绩；部分目录只有初始化/中断产物，存在目录不代表完成。
- 北京时间 **2026-09-10 13:11 左右最后一次接口检查**，原对话记录 Sonnet 5、Sonnet 4.6、Opus 4.8、Opus 5 都返回 503，提示 `cc-sale` 分组没有可用渠道。此前 Sonnet 曾返回 403，Opus 曾可用；以后者的最新时间点为准。这是历史检查结果，当前可用性尚未重新探测。
- 本次检查未发现仍在运行的 `run_multienv.py` 进程。

本批实际配置已从四组 `args.json` 核实：

| 参数 | 值 |
| --- | --- |
| 环境 | `osworld-yhyx`，避免误用缺依赖的默认 Python |
| 模型 / 接口 | `claude-sonnet-5` / `anthropic_messages` |
| API 根地址 | `https://www.packyapi.ai` |
| VM | `docker_vm_data/Ubuntu-realtime-gui-fmp4.qcow2` |
| 清单 | `/tmp/realtime_smoke_tasks_sonnet5.json`，本次检查仍存在 |
| 最大动作数 | 15 |
| 最大输出 token / temperature | 1500 / 1.0 |
| 历史动作决策数 | 3 |
| 取帧预算 / 序列上限 | 4 / 16 |
| pause / 每组环境数 | 0 / 1 |
| 临时安装实时服务 | false，已使用预装镜像 |

试跑清单内容（原执行顺序 C36、A31、B1、D1；若 `/tmp` 清理，可据此恢复）：

```json
{"realtime_gui_bench":["0888c45a-7722-5a31-8bfd-25881c52a501","29be19ce-4409-5615-8a49-b22552000523","2df3a2ca-63b8-5c46-bdb2-a9e57d858e8e","b0860ad0-2faf-5198-b0f7-32d0cf2cdbe2"]}
```

## 接下来承接的工作

1. 先把最后的协议疑问说明准确：解释可保留，代码块内的动作才执行；非法 `CLICK` 工具调用不自动兜底。不要无证据归咎于用户代码或底层模型。
2. 继续实验时先小请求检查原定 Sonnet 通道；恢复后按原配置、原批次补 Agent2 的 B1、D1，不重跑已有分数。
3. Agent3/4 保留错误记录。若继续验证，先小规模复现和查看实际请求/响应；遵守用户“不为错误输出乱改执行器”的要求。
4. 不擅自换更贵的 Opus、不换协议或改 Prompt 来混入本批；若模型或实验设置改变，使用新批次并清楚记录差异。
5. 16 个试跑任务尚未完成；69 题 × 四组的全量模型实验尚未完成。后续还需评估成绩、失败原因、模型/工具次数、查询延迟及录像资源占用。

工作区已有大量历史未提交改动。本次新增交接与恢复文档，未提交 Git、未覆盖现有成绩、未改动作/工具实现。
