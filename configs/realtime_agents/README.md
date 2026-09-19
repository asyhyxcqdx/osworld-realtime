# Realtime 模型配置

10 款模型（`configs/realtime_agents/` 下的 10 个模型名），每款都有 vanilla / anticipatory / video / combine 四份 YAML，目录合计 40 份，**system prompt 四组各一套**（同一组内 10 份完全相同）。

| model（区分大小写） | 协议 | 上下文声明 | 输出上限 | 思考 | 密钥环境变量 |
|---|---|---:|---:|---|---|
| `claude-sonnet-5` | anthropic_messages | 1000000 | 128000 | high | `PACKY_CLAUDE_SONNET_5_API_KEY` |
| `gpt-5.6-sol` | openai_responses | 1000000 | 128000 | high | `PACKY_GPT_5_6_SOL_API_KEY` |
| `gemini-3.8-flash` | openai_chat | 1000000 | **65536** | high | `PACKY_GEMINI_3_8_FLASH_API_KEY` |
| `qwen3.8-max-0902` | anthropic_messages | 1000000 | 128000 | high 兼容请求 | `PACKY_QWEN3_8_MAX_0902_API_KEY` |
| `kimi-k3` | anthropic_messages | 1000000 | 128000 | high | `PACKY_KIMI_K3_API_KEY` |
| `deepseek-flash` | anthropic_messages | 1000000 | 128000 | high | `PACKY_DEEPSEEK_FLASH_API_KEY` |
| `glm-5.3-flash` | anthropic_messages | 1000000 | 128000 | high 兼容请求 | `PACKY_GLM_5_3_FLASH_API_KEY` |
| `MiniMax-M3` | anthropic_messages | 1000000 | 128000 | **adaptive，无 effort 档位** | `PACKY_MINIMAX_M3_API_KEY` |
| `claude-fable-5` | anthropic_messages | 1000000 | 128000 | high | `PACKY_CLAUDE_FABLE_5_API_KEY` |
| `gpt-6-astra` | openai_responses | 1000000 | 128000 | high | `PACKY_GPT_6_ASTRA_API_KEY` |

`Claude-sonnet-5.0` 不是此处的 API ID。GLM 5.3（非 Flash）只有文本输入、Seed 2.1 Turbo 未确认可用，均未加入截图 Agent 配置。

上下文声明统一为 1000000，目前只用于配置校验，不发送给 API，也不据此在本地裁剪历史；不宣称它是各模型已经实测的真实上限。实际输入保留完整历史，服务端判断是否超限。输出上限则随请求实际发送；达到上限可能截断回答，思考 token 也可能占用输出预算。收到明确的 length/max_tokens/incomplete 结束状态时，不执行其中的部分动作。

另外两点容易误解：

- `temperature` 在全部 40 份里都是 `null`（统一不传，走各家服务端的默认值），而且**当前所有配置都开着 thinking**，两条路径（Messages、Gemini Chat）都会把它从请求里去掉、Responses 路径本来就不带它 —— 所以这个字段对实际请求**没有影响**，只是保留在 schema 里。
- `observation.historical_video.max_queries_per_turn: 0` 表示**不限制每回合查询次数**（不是"禁止查询"）。40 份都写 0；有历史帧能力的组才能写非 0，无历史帧能力的组必须是 0。

## 公共任务策略

所有 40 份 YAML 的 `system_prompt` **只放反作弊与评测诚信规则**（`# Anti-Cheating and Evaluation-Integrity Rules (Highest Priority)`，四个小节：Allowed Behavior / Forbidden Behavior / Evidence and Completion / Violations；禁止开发者工具/CDP/页内脚本、读写源码与存档、直连网络、终端与外部程序、刷新与导航、伪造结果等）。两套文本有两处不同，都源于能力差异：`video` / `combine` 多一句允许用 `get_frames` 查历史帧；`vanilla` / `anticipatory` 没有查帧工具，所以那条"只通过可见 GUI 元素交互"写的是"**每次工具结果带回的当前截图**、鼠标移动、点击、拖拽、键盘输入、滚动、等待"，不提 frames 与观察工具（对它们是空头支票）。

