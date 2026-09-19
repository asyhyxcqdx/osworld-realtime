# 实时 GUI 基准：执行同学作业单

> 目标读者：负责跑批量实验的同学。**先读完本文件再动手**；更细的部署说明见
> [`SETUP_GUIDELINE_CN.md`](SETUP_GUIDELINE_CN.md)，AI 助手用的操作规程见 [`AGENTS.md`](AGENTS.md)。
> 工程侧的实现细节不用看，照本文件的命令做即可。

---

## 0. 一句话任务

在你们的服务器上，用 **combine（agent4）** 这一组 Agent，把下面 **5 个模型** 各自跑完 **69 个网页游戏任务**
（共 **345** 条记录），然后把每条记录填进飞书结果总表：

```
qwen3.8-max-0902, deepseek-flash, kimi-k3, glm-5.3-flash, MiniMax-M3
```

其余模型（`claude-sonnet-5`、`gpt-5.6-sol`、`gemini-3.8-flash` 等）**由我们这边跑**，你不用管；
飞书总表是两边共用的，各填各自的行即可。

判分口径只有两个：`pass@1` 和 `pass@3`，单任务得分 = `pass@3`。

---

## 1. 你会拿到什么

| 内容 | 位置 |
|---|---|
| 代码 | GitHub 公开仓库 `https://github.com/asyhyxcqdx/osworld-realtime`（`git clone` 即可，无需权限） |
| 虚拟机镜像（22.8 GiB） | `hf download bright-star123/osworld-realtime-vm --repo-type dataset Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2 --local-dir docker_vm_data`（公开 dataset 仓库） |
| 结果总表 | 飞书多维表格：<https://ycnp9ghuv61a.feishu.cn/wiki/IxhzwkR3cih15Dk86sJcx74ln7f?table=tblhBdTpZMEqX5Qh&view=vew5MRmeHl>（**16 列已建好，不要改列名**）；表格标识：base `DVwrbns4LaLi8oswq9XcTdlHnGf`、table `tblhBdTpZMEqX5Qh` |
| 模型密钥 | 你们公司网关自己的 key，不用给别人 |
| 你要跑的模型 | 只跑 `qwen3.8-max-0902`、`deepseek-flash`、`kimi-k3`、`glm-5.3-flash`、`MiniMax-M3` 这 5 个；其余模型我们来 |

镜像下载后**必须核对大小**：`24493359104` 字节（`ls -l` 看一下，差一点就是没下完）。

---

## 2. 机器要求

| 项 | 要求 |
|---|---|
| 系统 | Linux，`ls -l /dev/kvm` 必须存在（没有 KVM 会慢到不可用） |
| 磁盘 | ≥ 200 GB（镜像 23 GB + 345 条任务的截图/录像/轨迹，录像很占地方） |
| CPU / 内存 | 建议 ≥ 8 核 / 32 GB；`--num_envs N` 表示同时开 N 台虚拟机 |
| 网络 | 能访问你们公司的模型网关 |

---

## 3. 装环境（一次）

```bash
git clone <仓库地址> && cd osworld-realtime

uv sync                                  # 或 pip install -r requirements.txt
pip install pytest imageio-ffmpeg huggingface_hub   # huggingface_hub 提供下面的 hf 命令
docker pull happysixd/osworld-docker
ls -l /dev/kvm                           # 必须有输出

hf download bright-star123/osworld-realtime-vm --repo-type dataset \
  Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2 --local-dir docker_vm_data
ls -l docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2   # 应为 24493359104
```

> `uv sync` 如果报 `mm_agents/surferH/agp_client does not appear to be a Python project`，那是上游 submodule 没初始化：改用 `pip install -r requirements.txt` 即可，实时基准不需要那个包。
> 国内下载慢可以加 `HF_ENDPOINT=https://hf-mirror.com`（见 `.env.example` 第五节）。

---

## 4. 配置（一次，之后不用再输 key）

```bash
cp .env.example .env
chmod 600 .env
```

`.env` 只填三件事（`.env` 已被 git 忽略，不会被提交）：

**① 网关地址** —— 推荐用 `REALTIME_API_BASE_URL`，一个地址对全部协议生效：

```
REALTIME_API_BASE_URL=https://你们的网关地址
```

