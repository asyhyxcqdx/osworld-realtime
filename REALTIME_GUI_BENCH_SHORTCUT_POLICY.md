# Realtime GUI Bench 禁用快捷键

覆盖全部 69 个游戏，由 OSWorld 环境统一执行；**游戏网页本身不需要做任何事**。

## 禁用的按键

下面这些 `PRESS` / `HOTKEY` 在动作执行前就会被拦下，**不会发送到虚拟机**：

| 用途 | 按键或组合 |
| --- | --- |
| 刷新页面 | `f5`、`ctrl+r`、`ctrl+shift+r`、`browserrefresh` |
| 开发者工具 | `f12`、`ctrl+shift+i`、`ctrl+shift+j`、`ctrl+shift+c` |
| 查看源码 | `ctrl+u` |
| 地址栏 | `ctrl+l` |
| 前进 / 后退 / 主页 | `alt+left`、`alt+right`、`browserback`、`browserforward`、`browserhome` |
| 保存 / 打印 | `ctrl+s`、`ctrl+p` |

把组合拆开也一律算违规：例如先 `KEY_DOWN ctrl`，下一回合再 `PRESS u`，同样会被拦下——程序记住哪些修饰键还按着，跨回合也能识别。

## 鼠标绕道也不行

用鼠标点刷新按钮或手势前进/后退，绕不过按键过滤：运行器会在动作后、评分前检查标签页、URL 和页面加载时间，发现变化就把这次运行判为无效（`run_error`），不评分。

## 被拦下之后

被拦的动作不执行，但会在任务目录的 `trajectory.jsonl` 里留一条 `action_rejected` 记录（写明被拦的动作和所属回合），便于事后审计。**发生拦截的那次运行没有有效成绩，需要重跑。**

校验发生在宿主控制器发送给虚拟机之前，所以对四组 Agent 和所有模型一致生效。