原来的操作说明（自称句、动作时序、"截图未等待重绘"、探索与谨慎策略、一次 1 个还是 1–100 个动作、`get_frames` 的使用协议、`computer_done` 的调用要求、不要用纯文本回复）**已从 system prompt 移除**，改由新增的 `user_prompt` 承载：每个变体一套（`vanilla` 64 行 / `anticipatory` 65 行 / `video` 68 行 / `combine` 69 行），内容为角色与目标、Real-Time Constraints、Success-Oriented Execution Policy（14 条）、Tools and Perception、Rules and Completion；`video` / `combine` 才提 `get_frames`，`anticipatory` / `combine` 才写"可以一次提交多个动作"。它**替换掉任务配置里那句通用 instruction**，作为对话开头的唯一一条任务 user 消息（所有 69 个任务的 instruction 本来就是同一句话）。

YAML 的 `system_prompt` 仍是唯一 prompt 来源：`RealtimeAgent` 必须收到它，缺失或为空时直接报错，代码不再内置默认 prompt；**坐标说明不在 YAML 里**，由 `api.coordinate_system` 在运行时追加在整段 prompt 之后（`realtime_agent.py` 的 `self.system = system_prompt_text + "\n" + coordinates.guidance`）。四处操作说明的原文可在旧提交里取回：`git show HEAD~1:configs/realtime_agents/vanilla-deepseek-flash.yaml`。

在时序说明之后，统一要求先探索环境和游戏机制，通过观察与谨慎试探发现界面未说明的细节；不可逆、无法返回当前状态或会消耗 attempt 的操作须先获取足够信息，关键操作有把握后再执行，尤其珍惜最后一次尝试。

这段策略只放在 system prompt，开头的 Task 说明保持原样。观察和试探限于各 Agent 已有工具，不增加强制等待、强制查询次数或新的执行条件；四组的单动作/动作序列与历史帧能力保持原有区别。新运行自动使用更新后的 prompt，并保存完整实际文字到 `system_prompt.txt`；已有轨迹保留当时的 prompt。

## 协议和坐标适配

所有模型原样接收 1920×1080 PNG，VM 统一执行原生像素坐标。`api.coordinate_system` 同时控制追加的坐标 prompt、模型可见工具 schema、回复校验与动作转换：

| 模型 | 配置值 | 模型输出 x/y | 交给 VM |
|---|---|---|---|
| Gemini 3.8 Flash | `normalized_0_1000` | 0–1000 整数 | `floor(x*1920/1000)`、`floor(y*1080/1000)`；合法端点 1000 落到最后一个屏幕像素 |
| MiniMax M3 | `normalized_0_1000_unclipped` | 0–1000 整数 | 按上游 `int(x*1920/1000)`、`int(y*1080/1000)` 直接换算；1000 分别得到 1920、1080，不额外截边 |
| Claude Sonnet 5、Sol、Qwen、Kimi、DeepSeek、GLM，以及 Fable/Astra | `native_pixels`（缺省值） | 原生像素 | 原样传递 |

Gemini 和 MiniMax 的四组 YAML 均显式选择对应协议；两者输出范围相同，但端点处理分别跟随各自上游实现。Qwen 按本项目原生坐标实测结果保持现状。没有图片预缩放，也不根据某次返回值大小猜测坐标单位。相对协议不接受超范围、非整数的 x/y，沿用现有整段拒绝与工具错误纠正流程，不执行有效前缀。键盘、WAIT、滚动量、动作数量与历史查询规则不变。`tool_format: native` 指 API 原生工具调用，与坐标单位是两回事。

实际 prompt 保存于 `system_prompt.txt`，协议保存于 `experiment.json` 和 `model_request.coordinate_system`。原始回复/工具参数保留在 `model_response`，转换后的提交和执行动作保留在 `action_submitted` / `action_executed`；不会把像素值写回 assistant 历史。正常执行后的模型工具反馈只包含该调用的动作类型、现有状态字段及自身开始/结束/耗时，不回显 VM 坐标、不附整段 `sequence_actions`，因此不再添加 `execution_coordinate_system`。完整 VM 参数及原精度时间仍保留在 `action_executed.info.sequence_actions`。HTML 在原生图片上按记录的协议绘制模型坐标，工具参数仍展示原始值。

