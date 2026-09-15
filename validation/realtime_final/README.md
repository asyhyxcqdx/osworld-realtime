# 环境与 Agent review 证据

这些是精简的无模型核查证据，不是新增的真实模型实验结果：

- source_manifest.json：最新 ZIP 哈希、69 个 HTML 与必要任务配置哈希、manifest 映射。
- checker_single.json / checker_batch.json：OSWorld 保存的实际 BENCH 快照通过单文件/批量契约检查；完整快照和原始报告在文件内引用的本地证据目录。
- runtime_matrix.json：真实 VM + 两协议四组模拟回复；读取原生截图/历史帧，实际执行 WAIT 和 DONE，最后模拟刷新验证运行失效检查。
- image_execution_check.json：终版镜像重新启动后的源码哈希、实际 WAIT、100 动作序列检查。

没有模型密钥、请求授权头、模型私有推理、原始截图或 VM 镜像。真实 C1 结果另见仓库根目录的 PACKY_C1_FINAL_TRIAL_REPORT.md。
