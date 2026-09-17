# RealtimeGame v1.1(3) 接入与环境验收

当前游戏源为用户交付 `RealtimeGame_v1.1(3).zip`，协议 `realtime-gui-bench/1.1` 和 `realtime-gui-bench-anti-cheat/1.1`。ZIP SHA-256：`7a4656df0e2b6b93b0bd0bb90f1830e4316d0d0d7eeef10833e244423399507a`。

## 接入

69 个运行网页与 ZIP 对应 HTML 逐字节一致；最新包导入改变了旧运行树的 22 个 HTML。运行路径为 `websites/realtime_gui_bench/games/<小写编号>/index.html`，只含 HTML。包内 README、test.mjs、manifest 和交付方报告不放进 VM 游戏目录。

69 个 `examples/realtime_gui_bench/<UUID>.json` 与 `test_realtime_gui_bench.json` 是必要接入配置，编号不重排。UUID 由既有 URL 的 UUIDv5 规则保持稳定。A/B/C/D 数量为 22/12/17/18。D1 现对应交付包的 D1_Random_Enemy；早期 C2 命名和手动删除页脚的约定已被最终包替代。

逐文件 HTML/任务哈希与包级清单映射保存在项目外的验收记录中，不写入仓库。

## 三类证据分别记录

- 交付方自测报告：69/69，四核心路径 ready/pass1/pass3/fail3，B/C/D 可能另含 neg。该报告是交付方已完成的自测，不视为我方重新运行的结果。
- OSWorld 独立浏览器验收：69 个页面各 1 次独立加载，原生 1920×1080；加载 3 秒读取 BENCH，再等待 3 秒复查，69/69 初始状态、可见入口和稳定性通过。保存的 138 个实际 BENCH 快照由离线 checker 单文件/批量检查，无契约错误。该验收不等于 69 道完整 VM 交互或模型成绩。
- 真实最终 VM：源码哈希与仓库相符，WAIT 0.25 实测约 0.250364 秒；Fable5 与 Astra 的 combine 分别第二次、第三次通过 C1。最新整理又用模拟模型回复完成两协议 × 四组的真实 VM 链路核验，详见 [review](../REALTIME_REVIEW_REPORT.md)。

浏览器原始证据保存在本地证据目录 `realtime-game-final-validation-20260915/`（不在仓库中），没有把旧版浏览器交互成绩算作新版成绩。交付包 test.mjs 曾有启动失败/中断的重复运行尝试，未作为本次独立验收通过依据。

## VM 镜像

使用 `docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2`，SHA-256：`8196fe7eda43ca93b4ffe377541a245e0e57b41991890e05f6698248883df0d2`。

镜像内 `/home/user/server/realtime.py` 哈希 `8ffc76917c4484f078f02218032ee09cc3c54650da4e0f272427d8afd7aeeab7`。旧 `Ubuntu-realtime-gui-fmp4.qcow2` 已淘汰；`Ubuntu-realtime-gui.qcow2` 是重建用只读源，保留。每道游戏由正式 Config 上传，所以更新 HTML 不要求重打镜像；更新 VM 服务才需安装/重建。当前宿主检查不会改变 final 镜像字节。

## 初始化和评分

每道 Config：上传单个 HTML → 本地 HTTP 8765 → Chrome/CDP → 打开对应 URL → 激活窗口 → 等 3 秒。Config 不提前点击 Start。

评分唯一来源是游戏直接写入的 BENCH 双指标，result.txt=pass_at_3，原始 BENCH 保存到 result.json.raw_bench。results 数组、URL hash 和 __dbg 都不参与正式评分。正常评估时，提前 DONE 或回合上限后仍 ready/running 且未成功记有效 0 分，保留原始状态；接口或环境异常无有效成绩。结束原因由 termination_reason 单独记录，项目不生成整体/分类汇总。更改任务或实验设置时用新批次；同批次续跑会清空无 result.txt 的旧任务记录后重跑，具体行为见 [运行说明](../SETUP_GUIDELINE_CN.md)。

原始历史报告仍在本地结果目录，旧交付讨论稿和过时配置从活动文档删除；需要追溯时使用 Git 历史。

## 交付方已知限制

C29 开局目标位置固定是交付方确认保留的设计取舍；D35 的验收策略曾有偶发失败，交付方调整测试策略而非游戏逻辑。以上不通过修改 HTML 隐藏，详细原始 README 和报告保留在用户 ZIP。