- Messages：保留完整 thinking/tool_use/tool_result。MiniMax M3 只发送 `thinking: {type: adaptive}`，不发送 `output_config.effort` 或 Claude 的 `thinking.display`。
- Responses：`reasoning: {effort: high, summary: auto}`，保留 reasoning 内容，收齐完成响应才解析动作。
- Gemini Chat：通过 `extra_body.google.thinking_config` 发送 `thinking_level: high`、`include_thoughts: true`，不再同时发送 `reasoning_effort`。正式配置校验和 ModelWire 均支持该路径。保留完整 assistant 消息、reasoning_content 和供应商返回的扩展字段。
- 三种协议统一发送 `stream: true`；Chat 另外请求 `stream_options.include_usage: true`。收齐完成标志后再执行动作，保留分片工具参数、思考签名和累计 usage。读取超时仍为 120 秒，最多尝试三次；不改思考档位、输出上限或工具动作顺序。`model_response.stream_received` 标明实际响应是否为 SSE。
- 若 Chat 中转流对不同工具重复使用同一 index，按不同调用 ID 分开保存；索引冲突后缺少 ID 的分片无法确定归属时明确报错，不猜测或把两个动作拼成一个。
- 四组的截图、历史、工具、动作预算规则相同于既有基线。Agent3 每次回复只能提交一个 get_frames 或一个动作；Agent4 仍允许多个查询或动作序列，但不能混合查询与动作。

兼容接口接受某个参数，并不证明所有供应商内部具有完全相同的推理预算。Qwen/GLM 通过 Packy Messages 的 high 兼容请求沿用已验证路径，其底层映射由中转站实现。

## 密钥与启动

真实密钥不写进 YAML、源码、示例或报告。`api.key_env` 只保存环境变量名；Agent 层只读这一个变量，缺失即报错。**批量入口会多兜一层**：`run_realtime_batch.py` 按 `api.key_env` → `REALTIME_API_KEY` → `PACKY_API_KEY` 取第一把非空 key，再把它注入到该模型自己的变量名里，所以临时共用一把 key 时也能跑。

**推荐做法**：在仓库根目录 `cp .env.example .env`（`.env` 已被 git 忽略），把上面表格里的变量名和 key 填进去。批量脚本和 `run_multienv.py` 会自动加载它，之后不需要再传任何 key 参数；所有模型共用一把 key 时只填 `REALTIME_API_KEY` 即可兜底。换非 Packy 网关时在同一个文件里设 `REALTIME_API_BASE_URL`（或按协议设 `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL`）。

也可以临时在终端里按需输入对应组密钥（输入不回显）：

