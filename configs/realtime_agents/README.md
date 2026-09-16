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

## 公共任务策略

所有 40 份 YAML 的 `system_prompt` 在当前截图与任务上下文说明之后，统一要求先探索环境和游戏机制，通过观察与谨慎试探发现界面未说明的细节；不可逆、无法返回当前状态或会消耗 attempt 的操作须先获取足够信息，关键操作有把握后再执行，尤其珍惜最后一次尝试。直接构造 Agent 时使用的默认 prompt 也包含同样要求。

这段策略只放在 system prompt，开头的 Task 说明保持原样。观察和试探限于各 Agent 已有工具，不增加强制等待、强制查询次数或新的执行条件；四组的单动作/动作序列与历史帧能力保持原有区别。新运行自动使用更新后的 prompt，并保存完整实际文字到 `system_prompt.txt`；已有轨迹保留当时的 prompt。

## 协议和坐标适配

所有模型原样接收 1920×1080 PNG，VM 统一执行原生像素坐标。`api.coordinate_system` 同时控制追加的坐标 prompt、模型可见工具 schema、回复校验与动作转换：

| 模型 | 配置值 | 模型输出 x/y | 交给 VM |
|---|---|---|---|
| Gemini 3.8 Flash | `normalized_0_1000` | 0–1000 整数 | `floor(x*1920/1000)`、`floor(y*1080/1000)`；合法端点 1000 落到最后一个屏幕像素 |
| MiniMax M3 | `normalized_0_1000_unclipped` | 0–1000 整数 | 按上游 `int(x*1920/1000)`、`int(y*1080/1000)` 直接换算；1000 分别得到 1920、1080，不额外截边 |
| Claude Sonnet 5、Sol、Qwen、Kimi、DeepSeek、GLM，以及 Fable/Astra | `native_pixels`（缺省值） | 原生像素 | 原样传递 |

Gemini 和 MiniMax 的四组 YAML 均显式选择对应协议；两者输出范围相同，但端点处理分别跟随各自上游实现。Qwen 按本项目原生坐标实测结果保持现状。没有图片预缩放，也不根据某次返回值大小猜测坐标单位。相对协议不接受超范围、非整数的 x/y，沿用现有整段拒绝与工具错误纠正流程，不执行有效前缀。键盘、WAIT、滚动量、动作数量与历史查询规则不变。`tool_format: native` 指 API 原生工具调用，与坐标单位是两回事。

实际 prompt 保存于 `system_prompt.txt`，协议保存于 `experiment.json` 和 `model_request.coordinate_system`。原始回复/工具参数保留在 `model_response`，转换后的提交和执行动作保留在 `action_submitted` / `action_executed`；不会把像素值写回 assistant 历史。VM 执行反馈可能包含动作坐标，相对协议的工具结果会标注 `execution_coordinate_system: native_pixels`。HTML 在原生图片上按记录的协议绘制模型坐标，工具参数仍展示原始值。

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
  --max_steps 100 \
  --sleep_after_execution 0 \
  --environment_ready_wait_s 3 \
  --evaluation_settle_s 3 \
  --num_envs 1 \
  --result_dir results_four_agents
