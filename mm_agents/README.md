# Agent 说明

本目录包含 OSWorld 的多模态 GUI Agent 实现。实时 GUI 项目使用
`realtime_agent.py` 和 `realtime_protocol.py`，其他子目录是不同模型或历史 Agent 的适配实现。

## 实时 GUI Agent

四个实时变体共用截图、`computer_13` 动作空间和模型 API：

| Agent | 历史帧工具 | 单回合动作 |
| --- | --- | --- |
| Agent1 | 无 | 一个原子动作 |
| Agent2 | 无 | 一个动作序列 |
| Agent3 | 有 | 一个原子动作 |
| Agent4 | 有 | 一个动作序列 |

当前实现入口：

- `realtime_protocol.py`：模式配置、动作校验、工具 schema 和提示词。
- `realtime_agent.py`：模型请求、工具调用、历史帧查询和响应日志。
- 四个 Agent 的配置字段和内嵌 system prompt 规范见仓库根目录的
  `REALTIME_AGENT_CONFIG_PROTOCOL.md`。配置文件按
  `<agent_id>-<model>.yaml` 命名，例如 `vanilla-claude-fable-5.yaml`。

最终实验要求所有 Agent 使用原生 API tool use。历史帧查询可以在一个回合内调用多次，但不计额外回合；Agent1/3 的行动工具必须限制为单个动作。当前正式接口不接受文本 JSON 代替原生工具。

## 如何接入模型 API

模型凭据从环境变量读取，例如 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 或项目使用的中转站密钥。不要把密钥写入代码、配置文件或提交记录。

实时实验通过 `scripts/python/run_multienv.py` 启动，而不是直接运行 Agent 文件。完整参数和结果目录约定见仓库根目录的 `README_CN.md` 与 `AGENT_EXPERIMENT_DESIGN.md`。

## 修改原则

修改 Agent 时应保持四组之间只有预先定义的能力差异。动作 schema、工具调用、回合计数和日志格式的改动必须同时更新测试和实验文档。