也可以按协议分开写：`ANTHROPIC_BASE_URL`（走 `/v1/messages`）、`OPENAI_BASE_URL`（走 `/v1/chat/completions`）。
**只写到网关根目录**，程序会自己补 `/v1/messages`、`/v1/chat/completions` 这些路径。

**② 模型 key** —— 所有模型共用一把就写这一行：

```
REALTIME_API_KEY=你们网关的key
```

想按模型分开计费/限流，就按 §8 的 `api.key_env` 分别填。

> **一把 key 服务全部模型没问题**，两种写法都支持：只填 `REALTIME_API_KEY` 时 5 个模型都用它；
> 也可以按模型分别填各自的变量名。
>
> 命令里的 `--exclusive-keys-confirmed` 确认的**不是"一个模型一把 key"**，而是
> **跑批这段时间没有别人也在用这把 key**——别人同时用会撞限流（任务被打断就成了"无有效成绩"），
> 以后要按网关账单核对金额时也会把别人的流量混进来。一个 key 跑全部模型时，这一条更要确认。

**③ 代理**（可选；走代理时**虚拟机地址必须排除**，否则连不上虚拟机）：

```
HTTPS_PROXY=http://127.0.0.1:7890
NO_PROXY=localhost,127.0.0.1,::1
```

配好后**不需要再传任何 key 参数**；批量脚本会自动从这里读。

---

## 5. 第一步：冒烟（1 个模型 × 1 个任务）

```bash
python scripts/python/run_realtime_batch.py \
  --agent_variant agent4 --models deepseek-flash \
  --run_id smoke_$(date +%Y%m%d) \
  --task 5169e1b0-1a7d-538b-8e59-8785c39460ce \
  --skip-billing --exclusive-keys-confirmed
```

约 5–20 分钟。跑完在结果目录里打开 `trajectory.html`（浏览器直接打开，离线单文件），
确认**每轮都有截图、动作和模型回复**。看不到截图或全是报错就先别往下跑。

> `--skip-billing` 表示不查 Packy 那套账单接口。用公司网关时它是**必须**的
> （程序检测到不是 packyapi.ai 也会自动跳过，传上更明确）。

---

## 6. 第二步：单模型全量（1 个便宜模型 × 69 任务）

单价表用仓库里的模板，按你们网关的单价改（单位：**美元 / 百万 token**；`"*"` 是兜底，有缓存折扣就加 `cached_input_per_mtok`）：

```bash
cp prices.example.json prices.json
```

```bash
python scripts/python/run_realtime_batch.py \
  --agent_variant agent4 --models deepseek-flash \
  --run_id batch01 --result_dir results_realtime_batches \
  --num_envs 4 --keep-going \
  --skip-billing --prices prices.json \
  --exclusive-keys-confirmed
```

跑完检查：

```bash
ls -d results_realtime_batches/deepseek-flash/batch01/agent4/computer_13/screenshot/realtime_gui_bench/*/ | wc -l   # 应为 69
cat results_realtime_batches/_cost/batch01/cost_report.json
```

**69 条里每条都应该有 `result.txt`。** 现在只有**基础设施故障**（网络/VM/评分异常）才会没有 `result.txt`，属于"无有效成绩"；**模型违反动作协议**（例如连续 3 轮不回工具调用、越权刷新）会写成 `result.txt=0.0`（有效 0 分，`termination_reason: run_error`），按失败计入。
不算 0 分，记下来重跑即可。

---

## 7. 第三步：五个模型全量

同一条命令，`--models` 指定你要跑的这 5 个（**必须显式写**，不要用默认值——默认是 10 个模型）：

```bash
python scripts/python/run_realtime_batch.py \
  --agent_variant agent4 \
  --models qwen3.8-max-0902,deepseek-flash,kimi-k3,glm-5.3-flash,MiniMax-M3 \
  --run_id batch01 --result_dir results_realtime_batches \
  --num_envs 4 --keep-going \
  --skip-billing --prices prices.json \
  --exclusive-keys-confirmed
```

**耗时与费用**

