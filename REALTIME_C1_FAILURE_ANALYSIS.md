# C1 combine 失败分析与接口指令一致性审计

日期：2026-09-14。分析对象为 `gpt-6-astra` 经用户指定的 Sudorelay 接口运行的 Agent4 / combine。

**当前首先需要解决的是接口指令一致性。旧 C1 轨迹中，8 个原始响应有 7 个报告了与请求不同的 instructions；不能将这次三轮失败用于推断 GPT-6 Astra 或 combine 的能力上限。此前把遗漏显式 parallel_tool_calls 判为根因的结论已撤回。**

## 1. 最直接的异常证据

正式运行实际发送的 system prompt 共 1,062 字符，以 `You are the combine Agent` 开始。旧运行的第 1、3、4、5、6、7、8 次 Responses 回复却都报告了相同的 21,488 字符 instructions，以 `You are Codex, a coding agent` 开始，而且不包含我们发送的完整 prompt。只有第 2 次回复与请求一致。

替换文本包含编程、文件搜索、前端设计和专用并行工具的使用指令，与本实验注册的 GUI 原子动作工具不匹配。本次新 C1 的首个回复，以及三个协议探测回复也报告了相同文本。共审计 13 个完整回复，11 个不一致。

- [逐响应哈希、来源行号与统计](/mnt/zhaorunsong/yhyx/OSWorld/results_realtime_c1_trials/gpt-6-astra/diagnostics_20260914/instruction_integrity_audit.json)
- [旧实验原始轨迹](/mnt/zhaorunsong/yhyx/OSWorld/results_realtime_c1_trials/gpt-6-astra/c1_combine_astra_sudorelay_high_repair_20260914T210313/agent4/computer_13/screenshot/realtime_gui_bench/5169e1b0-1a7d-538b-8e59-8785c39460ce/trajectory.jsonl)

这是返回协议中的可观测事实。仅凭响应字段无法查看供应商内部最终送入模型的全部消息，因此严格表述为“接口未稳定保留或正确报告请求指令”。行为证据也与指令丢失一致：显式多调用探测把三个 WAIT 的数值全部写在 instructions 中，模型却回答没有收到三个调用的具体要求。

## 2. 为什么原来的多调用解释不成立

旧 C1 的 8 个回复全部报告 `parallel_tool_calls=true`。新做的省略字段探测也报告有效值为 true。因此，提交 `f772d878` 使请求意图更明确，但不能宣称它解除了此前已证实存在的单动作限制。

