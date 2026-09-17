# Realtime GUI 项目复核记录

## 当前复核（2026-09-17）

当前环境是 RealtimeGame v1.1(3) 的 69 个原始 HTML 与终版镜像；八款实验模型各四组配置齐全，另保留 Fable/Astra 八份历史配置。Gemini/MiniMax 的坐标 prompt、工具字段及转换器按配置配套，其余模型保持原生坐标。所有模型原样接收 1920×1080 PNG。

上一轮提交（`72f1830b`）包含此前已确认的模型坐标适配、三位小数展示、100 回合默认预算、公共探索 prompt、Chrome 更新提示启动参数，以及另一线程的单任务结束状态/移除类别汇总改动。补齐活动文档的模型数量、实验范围和评分口径，并修正 HTML 浏览器测试点击后未等待异步内容出现的问题。

批量实验行为已确认保留：单任务异常后继续下一任务；同一 run_id 续跑清空无 result.txt 的旧任务记录后重跑，已取得结果的任务跳过；初始化早期错误可能只有日志，无统一指标文件。费用账单和结果总表统计继续在项目外完成，不作为本项目缺失功能。显示呈现延迟保留为已知现象，不做修复。

环境哈希、配置加载及上一轮完整 realtime 回归：317 项全部通过，无跳过；该轮未发现需要额外修复的运行问题，未调用付费模型、未启动游戏 VM、未更改游戏 HTML 或 VM 服务，镜像不需要重打。

### 本轮 prompt 与代码变更（commit `08949d4e`）

四类 Agent 的 system prompt 首句统一为 `You are the computer-using Agent in a real-time GUI benchmark.`；在「收到当前截图与完整任务上下文」之后统一加入动作时序说明：动作执行需要一点时间，随动作结果返回的截图紧跟执行结束取得、未等待界面重绘，可能尚未反映动作执行后的状态，不能仅凭该截图断定动作无效，应以更晚的截图为准。

删除三处与运行时实现重复的说明：vanilla 的 `Do not request or infer historical video frames; this agent has no historical-video tool.`、video 的 `Do not submit an action sequence.`、anticipatory 的 `There is no historical-video tool for this agent.`。能力边界仍由配置校验与工具注册强制：没有历史帧能力的组不会注册 get_frames，atomic 组仍拒绝一次提交多个动作。

YAML 的 `system_prompt` 成为唯一 prompt 来源：删除代码内置的默认 prompt（原 `realtime_protocol.system_prompt`，−47 行），`RealtimeAgent` 现在必须收到 `system_prompt_text`（缺失抛 TypeError、全空白抛 ValueError）。模型实际 system 仍为 YAML 原文 + `api.coordinate_system` 对应的坐标约定；后者由代码按配置追加，不写进 YAML。

移除从未被任何配置、运行记录或验证记录使用的旧坐标选项 `normalized_0_999`；在用协议仍为 native_pixels、normalized_0_1000（Gemini）、normalized_0_1000_unclipped（MiniMax），三者 guidance 文本与改动前逐字一致。文案契约测试检查 40 份 YAML 的首句、时序说明位置，以及上述三句不再出现。

Gemini 官方口径复核（2026-09-17）：官方 Computer Use 正文与参考实现使用归一化 0–1000（`int(x/1000*screen_width)`），动作参数表仍写 0–999 属文档遗留；OSWorld-V2 的 Gemini 解析器另将端点限定到 width-1。本项目保持 0–1000，端点行为与各自上游一致。

### 验证

本轮实时相关回归 **329 项**、全量 `tests/` **339 项** 通过，无失败；`tests/test_maestro_minimax_provider.py` 因本地缺少上游依赖 `zhipuai` 未被收集，属既有环境问题、与实时项目无关。40 份 YAML 全部加载通过。本轮未调用付费模型、未启动 VM、未改游戏 HTML 与 VM 服务，镜像不需要重打。

Gemini/MiniMax 相对坐标沿用此前的真实调用记录（2026-09-16 受控定位相对协议 3/3 命中；2026-09-17 正式 combine 配置首次定位两模型均命中，误差 <4 px）。新 prompt 下 combine × 八模型 × 69 任务的正式实验尚待运行；成绩与逐模型金额在项目外的结果表单独记录，不写入仓库。

## 更早的复核记录

2026-09-15 的历史基线范围（两模型 8 组真实 VM 核验、138/212/317 项回归、环境来源与哈希核对、四组 Agent 逐项核查、当时的清理与边界说明）已从当前文档移除，需要时从 Git 提交历史追溯。