| 项 | 参考值 |
|---|---|
| 单模型 69 任务 | 约 2–6 小时（`--num_envs 4`，取决于任务难度和模型速度） |
| 你的这 5 个模型 | 约 1 天（模型是**顺序**跑的，同一时刻只有一个模型在跑） |

C1 单任务实测花费（仅作记录口径参考，不用于限制跑量）：`gpt-5.6-sol` 22.53、`claude-sonnet-5` 10.40、
`kimi-k3` 2.65、`qwen3.8-max-0902` 0.87、`gemini-3.8-flash` 0.24、`deepseek-flash` 0.21、
`glm-5.3-flash` 0.14、`MiniMax-M3` 0.03（美元；sol 那次含一轮退化刷屏，kimi 含一次中断重跑）。

费用不设限，按你们网关的实际消耗如实记录进 `成本` 列即可；需要的话可以用 `--prices` 先算，
或者拿到网关账单后，导出时用 `--charges <cost_report.json>` 覆盖（这是导出脚本的参数，不是跑批量的参数）。

**模型顺序**可以按你们网关的可用性和配额自行安排；`--models` 写几个就跑几个，之后补齐用同样的
`--run_id` 再跑一次（已完成的会自动跳过）。

**中途断了怎么办**：把**同一条命令、同样的 `--run_id` 和 `--result_dir`** 再执行一次即可。
已有成绩的任务会自动跳过，没有成绩的会被清空重跑（`trajectory.jsonl` 是追加模式，不清会新旧混写）。

**并发不要盲目调大**：`--num_envs 8` 就是 8 台虚拟机同时跑，瓶颈通常是 CPU/内存和你们网关的限流。

---

## 8. 如果你们的模型名跟我们不一样

**名字能对上就不要动任何配置**，直接跑 §5–§7 的命令即可。只有网关里的模型名和我们不同，才需要复制一份 `configs/realtime_agents/combine-<模型名>.yaml`，改三个字段：

```yaml
api:
  model: <网关里真实的模型名>          # 必须和网关的 model 名逐字一致
  protocol: openai_chat                # 或 anthropic_messages / openai_responses
  key_env: COMPANY_API_KEY             # 想单独配 key 就换个变量名，然后在 .env 里填
```

然后 `--models <模型名>` 用新名字即可（YAML 必须存在，名字要和文件名后缀一致）。

校验器对个别模型名有硬编码规则（gemini / MiniMax 特例），如果报
`thinking effort must be one of ...` 或 `Chat thinking is currently supported only for gemini-3.8-flash`，
按报错调整 `api.thinking.effort`，或把这个报错原文发给负责人。

---

## 9. 导出结果并写入飞书

### 先装飞书 CLI（一次）

