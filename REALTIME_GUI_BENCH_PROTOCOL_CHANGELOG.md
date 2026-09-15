# Realtime GUI Bench 协议变更记录

## 1.1（2026-09-14）

### `REALTIME_GUI_BENCH_PROTOCOL.md`

- `protocol_version` 从 `realtime-gui-bench/1.0` 升级为 `realtime-gui-bench/1.1`。
- `running` 现在包括“失败结算后等待下一次重试入口”。
- 重新加载只用于新的 OSWorld `run_id`；同一 `run_id` 内的刷新、导航和复制页面由环境判定为运行失效。
- 明确 `pass_at_1/pass_at_3` 是同一个 run 内三次机会的结果，不是跨 run 的独立样本。
- `window.__dbg()` 明确为测试专用只读接口；离线测试可读取内部状态，正式 Agent 和评分不使用它。
- 区分 Agent 未完成与环境/接口无效：前者兼容标量为 0，不能伪造游戏失败；当前终态汇总只纳入 passed/failed，完整任务成功率需另行说明分母。
- 明确 URL hash 只写入诊断信息，不能驱动游戏状态或评分。
- 实时动作空间只保留 `DONE` 作为终止动作，不提供 `FAIL`。
- `evaluation_settle_s` 的正式默认值为 3 秒，仍可通过命令行覆盖。

### `REALTIME_GAME_ANTICHEAT_ACCEPTANCE.md`

- 反作弊协议从 `realtime-gui-bench-anti-cheat/1.0` 升级为 `1.1`。
- 反作弊范围限定为 Agent 通过 GUI 越权获取信息、修改状态或重置机会。
- 移除 `isTrusted`、合成事件和统一事件模型等非必要要求，不改变游戏原有玩法。
- F5、F12、查看源码、地址栏、开发者工具和导航快捷键由环境统一阻断。
- 不要求客户端源码或测试专用 `__dbg()` 绝对不包含内部答案；正式 Agent 无法访问这些接口。
- 快捷键阻断报告由 OSWorld 环境统一提供，不要求逐游戏重复提交。

### 配套文件

- `scripts/python/check_realtime_gui_contract.py`：离线校验 `window.BENCH` 八字段和状态不变量。
- `REALTIME_GUI_BENCH_SHORTCUT_POLICY.md`：环境快捷键阻断和轨迹记录约定。
