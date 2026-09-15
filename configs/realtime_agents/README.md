# Realtime 模型配置

当前新增的 Packy 实验模型共 8 款，每款都有 vanilla / anticipatory / video / combine 四份 YAML。Fable 5、Astra 原有 8 份配置保留，目录合计 40 份 YAML。

| model（区分大小写） | 协议 | 上下文声明 | 输出上限 | 思考 | 密钥环境变量 |
|---|---|---:|---:|---|---|
| `claude-sonnet-5` | anthropic_messages | 1000000 | 128000 | high | `PACKY_COMMON_API_KEY` |
| `gpt-5.6-sol` | openai_responses | 1000000 | 128000 | high | `PACKY_COMMON_API_KEY` |
| `gemini-3.8-flash` | openai_chat | 1000000 | **65536** | high | `PACKY_COMMON_API_KEY` |
| `qwen3.8-max-0902` | anthropic_messages | 1000000 | 128000 | high 兼容请求 | `PACKY_COMMON_API_KEY` |
| `kimi-k3` | anthropic_messages | 1000000 | 128000 | high | `PACKY_KIMI_API_KEY` |
| `deepseek-flash` | anthropic_messages | 1000000 | 128000 | high | `PACKY_COMMON_API_KEY` |
| `glm-5.3-flash` | anthropic_messages | 1000000 | 128000 | high 兼容请求 | `PACKY_GLM_MINIMAX_API_KEY` |
| `MiniMax-M3` | anthropic_messages | 1000000 | 128000 | **adaptive，无 effort 档位** | `PACKY_GLM_MINIMAX_API_KEY` |

`Claude-sonnet-5.0` 不是此处的 API ID。GLM 5.3（非 Flash）只有文本输入、Seed 2.1 Turbo 未确认可用，均未加入截图 Agent 配置。

上下文声明统一为 1000000，目前只用于配置校验，不发送给 API，也不据此在本地裁剪历史；不宣称它是各模型已经实测的真实上限。实际输入保留完整历史，服务端判断是否超限。输出上限则随请求实际发送；达到上限可能截断回答，思考 token 也可能占用输出预算。收到明确的 length/max_tokens/incomplete 结束状态时，不执行其中的部分动作。

## 协议和原生坐标

所有模型原样接收 1920×1080 PNG，动作坐标原样交给 VM。没有图像预缩放或坐标映射。模型输出的错误坐标保留为模型表现。

- Messages：保留完整 thinking/tool_use/tool_result。MiniMax M3 只发送 `thinking: {type: adaptive}`，不发送 `output_config.effort` 或 Claude 的 `thinking.display`。
- Responses：`reasoning: {effort: high, summary: auto}`，保留 reasoning 内容，收齐完成响应才解析动作。
- Gemini Chat：通过 `extra_body.google.thinking_config` 发送 `thinking_level: high`、`include_thoughts: true`，不再同时发送 `reasoning_effort`。正式配置校验和 ModelWire 均支持该路径。保留完整 assistant 消息、reasoning_content 和供应商返回的扩展字段。
- 三种协议统一发送 `stream: true`；Chat 另外请求 `stream_options.include_usage: true`。收齐完成标志后再执行动作，保留分片工具参数、思考签名和累计 usage。读取超时仍为 120 秒，最多尝试三次；不改思考档位、输出上限或工具动作顺序。`model_response.stream_received` 标明实际响应是否为 SSE。
- 若 Chat 中转流对不同工具重复使用同一 index，按不同调用 ID 分开保存；索引冲突后缺少 ID 的分片无法确定归属时明确报错，不猜测或把两个动作拼成一个。
- 四组的截图、历史、工具、动作预算规则相同于既有基线。Agent3 每次回复只能提交一个 get_frames 或一个动作；Agent4 仍允许多个查询或动作序列，但不能混合查询与动作。

兼容接口接受某个参数，并不证明所有供应商内部具有完全相同的推理预算。Qwen/GLM 通过 Packy Messages 的 high 兼容请求沿用已验证路径，其底层映射由中转站实现。