写入用官方 CLI [`@larksuite/cli`](https://www.npmjs.com/package/@larksuite/cli)（需要 Node.js）：

```bash
npm install -g @larksuite/cli
lark-cli --version
lark-cli auth login          # 浏览器里完成授权；按提示打开链接即可
lark-cli auth status         # 确认 user 身份为 valid
```

授权时至少要包含多维表格读写权限（`base:record:read`、`base:record:create`）和 `wiki:node:retrieve`。
`lark-cli` 会把自己的 token 存在本机，之后不用重复登录。

### 再导出并写入

结果总表：<https://ycnp9ghuv61a.feishu.cn/wiki/IxhzwkR3cih15Dk86sJcx74ln7f?table=tblhBdTpZMEqX5Qh&view=vew5MRmeHl>

```bash
# 先干跑，确认要写进去的内容
python scripts/python/export_realtime_results.py \
  --result_dir results_realtime_batches --run_id batch01 --prices prices.json \
  --lark-base-token DVwrbns4LaLi8oswq9XcTdlHnGf --lark-table-id tblhBdTpZMEqX5Qh \
  --lark-dry-run

# 确认无误后去掉 --lark-dry-run 真正写入
python scripts/python/export_realtime_results.py \
  --result_dir results_realtime_batches --run_id batch01 --prices prices.json \
  --lark-base-token DVwrbns4LaLi8oswq9XcTdlHnGf --lark-table-id tblhBdTpZMEqX5Qh
```

不想用 CLI 也可以：去掉 `--lark-*` 只生成 CSV，在飞书表里用「导入」把 CSV 贴进去——
CSV 就是表头的 16 列（utf-8-sig 编码，Excel 直接可开）；同名 `.json` 里还多带 `task_id`/`run_id` 便于追溯。

**⚠️ 不要重复导入同一个 run**：写入是"新增记录"，不是覆盖，重复执行会多出一倍行。
要重导就先在飞书里删掉旧行。

**16 列的口径**

| 列 | 来源与口径 |
|---|---|
| benchmark_id | 游戏编号（如 C1），来自 `result.json`；无成绩时用任务配置里的编号 |
| agent | `agent4`（combine） |
| model / effort | 配置里的模型名 / 思考档位 |
| step | 动作决策回合数（`agent_metrics.action_decisions`） |
| 模型请求数 | 逻辑请求次数（含查帧与格式纠正），不等于底层 HTTP 次数 |
| 执行动作数 | 实际执行的动作条数（含 WAIT/DONE） |
| 工具调用数 | 轨迹里所有已解析的工具调用（含后来被拒绝的） |
| result | `yes` / `no`；**基础设施故障导致的无有效成绩才留空**（模型协议违规会写 `no` 并计 0 分） |
| attempts_completed | 已结算的游戏机会数（成功失败都算，进行中的不算） |
| pass@1 / pass@3 | 第一次 / 三次内是否成功，0 或 1；基础设施故障导致的无有效成绩留空 |
| 结束状态 | `done`→正常结束、`decision_limit`→回合上限、`execution_error`→执行异常、`run_error`→其他运行异常、`interrupted`→中断 |
| 成本 | token × `prices.json` 单价（美元）。导出时给 `--charges <cost_report.json>` 则改用账单原值 |
| 输入/输出Token数量 | 逐条模型回复的用量求和。**注意**：流中断的请求可能没有记录，会比网关统计略低 |

---

## 10. 出问题怎么办

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动就报缺 key | `.env` 没填或变量名不对 | 对照 §4；跑一次带 `--dry-run` 的命令看解析结果 |
| `KEY_SOURCE` 打印出来是 `stdin` | `.env` 里没有对应变量 | 补 `.env`；`stdin` 表示要你手动粘贴 key |
| 某个任务没有 `result.txt` | 基础设施故障（API/网络/VM/评分异常）→ **无有效成绩，不是 0 分**；模型协议违规会写 `result.txt=0.0`（有效 0 分） | 记下任务 id，重跑同一条命令；判分时和真正的 0 分区分开 |
| 单轮花费特别大 | 模型退化刷屏直到撞上 128k 输出上限，那轮照样计费 | 正常现象，照实记录该轮 token 与费用即可，不擅自改配置或削减预算 |
| 跑一半整批中断 | 本机代理瞬断 | 账单抓取失败已经被降级为 `billing_error` 不会中断；模型请求失败会让该任务"无有效成绩"。断点续跑即可 |
| 模型"来不及操作" | 环境是**实时**的，模型思考时游戏在继续跑 | 这是被测对象的行为，不用干预；照原样跑，不要改任何配置 |
| 虚拟机连不上/超时 | 代理把虚拟机地址也代理了 | `NO_PROXY` 必须包含 `localhost,127.0.0.1,::1` 和虚拟机 IP |
| `get_frames` 返回 `not_ready` | 请求的时间比已录完的片段新 | 正常现象，不是错误 |
| 两次导入飞书，行数翻倍 | 写入是新增不是覆盖 | 在飞书里删掉重复行，只导一次 |

> 这里只列同学最可能遇到的几种；完整的坑与约定见 [AGENTS.md](AGENTS.md) 第 6 节。

---

## 11. 交付验收清单

- [ ] 345 个任务目录（5 模型 × 69 任务），每个目录有 `result.txt` 或有明确的"无有效成绩"说明
- [ ] `results_realtime_batches/_cost/batch01/cost_report.json` 存在，逐模型金额齐全
- [ ] `export_batch01.csv` / `.json` 生成，行数 = 345
- [ ] 飞书总表里你负责的这 5 个模型共 345 行，`成本`、`输入Token数量`、`输出Token数量` 三列有值
- [ ] 结果目录打包成 zip（体积大，用 `zip -r` 或 `tar`），连同 `cost_report.json` 一起交回
