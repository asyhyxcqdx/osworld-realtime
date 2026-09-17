# Realtime GUI 项目入口

项目基于 OSWorld，当前接入最终 RealtimeGame v1.1(3) 的 69 道网页游戏和四类 Agent。

> **AI 助手请先读 [`AGENTS.md`](AGENTS.md)** —— 那是面向 AI 的唯一操作规程（当前状态、能改/别碰的路径、命令、硬性约定、已知坑）。本文件面向人。

## 快速接手

最短路径只有五步，完整细节、排错与验收见 [运行说明](SETUP_GUIDELINE_CN.md)。

1. **准备环境**：Python 3.12 + `uv sync`，再 `pip install pytest imageio-ffmpeg`；`docker pull happysixd/osworld-docker`；确认 `ls -l /dev/kvm` 可用（没有 KVM 会极慢）。
2. **取 VM 镜像**（约 22.8 GiB，不入仓库，需从 HuggingFace 下载）：
   `hf download bright-star123/osworld-realtime-vm Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2 --local-dir docker_vm_data`
3. **配密钥**：按 YAML 里的 `api.key_env` 设置 `PACKY_COMMON_API_KEY` / `PACKY_KIMI_API_KEY` / `PACKY_GLM_MINIMAX_API_KEY`；也支持无回显 stdin 或 0600 的 keys 文件。**密钥只从环境变量读，绝不写进仓库任何文件。**
4. **冒烟一个模型一个任务**：
   `python scripts/python/run_realtime_batch.py --agent_variant agent4 --models gemini-3.8-flash --run_id smoke_$(date +%Y%m%d) --task 5169e1b0-1a7d-538b-8e59-8785c39460ce --exclusive-keys-confirmed`
5. **正式批量**：同一条命令去掉 `--task`，加上 `--num_envs 4 --keep-going --result_dir results_realtime_batches`。**续跑就是同一条命令、同样的 `--run_id` 和 `--result_dir` 再跑一次**：已有成绩的任务自动跳过，没成绩的目录会被清空重跑，无需额外参数。

结果和账单都不入库：单任务目录里有 `trajectory.html`（离线查看器）、`result.txt`、`system_prompt.txt`、逐事件 `trajectory.jsonl`；逐模型金额在 `--cost_dir`（默认 `<result_dir>/_cost/<run_id>`），含 `cost_report.json` 与网关明细。成绩与金额总表在仓库外单独整理。

## 当前状态

- 当前八款实验模型各有四组配置，另保留 `claude-fable-5`、`gpt-6-astra` 历史基线，共 40 份 YAML；使用 Packy 和系统 HTTPS 代理。当前计划为 combine × 八模型 × 69 任务 × 一次。
- 已完成实验仅 C1 单任务 × 八模型一轮；完整 69 任务矩阵尚未运行。新增模型配置与接口验证不等同于完整游戏成绩，Fable5、Astra 的 combine 均已实际通过 C1。
- 所有模型接收原始 1920×1080 截图。Gemini、MiniMax M3 参考 OSWorld-V2 的 0–1000 整数相对坐标配置 prompt、工具与转换，按 1000 换算成 VM 原生像素，其余模型保持原生坐标。适配后的完整游戏结果需另行验证。
- 69 个实时任务启动 Chrome 时关闭 `OutdatedBuildDetector`，避免旧版浏览器更新弹窗干扰；此启动参数在宿主机任务配置中生效，无需重打镜像。
- 最终镜像：`docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2`，已验证源码和实际 WAIT。

| Agent | 历史录像 | 一次提交动作 |
| --- | --- | --- |
| agent1 / vanilla | 无 | 1 |
| agent2 / anticipatory | 无 | 1–100 |
| agent3 / video | 有 | 1 |
| agent4 / combine | 有 | 1–100 |

Agent3 每次模型回复最多一个工具调用（get_frames 或动作）；取帧结果返回后可继续查询，单次 get_frames 仍可取 1–8 张帧。Agent4 可以同一回复提交多个查询。

所有组共享先观察探索、谨慎试探并珍惜 attempt 的公共 prompt；四组首句统一，并统一说明动作结果附带的截图可能尚未反映执行后的状态。每任务默认 100 个动作决策，三次游戏机会共用；正常评估时预算内未成功记有效 0 分，异常未取得有效成绩时不伪造分数。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [`AGENTS.md`](AGENTS.md) | **AI 助手操作规程**（接手前先读） |
| [`REALTIME_GUI_PROJECT.md`](REALTIME_GUI_PROJECT.md) | 项目总览：环境、四组 Agent、评分边界 |
| [`SETUP_GUIDELINE_CN.md`](SETUP_GUIDELINE_CN.md) | 部署与运行 + 接手准备 + 批量与续跑命令 |
| [`AGENT_EXPERIMENT_DESIGN.md`](AGENT_EXPERIMENT_DESIGN.md) | 实验设计：受控变量、工具与时序、预算、验证 |
| [`REALTIME_AGENT_LOOP_GUIDE.md`](REALTIME_AGENT_LOOP_GUIDE.md) | 实际程序流程（逐步骤） |
| [`REALTIME_AGENT_CONFIG_PROTOCOL.md`](REALTIME_AGENT_CONFIG_PROTOCOL.md) | YAML 字段协议 |
| [`configs/realtime_agents/README.md`](configs/realtime_agents/README.md) | 模型配置表、坐标协议、三组密钥启动方式 |
| [`REALTIME_GUI_BENCH_PROTOCOL.md`](REALTIME_GUI_BENCH_PROTOCOL.md) | 游戏接口协议（`window.BENCH` 字段、评分与验收清单） |
| [`REALTIME_GUI_BENCH_SHORTCUT_POLICY.md`](REALTIME_GUI_BENCH_SHORTCUT_POLICY.md) | 环境快捷键阻断策略（按键表、轨迹字段） |
| [`REALTIME_TRAJECTORY_VIEWER.md`](REALTIME_TRAJECTORY_VIEWER.md) | HTML 轨迹查看器用法 |
| [`evaluation_examples/REALTIME_GUI_BENCH_INTEGRATION.md`](evaluation_examples/REALTIME_GUI_BENCH_INTEGRATION.md) | 环境接入与验收记录 |
| [`REALTIME_REVIEW_REPORT.md`](REALTIME_REVIEW_REPORT.md) | 当前复核记录 |

## 其他说明

- 环境目录只含 69 个 HTML；任务 JSON、任务清单、评分器是必要运行文件。交付方自测与 OSWorld 独立验收分别记录于 [接入记录](evaluation_examples/REALTIME_GUI_BENCH_INTEGRATION.md)。
- 开发和运行命令在仓库根目录执行。密钥、截图、录像、缓存、VM 镜像和私有实验输出不提交到 Git。本次核查和清理结果见 [review 报告](REALTIME_REVIEW_REPORT.md)。
- Gemini 输出上限 65536；MiniMax M3 只开 adaptive thinking，不设 high。
- 实时实验结束后自动生成 `trajectory.html`，可离线查看截图、模型回复、历史帧和动作时长；已有轨迹转换方法见 [HTML 轨迹查看器](REALTIME_TRAJECTORY_VIEWER.md)。