官方文档说明，模型可以选择一轮调用多个函数，false 会限制为零或一个；true 并不要求模型一定生成多个调用。[OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling#parallel-function-calling)

| 探测 | 指令是否一致 | 实际返回 | 能说明什么 |
| --- | --- | --- | --- |
| 省略 parallel_tool_calls，要求依次 WAIT 0.11、0.22、0.33 | 一致 | 有效值 true，仅 WAIT 0.11 | 至少这一次，即使指令保留且允许多调用，模型仍未提交完整序列 |
| 显式 true，同一要求 | 不一致 | 没有调用，文本称缺少具体要求 | 对照被指令变化干扰，不能用来衡量开关的效果 |
| 要求 get_frames(62.56) | 不一致 | get_frames(0) | 工具存在，但没有遵循指定时间 |
| 回传旧 step_3.png 图片 | 不一致 | 正确描述 Attempt 2/3 和 Next 按钮，但没有按要求提交 WAIT | 这条图片序列化链路可被模型读取；不能验证主动复盘策略 |

历史图片探测使用固定旧截图作为工具结果，没有调用真实 VM 的取帧服务；模型请求 0 秒而固定结果标注 62.56 秒，此测试不构成时间选择或录像定位正确性的验证。原始响应与局限说明保存在 [diagnostics_20260914](/mnt/zhaorunsong/yhyx/OSWorld/results_realtime_c1_trials/gpt-6-astra/diagnostics_20260914)。

本项目按回复中调用的顺序在 VM 内串行执行。API 名称中的 parallel 不代表游戏动作被同时执行；动作序列的执行顺序由我们的执行器实现。

## 3. 旧 C1 是怎样失败的

| 时间（任务秒） | 已核实的事件 |
| ---: | --- |
| 38.321 | VM 开始执行 KEY_DOWN d |
| 38.525 | 给下一轮模型的截图中，角色尚在左平台 |
| 39.479 | 录像已显示 Fell、attempts_completed=1 |
| 约 51 | 首次跳跃模型请求返回，耗时 12.447 秒；space 被旧 schema 拒绝 |
| 62.560 | 纠正请求再耗时 11.293 秒后执行旧字面空格；已在下一轮入口 |
| 79.431 | 点击 Next 后的观测 |
| 81.508 | 录像已显示 Attempt 3/3，即第二次也已掉落 |

关键证据：[第一次掉落帧](/mnt/zhaorunsong/yhyx/OSWorld/results_realtime_c1_trials/gpt-6-astra/diagnostics_20260914/old_run_t39.5.png)、[第二次掉落后画面](/mnt/zhaorunsong/yhyx/OSWorld/results_realtime_c1_trials/gpt-6-astra/diagnostics_20260914/old_run_t81.5.png)。抽帧的实际解码时间见 [帧索引](/mnt/zhaorunsong/yhyx/OSWorld/results_realtime_c1_trials/gpt-6-astra/diagnostics_20260914/old_video_frames.json)。

第一轮从按住 D 到已掉落只有约 1.16 秒；即使 space 校验一次成功，12.447 秒的请求延迟也来不及。D 后续没有 KEY_UP，游戏的 nextRound/reset 也没有清除方向输入状态，因此点击 Next 后会继续向右移动。这解释了为什么单步点击重试同样快速消耗机会。

space 校验确实是项目问题，已在 `a7354757` 统一为 `space`；它增加了额外等待，但不能单独解释首次失败。

旧响应的实际摘要提到了跳跃物理、时序差异、延迟输入和同步操作规划。不能说它“完全没有反思”。可以确认的是：7 个执行回合都只有一个动作，没有调用 get_frames，也没有将摘要所提的规划落实为连续二段跳。摘要不是完整内部推理，不能补写其具体思考过程。

## 4. 项目自身还需要怎样改进

已落实的改动：

1. Responses 若明确报告不同的 instructions，记录 `instruction_audit` 和 `provider_instruction_mismatch`，保存完整原始回复，并在执行任何工具前抛出 `ProviderInstructionMismatchError`。不能将这类响应视为有效 benchmark 行为。
2. 若供应商没有报告 instructions（缺失或 null），记为 not_reported；这不等于一致性已获验证。其他 API 协议记为 not_applicable。
3. 保留 sequence 请求的显式 true；恢复 atomic Agent 的供应商默认值，避免把 Agent3 本来可以成组提交的帧查询一起限制掉。单动作上限仍由动作校验器执行。

尚待有效接口上的独立对照：YAML 的简短 prompt 覆盖了代码中的默认详细 prompt。因此默认 prompt 里的“推理期间画面继续变化”“一整段动作后才返回截图”“按明确 WAIT 控制间隔”等内容并非全部出现在正式请求中。可补齐这些通用执行语义，再与原 prompt 比较；这应记录为提示词实验，不能声称已经提升通关率。无需强迫每轮多个动作或每次失败必须调用录像，也不应注入 C1 源码或通关时序。

## 5. 本次真实复跑与验证

运行 `c1_combine_astra_explicit_parallel_20260914T233445` 使用提交 `f772d878`，原 YAML 未改、同一模型和接口、截图 1920×1080。第一请求用时 73.415 秒并点击 Start；第二请求用时 147.307 秒后读超时。总共执行 1 个动作、0 次帧查询，done=false，无 result.json，分类为 **未评分 / 接口异常中断**。日志中的通用 Average score: 0 和进程退出码 0 不能替代有效游戏终态评分。

- [新运行目录](/mnt/zhaorunsong/yhyx/OSWorld/results_realtime_c1_trials/gpt-6-astra/c1_combine_astra_explicit_parallel_20260914T233445)
- [运行日志](/mnt/zhaorunsong/yhyx/OSWorld/results_realtime_c1_trials/gpt-6-astra/c1_combine_astra_explicit_parallel_20260914T233445/console.log)

本地 realtime Agent / runner 测试 **71 passed**，覆盖指令不一致时禁止动作和帧查询、保存原始证据，以及一致或未报告指令时维持原有流程。旧轨迹、录像和评分均保留；旧试跑报告已补充更正。

将旧 C1 的真实首个响应离线重放给新检查器，已确认触发 ProviderInstructionMismatchError，pending_action_calls=null、tool_calls=0：[真实响应重放结果](/mnt/zhaorunsong/yhyx/OSWorld/results_realtime_c1_trials/gpt-6-astra/diagnostics_20260914/guard_replay_result.json)。另一次真实接口检查在返回完整响应前读超时，零工具执行；没有把这次超时误记为在线成功拦截：[在线检查结果](/mnt/zhaorunsong/yhyx/OSWorld/results_realtime_c1_trials/gpt-6-astra/diagnostics_20260914/guard_live_result.json)。

当前可以归因的是：项目原有 space 校验错误、供应商指令一致性异常、此次读超时，以及轨迹中的单步时序策略。尚不能确定纯模型能力上限，也不能声称 combine 已通关。下一次有效模型实验的前提是该路由稳定保留 benchmark 指令，或更换为已验证的同模型直通路由。
