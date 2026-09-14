# Realtime GUI Bench 迁移前对齐确认回复

日期：2026-09-14
适用版本：`realtime-gui-bench/1.1`、`realtime-gui-bench-anti-cheat/1.1`

已收到游戏制作侧的迁移前确认。第〇节列出的协议语义、反作弊边界、交付规格、测试环境、统计口径和执行顺序与 OSWorld 侧一致，无需重新设计游戏玩法。

## 迁移开始文件

以下文件已经放入 OSWorld 仓库根目录，游戏制作侧可直接按此版本迁移：

```text
REALTIME_GUI_BENCH_PROTOCOL.md
REALTIME_GAME_ANTICHEAT_ACCEPTANCE.md
REALTIME_GUI_BENCH_PROTOCOL_CHANGELOG.md
REALTIME_GUI_BENCH_SHORTCUT_POLICY.md
scripts/python/check_realtime_gui_contract.py
```

协议正文和反作弊正文均为 `1.1`。changelog 记录本次字段、状态机、运行失效、调试接口、统计和终止动作的变化；快捷键策略说明环境统一拦截范围和 `trajectory.jsonl` 记录格式。

## Checker 使用

单个 `BENCH` JSON：

```bash
python scripts/python/check_realtime_gui_contract.py \
  --input bench.json --benchmark-id A1
```

批量目录：

```bash
python scripts/python/check_realtime_gui_contract.py \
  --input-dir games \
  --manifest realtime_gui_bench_manifest.csv \
  --output realtime_gui_contract_report.json
```

checker 只校验并读取游戏提供的八个正式字段，不推导 `pass_at_1` 或 `pass_at_3`，也不会修改网页状态。接口错误返回 `unscored`；合法但仍为 `ready/running` 的任务归为 Agent 未完成。

## 运行约定

- Node.js 使用 22 或更高版本，Chrome 允许回环地址和动态端口。
- 每个游戏测试使用独立临时 profile 与端口，单游戏超时 180 秒，最多并行 4 个。
- `evaluation_settle_s` 默认 3 秒，可用命令行参数覆盖。
- 迁移包中的每个目录包含 `index.html`、同目录 `test.mjs` 和 `README.md`；根目录放 CSV 与 Markdown 清单。
- 正式实验中唯一终止动作是 `{"action_type":"DONE"}`。动作空间不提供 `FAIL`。

收到上述文件后即可开始 69 个游戏的接口迁移。OSWorld 侧随后使用独立环境运行 checker 和全量复验，不要求制作侧修改原有玩法或额外提交 `memory.md`。

## Q16. Checker 输入与交付包

1. 迁移压缩包不需要包含每个游戏的 `BENCH` JSON 快照。`bench.json` 是测试运行时通过 CDP 从 `window.BENCH` 读取后生成的临时输入，不是游戏交付物。
2. `realtime_game_test_report.json` 和 `realtime_gui_contract_report.json` 不作为必交文件；制作侧可以保留并提交，便于 OSWorld 复验，但缺少它们不会影响接收。
3. 本地自测时可以在每个游戏目录放一个 JSON 快照，再使用 `--input-dir` 和 manifest 批量校验。正式交付包仍只要求 `index.html`、`test.mjs`、`README.md` 及根目录清单。

Checker 的正式路径是：

```text
scripts/python/check_realtime_gui_contract.py
```

仓库根目录没有同名副本；后续如路径调整会另行通知。

## Q17. 反作弊测试归属

理解正确。刷新、后退、前进、复制标签页和重新打开页面造成的运行失效由 OSWorld 根据 `run_id` 验收，游戏不需要跨加载保存 `invalid` 状态。游戏侧的 `test.mjs` 只需验证页面生命周期内的终态冻结、机会计数不回退、没有第四次尝试以及 `__dbg()` 不提供写状态入口。

四条路径（`ready`、`pass1`、`pass3`、`fail3`）和最后一行 `RESULT_JSON` 作为游戏侧测试记录。README 只需按模板记录协议版本、测试命令和测试结果，不需要另写刷新或快捷键报告；环境统一提供快捷键阻断和运行失效报告。

`BENCH.task` 可以在迁移时改为清晰的 snake_case。只要每个游戏内部保持稳定、manifest 和 README 同步更新，且不把它当作外部 `benchmark_id`，OSWorld 不要求保留旧名称。