```

替换 `--model`、`--agent_variant` 即自动选择对应 YAML。依次运行各组，一次一个 VM。不要用 CLI 的 `--max_tokens` 改正式预算：Realtime 运行值来自 YAML。

Fable 5、Astra 的旧配置未指定 `key_env`，仍沿用 `PACKY_API_KEY` 优先、供应商环境变量兜底的兼容规则。

## 验证范围

新增配置的回归测试检查实际请求字段、四组能力隔离、查帧回图、历史和 reasoning 保留、密钥选择、原图字节不变及各坐标协议的执行结果。API 短测不等于完整游戏成功，也不等于所有 69×4×8 实验已运行。

参考：[Gemini 思考参数](https://ai.google.dev/gemini-api/docs/openai)、[Gemini 3.8 Flash 上限](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)、[Kimi Messages](https://platform.kimi.ai/docs/api/messages)、[MiniMax Messages](https://platform.minimax.io/docs/api-reference/text-chat-anthropic)、[DeepSeek Anthropic 兼容](https://api-docs.deepseek.com/guides/anthropic_api)。

流式协议参考：[OpenAI 工具调用分片](https://developers.openai.com/api/docs/guides/function-calling)、[OpenAI Responses 流式事件](https://developers.openai.com/api/docs/guides/streaming-responses)、[Claude Messages 流式事件](https://platform.claude.com/docs/en/build-with-claude/streaming)。

本次接入验证（2026-09-15）：实时相关回归 209 项通过；8 款模型各使用正式 Agent3 配置完成一次真实 API 查帧/回图/动作往返，共 16 次模型请求，Gemini 65536 和其他模型 128000 的请求上限均被接受。回图由静态原生截图回调提供，没有执行 VM 动作；坐标错误没有修正，也不将这次接口检查记为游戏成功。

流式更新验证（2026-09-16）：8 款实验模型使用 combine 配置完成真实流式查帧/回图/动作往返，16 份成功响应均实际为 SSE 并返回 usage；原始流离线重放与记录结果一致。Sol 的动作回复只有 CLICK，其余模型返回了动作序列；不把 Sol 这一次计为完整序列样本。以上仍是静态图片接口检查，不是完整游戏成绩。核查摘要见 `validation/realtime_final/streaming_review.json`。

## 官方依据与验证边界（2026-09-16）

[Gemini 官方 Computer Use 文档](https://ai.google.dev/gemini-api/docs/computer-use) 的动作参数表为 `x/y: int (0-999)`，示例转换为 `int(x/1000*screen_width)`；说明段落也使用 0–1000/1000×1000 的表述。原 OSWorld 工具描述也是 0–999；本次进一步对照 OSWorld-V2，采用其 prompt、工具描述和解析器一致的 **0–1000**，将最初 Gemini 配置的上限从 999 扩到 1000，转换分母始终为 1000。[MiniMax 官方 M3 报告](https://www.minimax.io/blog/minimax-m3) 的 OSWorld-Verified 方法说明为 “M3 uses relative coordinates 0–1000, image resolution 1920×1080”；OSWorld 的 M3 prompt 也明确该范围为整数。

OSWorld-V2 源码对照（固定提交 `627a1d691fbd0fd93b6161ecafa81530d50f6138`）：

- Gemini：[工具 x/y 描述](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/gemini_agent.py#L54)、[system prompt](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/gemini_agent.py#L274)、[坐标校验与换算](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/gemini_action_parser.py#L65)。范围 0–1000、除以 1000，端点限制到 `width-1` / `height-1`；截图按原 PNG 字节提交。
- MiniMax：[坐标 prompt 与工具格式](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/m3/prompts.py#L49)、[坐标换算](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/m3/parser.py#L284)、[图片编码](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/m3/agent.py#L105)。0–1000 相对整数，除以 1000 乘原图尺寸；不改变图片分辨率，但默认转 JPEG，可设置 PNG。本项目保持原 PNG 字节。坐标换算按上游直接返回结果，已撤掉此前额外加入的端点截边；`(1000,1000)` 传给执行器的值为 `(1920,1080)`。

借鉴范围是坐标说明、工具坐标语义和确定性换算。上游 Gemini 使用 Google SDK，M3 使用 XML 文本工具及 PyAutoGUI 代码，还带有历史图片裁剪和通用桌面任务规则。本项目继续使用 Packy 已验证的 Chat / Messages 注册工具调用、完整历史和 `computer_13` 执行器，保持四组动作/查帧能力一致；不引入上游的 shell、网页导航、FAIL 或额外等待规则。相对协议的校验继续严格按本项目工具 schema 执行，不继承上游对数字字符串、非整数或损坏动作名称的宽松修复。

这些是官方 Computer Use/评测的坐标约定，不保证任意自定义工具会自动输出相同单位。本项目保留统一 `RealtimeAgent`、`computer_*` 工具能力和四组实验规则，通过模型配置明确单位，没有切换到 Google 内置 Computer Use，也没有引入其他 Agent 的规划策略。该改动位于宿主机模型层，无需重打 VM 镜像。

此前 Gemini 三张静态图在明确相对协议下均命中；当时测试上限为 1000，输出均在 0–999 内。MiniMax 旧 C1 的 `(517,670)` 按相对坐标可落到 Start，但其他静态测试仍有定位偏差。转换修复单位问题，不保证消除模型定位误差。历史 C1 和接口检查不会重记为本次适配后的成功；完整游戏还需单独复跑。

首轮坐标适配离线验证：Agent、模型配置、坐标、runner、HTML 轨迹和流式六组共 215 项测试通过。首次运行 214 项通过、浏览器测试因默认 Chromium 路径不可用跳过；显式指定本机已有 Chromium 后，该项也通过。HTML JavaScript 语法检查通过。覆盖正式 YAML → 模型请求 → 动作解码 → 模拟执行器 → 轨迹/HTML，验证原始回复不被改写、相对坐标只转换一次、其余模型保持原图/原生坐标、非法坐标不执行。

随后对照 OSWorld-V2，将 Gemini 上限补为 1000，并补充 prompt 中左右/上下端点的定义；当时模型配置、坐标链路和 HTML 相关 85 项回归通过，两个模型仍共用端点截边。这一历史结果不代表 MiniMax 当前的端点行为；按用户要求已去掉 MiniMax 额外截边，Gemini 保留其上游已有处理。此前两轮均没有付费 API 请求或实际 C1 复跑。

去掉 MiniMax 额外截边后，相关回归 97 项全部通过，覆盖四组配置、执行器入参、原始回复、日志与 HTML。另将 0–1000 的每个整数分别在 x/y 轴上与固定提交中的 M3 `scaled_xy` 函数对照，全部一致；没有付费 API 请求或 VM 实跑。
