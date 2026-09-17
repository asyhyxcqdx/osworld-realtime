# RealtimeGame v1.1(3) 接入与环境记录

游戏源为用户交付包 `RealtimeGame_v1.1(3).zip`（SHA-256 `7a4656df0e2b6b93b0bd0bb90f1830e4316d0d0d7eeef10833e244423399507a`），接口协议 `realtime-gui-bench/1.1`，环境级策略名 `realtime-gui-bench-anti-cheat/1.1`。

- 69 个运行网页与交付包内 HTML 逐字节一致，位于 `evaluation_examples/websites/realtime_gui_bench/games/<小写编号>/index.html`，目录只含 HTML。
- 69 个任务配置位于 `evaluation_examples/examples/realtime_gui_bench/<UUID>.json`，清单为 `evaluation_examples/test_realtime_gui_bench.json`。
- A/B/C/D 四类分别为 22/12/17/18。

## VM 镜像

- `docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2`，24,493,359,104 字节。
- 镜像内 `/home/user/server/realtime.py` 的 SHA-256 `8ffc76917c4484f078f02218032ee09cc3c54650da4e0f272427d8afd7aeeab7` 与仓库同文件一致。
- 每道游戏由任务配置在运行时上传 HTML，所以镜像本身不含游戏页面；**但改动 HTML 或 VM 服务都会让已有成绩不可比**，必须重新验收，改 VM 服务还需要 `--install_realtime_server` 或重打镜像（见 [部署与运行](../SETUP_GUIDELINE_CN.md)）。

## 初始化与评分

每道任务：上传单个 HTML → 本地 HTTP 8765 → Chrome/CDP 打开对应 URL → 激活窗口 → 等 3 秒；不提前点击 Start。

评分唯一来源是游戏写入的 BENCH 双指标，`result.txt` = `pass_at_3`，原始 BENCH 保存在 `result.json.raw_bench`。`results` 数组、URL hash 和 `__dbg` 都不参与评分；详细字段与状态机见 [BENCH 协议](../REALTIME_GUI_BENCH_PROTOCOL.md)。

## 验收证据

- 交付方自测 69/69（四条核心路径 ready / pass1 / pass3 / fail3），属交付方结果，不是我方复跑。
- 我方独立浏览器验收：69 个页面各独立加载一次（原生 1920×1080），加载 3 秒读取 BENCH、再等 3 秒复查，全部通过；138 个 BENCH 快照经 `scripts/python/check_realtime_gui_contract.py` 批量检查无契约错误。这不等于完整 VM 交互，也不等于模型成绩。
- 交付包 test.mjs 曾有启动失败/中断的重复尝试，未计入上述验收。
- 已知取舍：C29 开局目标位置固定；D35 的验收策略曾有偶发失败，交付方调整测试策略而非游戏逻辑。详细原始报告保留在交付 ZIP 内。