```bash
# 变量名逐个对应模型；只想跑哪几个就 export 哪几个。
read -r -s -p 'claude-sonnet-5 key: ' PACKY_CLAUDE_SONNET_5_API_KEY; export PACKY_CLAUDE_SONNET_5_API_KEY
read -r -s -p 'gpt-5.6-sol key: ' PACKY_GPT_5_6_SOL_API_KEY; export PACKY_GPT_5_6_SOL_API_KEY
read -r -s -p 'gemini-3.8-flash key: ' PACKY_GEMINI_3_8_FLASH_API_KEY; export PACKY_GEMINI_3_8_FLASH_API_KEY
read -r -s -p 'qwen3.8-max-0902 key: ' PACKY_QWEN3_8_MAX_0902_API_KEY; export PACKY_QWEN3_8_MAX_0902_API_KEY
read -r -s -p 'kimi-k3 key: ' PACKY_KIMI_K3_API_KEY; export PACKY_KIMI_K3_API_KEY
read -r -s -p 'deepseek-flash key: ' PACKY_DEEPSEEK_FLASH_API_KEY; export PACKY_DEEPSEEK_FLASH_API_KEY
read -r -s -p 'glm-5.3-flash key: ' PACKY_GLM_5_3_FLASH_API_KEY; export PACKY_GLM_5_3_FLASH_API_KEY
read -r -s -p 'MiniMax-M3 key: ' PACKY_MINIMAX_M3_API_KEY; export PACKY_MINIMAX_M3_API_KEY
read -r -s -p 'claude-fable-5 key: ' PACKY_CLAUDE_FABLE_5_API_KEY; export PACKY_CLAUDE_FABLE_5_API_KEY
read -r -s -p 'gpt-6-astra key: ' PACKY_GPT_6_ASTRA_API_KEY; export PACKY_GPT_6_ASTRA_API_KEY
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

40 份配置都显式写了 `api.key_env`，而且是**一个模型一个变量**：变量名 = `PACKY_` + 模型名（全大写，非字母数字换成 `_`）+ `_API_KEY`。10 款模型对应 10 个变量，没有两款模型默认共用同一把 key；某个模型自己的变量没值时，才回退 `REALTIME_API_KEY` → `PACKY_API_KEY`。

## 验证范围

配置回归测试检查实际请求字段、四组能力隔离、查帧回图、历史和 reasoning 保留、密钥选择、原图字节不变及各坐标协议的执行结果。**接口短测不等于完整游戏成绩。**

参考：[Gemini 思考参数](https://ai.google.dev/gemini-api/docs/openai)、[Gemini 3.8 Flash 上限](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)、[Kimi Messages](https://platform.kimi.ai/docs/api/messages)、[MiniMax Messages](https://platform.minimax.io/docs/api-reference/text-chat-anthropic)、[DeepSeek Anthropic 兼容](https://api-docs.deepseek.com/guides/anthropic_api)。

流式协议参考：[OpenAI 工具调用分片](https://developers.openai.com/api/docs/guides/function-calling)、[OpenAI Responses 流式事件](https://developers.openai.com/api/docs/guides/streaming-responses)、[Claude Messages 流式事件](https://platform.claude.com/docs/en/build-with-claude/streaming)。

历史接口检查（2026-09-15/16）：八款模型各用正式配置完成过一次真实的流式查帧/回图/动作往返，均实际按 SSE 返回并带 usage；这是静态图片的接口检查，没有执行 VM 动作，不作为游戏成绩。

## 官方依据（坐标协议）

[Gemini 官方 Computer Use 文档](https://ai.google.dev/gemini-api/docs/computer-use) 的动作参数表写 `x/y: int (0-999)`，示例转换为 `int(x/1000*screen_width)`，说明段落用的是 0–1000。原 OSWorld 工具描述也是 0–999。本项目对照 OSWorld-V2 后采用与其 prompt、工具描述、解析器一致的 **0–1000**，分母固定 1000；[MiniMax 官方 M3 报告](https://www.minimax.io/blog/minimax-m3) 的 OSWorld-Verified 方法写明 “relative coordinates 0–1000, image resolution 1920×1080”。两个模型都保持原始 PNG 字节，不转 JPEG。

OSWorld-V2 源码对照（固定提交 `627a1d691fbd0fd93b6161ecafa81530d50f6138`）：

- Gemini：[工具 x/y 描述](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/gemini_agent.py#L54)、[system prompt](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/gemini_agent.py#L274)、[坐标校验与换算](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/gemini_action_parser.py#L65)：0–1000 除以 1000，端点限定到 `width-1` / `height-1`。
- MiniMax：[坐标 prompt 与工具格式](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/m3/prompts.py#L49)、[坐标换算](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/m3/parser.py#L284)、[图片编码](https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/m3/agent.py#L105)：0–1000 相对整数乘原图尺寸，**不加端点截边**，`(1000,1000)` 传到执行器即 `(1920,1080)`。本项目的换算与上游 `scaled_xy` 在 x/y 两轴逐整数比对一致。

借鉴的只有坐标说明、工具坐标语义和确定性换算。上游 Gemini 用 Google SDK、M3 用 XML 文本工具和 PyAutoGUI 代码，还带历史图片裁剪和桌面任务规则；本项目继续使用 Packy 已验证的 Chat / Messages 注册工具调用、完整历史和 `computer_13` 执行器，不引入上游的 shell、网页导航、FAIL 或额外等待，也不继承上游对数字字符串、非整数、损坏动作名的宽松修复。坐标单位由配置显式声明，不按返回值猜；这部分只改宿主模型层，不需要重打镜像。