## 密钥与启动

真实密钥不写进 YAML、源码、示例或报告。`api.key_env` 只保存环境变量名；指定后只读取该变量，缺失时在 CLI 启动 VM 前报错，不回退到其他模型的 `PACKY_API_KEY`。

在同一个 Bash 终端按需输入对应组密钥（输入不回显）：

```bash
read -r -s -p 'Packy 通用组 key: ' PACKY_COMMON_API_KEY
export PACKY_COMMON_API_KEY
read -r -s -p 'Packy Kimi key: ' PACKY_KIMI_API_KEY
export PACKY_KIMI_API_KEY
read -r -s -p 'Packy GLM/MiniMax key: ' PACKY_GLM_MINIMAX_API_KEY
export PACKY_GLM_MINIMAX_API_KEY
```

只运行其中一个模型时，只需设置它对应的变量。保留本机已配置的 HTTPS_PROXY/HTTP_PROXY 和访问 VM 所需的 NO_PROXY。

从仓库根目录启动，例如 Gemini Agent3：

```bash
python scripts/python/run_multienv.py \
  --agent_variant agent3 \
  --model gemini-3.8-flash \
  --run_id packy_v11_gemini_batch01 \
  --action_space computer_13 \
  --observation_type screenshot \
  --provider_name docker \
  --path_to_vm docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2 \
  --headless \
  --api_base_url https://www.packyapi.ai \
  --test_all_meta_path evaluation_examples/test_realtime_gui_bench.json \
  --max_steps 70 \
  --sleep_after_execution 0 \
  --environment_ready_wait_s 3 \
  --evaluation_settle_s 3 \
  --num_envs 1 \
  --result_dir results_four_agents
```

替换 `--model`、`--agent_variant` 即自动选择对应 YAML。依次运行各组，一次一个 VM。不要用 CLI 的 `--max_tokens` 改正式预算：Realtime 运行值来自 YAML。

Fable 5、Astra 的旧配置未指定 `key_env`，仍沿用 `PACKY_API_KEY` 优先、供应商环境变量兜底的兼容规则。

## 验证范围

新增配置的回归测试检查实际请求字段、四组能力隔离、查帧回图、历史和 reasoning 保留、密钥选择及原生图片/坐标不变。API 短测不等于完整游戏成功，也不等于所有 69×4×8 实验已运行。

参考：[Gemini 思考参数](https://ai.google.dev/gemini-api/docs/openai)、[Gemini 3.8 Flash 上限](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)、[Kimi Messages](https://platform.kimi.ai/docs/api/messages)、[MiniMax Messages](https://platform.minimax.io/docs/api-reference/text-chat-anthropic)、[DeepSeek Anthropic 兼容](https://api-docs.deepseek.com/guides/anthropic_api)。

流式协议参考：[OpenAI 工具调用分片](https://developers.openai.com/api/docs/guides/function-calling)、[OpenAI Responses 流式事件](https://developers.openai.com/api/docs/guides/streaming-responses)、[Claude Messages 流式事件](https://platform.claude.com/docs/en/build-with-claude/streaming)。

本次接入验证（2026-09-15）：实时相关回归 209 项通过；8 款模型各使用正式 Agent3 配置完成一次真实 API 查帧/回图/动作往返，共 16 次模型请求，Gemini 65536 和其他模型 128000 的请求上限均被接受。回图由静态原生截图回调提供，没有执行 VM 动作；坐标错误没有修正，也不将这次接口检查记为游戏成功。

流式更新验证（2026-09-16）：8 款实验模型使用 combine 配置完成真实流式查帧/回图/动作往返，16 份成功响应均实际为 SSE 并返回 usage；原始流离线重放与记录结果一致。Sol 的动作回复只有 CLICK，其余模型返回了动作序列；不把 Sol 这一次计为完整序列样本。以上仍是静态图片接口检查，不是完整游戏成绩。核查摘要见 `validation/realtime_final/streaming_review.json`。
