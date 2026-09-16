# Realtime GUI 项目复核记录

## 当前复核（2026-09-17）

当前环境是 RealtimeGame v1.1(3) 的 69 个原始 HTML 与终版镜像；八款实验模型各四组配置齐全，另保留 Fable/Astra 八份历史配置。Gemini/MiniMax 的坐标 prompt、工具字段及转换器按配置配套，其余模型保持原生坐标。所有模型原样接收 1920×1080 PNG。

本次提交包含此前已确认的模型坐标适配、三位小数展示、100 回合默认预算、公共探索 prompt、Chrome 更新提示启动参数，以及另一线程的单任务结束状态/移除类别汇总改动。补齐活动文档的模型数量、实验范围和评分口径，并修正 HTML 浏览器测试点击后未等待异步内容出现的问题。

批量实验行为已确认保留：单任务异常后继续下一任务；同一 run_id 续跑清空无 result.txt 的旧任务记录后重跑，已取得结果的任务跳过；初始化早期错误可能只有日志，无统一指标文件。费用账单和结果总表统计继续在项目外完成，不作为本项目缺失功能。显示呈现延迟保留为已知现象，不做修复。

环境哈希、配置加载及完整 realtime 回归的本轮结果见 [复核摘要](validation/realtime_final/project_review_20260917.json)：317 项全部通过，无跳过。本轮未发现需要额外修复的运行问题。未调用付费模型、未启动游戏 VM、未更改游戏 HTML 或 VM 服务，镜像不需要重打。离线检查通过不等于真实游戏成绩；后续仍需 Gemini/MiniMax 当前坐标协议的 C1 验证，以及新 prompt 下 combine × 八模型 × 69 任务的一次正式实验。

## 历史基线范围（2026-09-15）

后续更新：新增八款 Packy 模型的 32 份 Agent 配置与 Gemini Chat/high 正式通路；保留原有 8 份配置。Gemini 输出上限 65536；MiniMax M3 只开 adaptive、effort=null。当前模型/密钥/启动方式见 [配置目录说明](configs/realtime_agents/README.md)。下文的两模型、8 组 VM 验证和 138 项测试是此前基线 review 的历史范围，不代表新模型完整实验已经通过。

当时结论：最新交付的 69 个 HTML 已完整接入，当时两模型各四组配置齐全，原生坐标协议一致。已修正当时发现的配置回退、校验、跨回合快捷键与环境更新检查问题；当时检查范围内没有剩余阻塞。大规模模型实验尚未完成。

### 后续八模型接入复核（历史记录）

- 对齐另一任务的任务说明去重：对话开头只保留一条 Task 消息；后续决策只新增截图及时间，模型回复、工具结果和完整历史继续保留。三种协议和 reset 均有回归覆盖。
- 合入八模型四组配置、Gemini Chat/high（65536 输出上限）、MiniMax adaptive（不发送 high）、三组专用密钥选择和截断响应禁止执行部分动作的检查。
- 复核发现实时 CLI 原先可接受与固定坐标协议冲突的屏幕尺寸，现已在启动 VM 前拒绝非 1920×1080 设置。没有改变图片或动作坐标。
- 重新逐字节对比最新 ZIP 与当前 69 个 HTML，并核对 69 份任务配置哈希；两份 VM 服务源码未改变。模型 API 往返与原图测试使用此前实际完成的记录，不重复调用付费模型。
- 当前实时相关回归 **212 项通过**；40 份 YAML 均可加载；终版 qcow2 完整 SHA-256 与既有验收值一致。
- 可复核摘要见 [agent_model_review.json](validation/realtime_final/agent_model_review.json)。本次只提交宿主机 Agent、配置、测试与文档；环境源码不变，无需重打镜像。

### 环境来源

- 对用户的 RealtimeGame_v1.1(3).zip 逐文件 SHA-256 核对：69/69 HTML 原字节相同。
- 包内 manifest、任务 JSON、UUID 和运行清单对应一致，A/B/C/D=22/12/17/18。
- 运行游戏目录只有 69 个 index.html；任务 JSON、清单和评分器为必要文件。
- 交付方 69/69 自测仅作为其自测记录；OSWorld 已保存的 69 次独立页面加载及 138 个 BENCH 初始/延迟快照另用 checker 单文件和批量模式核对，错误为 0。
- 最终镜像里的 realtime.py、fmp4.py 哈希与仓库匹配。真实 WAIT 0.25 执行 0.250326 秒，100 动作序列完整执行。

