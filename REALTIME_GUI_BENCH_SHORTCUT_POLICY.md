# Realtime GUI Bench 环境快捷键阻断策略

版本：`realtime-gui-bench-anti-cheat/1.1`

这是 OSWorld 环境级策略，覆盖全部 Realtime GUI Bench 游戏。游戏制作侧不需要在每个网页中重复实现。

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

如果动作把修饰键和普通键拆成多个调用或多个原子回合，也在发送前识别并拒绝上述组合。宿主控制器在成功执行后更新已按住键集合，KEY_UP/DONE 或新的录制会话清除对应状态。拒绝动作不能发送到 VM。

鼠标刷新或导航不经过按键过滤时，runner 的页面身份检查在动作后/评分前发现标签页、URL 或加载时间改变，记 run_error 并停止评分。该检查不向模型提供 DOM 或答案。

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

字段含义：

- `event`：固定为 `action_rejected`；
- `reason`：固定为 `forbidden_browser_shortcut`；
- `action`：被拒绝的原始动作；
- `decision_id`：该动作所属的 Agent 决策回合；
- `timestamp_s`：相对于实时录屏起点的时间。

## 验收

环境统一运行一次快捷键矩阵测试，确认每个组合均在 VM 执行前被拒绝，并且每次拒绝都出现在轨迹中。该报告覆盖全部游戏，不要求游戏目录中保存副本。
