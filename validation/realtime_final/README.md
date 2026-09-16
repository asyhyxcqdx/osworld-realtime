# 环境与 Agent review 证据

这些是环境、Agent 和模型接口的核查摘要；接口短测不等同于完整游戏成绩：

- project_review_20260917.json：当前提交前复核，记录 69 个 HTML/任务配置与终版镜像哈希、40 份配置及完整 realtime 回归；并明确批量异常继续、无成绩任务续跑替换、初始化早期错误日志行为按用户确认保留，费用/总表统计位于项目外。

- source_manifest.json：交付核查时的 ZIP 哈希、69 个 HTML 与任务配置哈希、manifest 映射；作为当时的快照保留。
- done_prompt_review.json：后续将结束说明明确为 `computer_done({})` 并删除任务说明中重复结束要求的指令核查；任务说明仍保留游戏规则、继续尝试、禁止导航三句。69 个任务只改 instruction，记录新旧任务配置哈希；40 份 Agent 配置统一结束提示和 1000000 上下文声明，69 个 HTML 哈希仍与交付快照一致。
- checker_single.json / checker_batch.json：OSWorld 保存的实际 BENCH 快照通过单文件/批量契约检查；完整快照和原始报告在文件内引用的本地证据目录。
- runtime_matrix.json：真实 VM + 两协议四组模拟回复；读取原生截图/历史帧，实际执行 WAIT 和 DONE，最后模拟刷新验证运行失效检查。
- chrome_update_prompt_review.json：同一终版 VM 中 Chrome 130 的更新提示对照检查，以及 69 个任务新增过旧版本检测关闭开关后的配置哈希；旧交付快照不覆盖。
- image_execution_check.json：终版镜像重新启动后的源码哈希、实际 WAIT、100 动作序列检查。
- streaming_review.json：八款实验模型的真实 SSE 查帧/动作往返、原始流回放，以及当时的 120 秒/三次尝试、70 个决策回合、1000000 上下文声明；不代表游戏评分。后续按用户要求将默认决策回合上限改回 100，保留该历史快照原值。

没有模型密钥、请求授权头、模型私有推理、原始截图或 VM 镜像。真实 C1 结果另见仓库根目录的 PACKY_C1_FINAL_TRIAL_REPORT.md。

`agent_model_review.json` 是后续八模型配置与任务说明去重对齐的复核摘要，包含真实 API 短测及原生坐标小样本记录；不代表完整游戏实验已通过。
