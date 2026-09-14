# C1 / D1 试点验收反馈

日期：2026-09-14
验收方：OSWorld 侧
协议：`realtime-gui-bench/1.1`、`realtime-gui-bench-anti-cheat/1.1`

## 验收范围

本次检查了压缩包中的：

- `C1_Double_Jump/index.html`、`test.mjs`、`README.md`；
- `D1_Random_Enemy/index.html`、`test.mjs`、`README.md`；
- `realtime_gui_bench_manifest.csv`、`realtime_gui_bench_manifest.md`；
- `realtime_game_test_report.json`。

## 独立测试结果

两个 `test.mjs` 均按约定从各自目录运行。当前主机上的 `/usr/local/bin/chrome-headless-shell` 启动时因 ICU 文件描述符错误崩溃，因此通过支持的 `CHROME` 环境变量切换为 `/opt/chrome/chrome` 后复验：

```text
C1_Double_Jump: RESULT_JSON ok=true，退出码 0，总耗时约 12.8 秒
D1_Random_Enemy: RESULT_JSON ok=true，退出码 0，总耗时约 23 秒
```

两款游戏的 `ready`、`pass1`、`pass3`、`fail3` 四条路径均通过；终态冻结、计数单调、无第四次尝试、`__dbg()` 无写状态入口等附加验点也通过。

使用实际页面通过 CDP 读取的初始 `window.BENCH` 快照，确认两款游戏都提供完整的 1.1 八字段，初始状态为合法 `ready`。对代表性的终态快照运行离线 checker，C1 `passed` 和 D1 `failed` 均返回 `classification: scored`、`errors: []`。

## 需要修订的文档内容

以下不影响本次网页功能测试，但交付包中的 README 仍有旧口径，迁移批次请一并修正：

1. `C1_Double_Jump/README.md` 的“评分读取时机”仍写着 checker 等待 20 秒；当前正式默认值是 `evaluation_settle_s=3` 秒，应改为 3 秒，并注明可由命令行覆盖。
2. `D1_Random_Enemy/README.md` 把刷新/重开写成“待协议定义、待整改”。1.1 已明确刷新、导航和文档替换后的运行失效由 OSWorld 按 `run_id` 判定，游戏不做跨加载持久化；请改成已符合当前协议的说明。
3. D1 README 中“已知限制”关于跨加载重掷随机的旧待办也应删除或改写，避免与 1.1 交付口径矛盾。

## 结论

C1 和 D1 的网页接口及四路径测试通过，可以作为试点实现提交。请在下一版压缩包中同步上述 README 修订；Chrome 路径问题不是游戏代码问题，测试脚本已有 `CHROME` 环境变量回退机制，OSWorld 验收环境会显式设置可用的 Chrome 路径。
