# Packy API + C1 最终试跑

日期：2026-09-15。两次运行均使用系统 HTTPS 代理、Agent4 / combine、原始 1920×1080 截图和 `Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2`。没有使用坐标缩放适配。

## 结果

| 模型 | 终态 | 尝试 | pass_at_1 | pass_at_3 |
| --- | --- | ---: | ---: | ---: |
| `claude-fable-5` | passed | 2 | 0 | 1 |
| `gpt-6-astra` | passed | 3 | 0 | 1 |

两次都执行了 `DONE`，评分均为 `1.0`。这证明两个模型都能通过 Packy、Agent4 和最终 VM 镜像完成 C1；不是只完成了 API 文本 smoke test。

## Fable5

运行目录：[c1_fable5_packy_final_native_20260915](results_realtime_c1_trials/claude-fable-5/c1_fable5_packy_final_native_20260915)。共 5 次模型请求、1 次历史帧查询、21 个执行动作。

第一次点击 Start 后，模型提交了 `D → WAIT 0.35 → space → WAIT 0.35 → space → WAIT 1.2 → KEY_UP(D) → WAIT 1`，WAIT 的 VM 实际耗时分别约为 0.353、0.350、1.201、1.001 秒；第一次尝试失败。模型随后调用 `get_frames` 检查 6 个历史时间点，并根据轨迹重新安排跳跃时间。第二次提交 `点击 Next → WAIT 1.2 → D → WAIT 0.48 → space → WAIT 0.42 → space → WAIT 1.3 → KEY_UP(D) → WAIT 1`，成功到达终点并提交 `DONE`。

## Astra

运行目录：[c1_astra_packy_final_native_20260915](results_realtime_c1_trials/gpt-6-astra/c1_astra_packy_final_native_20260915)。共 15 次模型请求、2 次历史帧查询、22 个执行动作。

前两次主要是单动作：重复点击 Start、短暂按住/松开 D、只按一次 space，随后第一次和第二次尝试都失败。模型通过两次 `get_frames` 查看录像后，第三次才提交完整序列：`D → WAIT 0.1 → W → WAIT 0.34 → W → WAIT 0.7 → KEY_UP(D) → WAIT 0.5`，实际等待分别约为 0.111、0.340、0.700、0.501 秒，第三次成功并提交 `DONE`。

Agent4 的 sequence 能力和 VM 执行器均正常。前两次没有提交完整序列是模型的逐步试探策略，不是项目把 sequence 限制成单动作；最终响应实际包含连续动作并成功执行。

## 环境核验

终版镜像已验证 `fmp4=true`、`sequence=true`；镜像内 realtime 服务源码 SHA-256 与仓库一致；真实 `/realtime/sequence` 中 `WAIT 0.25` 的耗时为 0.250364 秒。此前旧镜像中 WAIT 被当成 0 秒的问题已不再存在。

原始轨迹和评分 JSON 保留在各自运行目录中，可逐轮复核。