来源与精简证据： [source_manifest.json](validation/realtime_final/source_manifest.json)、[checker_single.json](validation/realtime_final/checker_single.json)、[checker_batch.json](validation/realtime_final/checker_batch.json)、[image_execution_check.json](validation/realtime_final/image_execution_check.json)。

### 四组 Agent 核查和修正

1. 正式模型明确为 claude-fable-5、gpt-6-astra，各有 vanilla/anticipatory/video/combine 四份 YAML，共 8 份。取消未知模型配置静默回退到 Sonnet，显式指定模型也不得与配置冲突。
2. 启动器统一复用 agent_kwargs()，减少手工逐字段传递造成遗漏的风险。校验 Agent ID 与 sequence/frames 一致，必需约束不得缺失；旧 coordinate_mapping 被拒绝。
3. 两模型都原字节发送 1920×1080 图，原样执行 x/y。没有启用预缩放、比例猜测或自动放大。测试同时检查实际发出的图像字节和原始动作坐标。
4. review 当时保留 Agent3 单次回复多个帧查询；后续按用户要求统一为每次回复一个工具调用，即一个 get_frames 或一个动作。允许收到结果后继续查询；一次 get_frames 仍可取 1–8 张帧。Agent4 保持多查询能力。
5. 录制前比对 VM 两份源码的 SHA-256 并记录。旧镜像或安装遗漏会在调用付费模型前被发现。构建器的验证也检查实际 WAIT 和 100 动作上限。
6. 控制器持续记录已按住的修饰键，阻断跨原子回合组合出的刷新快捷键；整段非法动作仍不发送到 VM。
7. runner 记录页面 ID、URL、performance.timeOrigin 和标签页集合，动作后与评分前检查；重载、导航、复制页不计为正常游戏成绩。真实 VM 中模拟刷新已验证被识别。
8. 动作工具结果的 decision_id 与实际动作回合对齐；额外记录 run_error，避免把 API/环境错误混同于执行成功。getter 补齐与 metric/checker 一致的 pass_at_1 不变量检查。

### 验证

- 实时相关回归：138 passed，无跳过；包括 live fMP4 真实录制/解码测试。
- 真实 VM + 模拟模型回复：两协议 × 四组，8/8 通过；检查工具能力、原图尺寸、实际 WAIT、按键无隐含 0.1 秒等待、真实历史帧和 DONE。全程顺序运行，没有付费模型请求。见 [runtime_matrix.json](validation/realtime_final/runtime_matrix.json)。
- 真实模型成绩沿用此前独立 C1：Fable5 第二次通过，Astra 第三次通过；原始轨迹没有改写。这不是四组完整模型 benchmark；见 [C1 报告](PACKY_C1_FINAL_TRIAL_REPORT.md)。
- 本次未修改 69 个游戏 HTML 或 VM 两份服务源码，终版镜像无需重打。

### 清理

移除 12 份淘汰的 Fable5.1/Opus/Sonnet 配置，保留两模型 8 份当前配置。删除 7 份已由正式协议与最终报告覆盖的交付讨论/失败分析文档；将总览、运行说明、配置、loop、实验设计和环境接入记录更新为当前版本。旧材料可从 Git 历史追溯。

原始 ZIP 附件、实验结果、录像、最终镜像和构建源保留。没有删除上游 OSWorld 的其他任务、模型适配器或无关目录。旧 WAIT=0、坐标适配误判、单条 C1 成功等均不再被混写为全项目结论。

### 边界

仅 combine 已有两个真实模型 C1 成功样本，其余组本次验证的是实现链路，不是模型成功率。69 道完整模型实验、不同负载下时序稳定性和全部浏览器 GUI 越权路径的穷举测试不在本次通过结论中。新页面身份检查增加少量本地 CDP 读取，应以新 run_id 运行后续对照；不把修改前后实验混为同一批次。
