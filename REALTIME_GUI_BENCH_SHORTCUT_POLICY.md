# Realtime GUI Bench 环境快捷键阻断策略

策略名：`realtime-gui-bench-anti-cheat/1.1`（环境级策略，不是接口协议常量）

这是 OSWorld 环境级策略，覆盖全部 Realtime GUI Bench 游戏，游戏网页本身不需要重复实现。

## 必须拒绝的动作

动作校验器在执行前拒绝以下 `PRESS`/`HOTKEY`：

| 用途 | 按键或组合 |
| --- | --- |
| 刷新 | `f5`、`ctrl+r`、`ctrl+shift+r`、`browserrefresh` |
| 开发者工具 | `f12`、`ctrl+shift+i`、`ctrl+shift+j`、`ctrl+shift+c` |
| 查看源码 | `ctrl+u` |
| 地址栏 | `ctrl+l` |
| 页面导航 | `alt+left`、`alt+right`、`browserback`、`browserforward`、`browserhome` |
| 保存/打印 | `ctrl+s`、`ctrl+p` |

把修饰键和普通键拆成多个调用、或多个原子回合的组合，同样在发送前识别并拒绝；被拒绝的动作不会发送到 VM。

鼠标触发的刷新或导航不经过按键过滤，由 runner 的页面身份检查在动作后、评分前发现（标签页、URL、`performance.timeOrigin` 变化即判无效），记 run_error 并停止评分；该检查不向模型提供 DOM 或答案。

## 轨迹记录

每次拒绝写入 `trajectory.jsonl`：

```json
{
  "event": "action_rejected",
  "reason": "forbidden_browser_shortcut",
  "action": {"action_type": "HOTKEY", "parameters": {"keys": ["ctrl", "u"]}},
  "decision_id": 3,
  "timestamp_s": 12.345
}
```

- `event` 固定为 `action_rejected`，`reason` 固定为 `forbidden_browser_shortcut`。
- `action` 是被拒绝的原始动作：控制器的序列层拒绝时是**动作数组**，Agent 校验层拒绝时是**单个动作对象**。
- `decision_id` 是该动作所属的决策回合。
- `timestamp_s` 相对实时录屏起点，只有 runner 层写的事件带这个字段，Agent 层事件没有。
