# 查看 Agent 轨迹

每次实时 Agent 运行结束后，任务结果目录会自动生成 `trajectory.html`。成功或中途发生 API/执行错误都可以查看已有记录；生成 HTML 失败只记警告，不改变实验成绩或原始错误。生成在停止录屏之后进行，不影响游戏运行中的动作时序。

直接打开 HTML 即可离线查看截图和文字，不需要调用模型或启动 VM。原始 `trajectory.jsonl`、截图和录像都不会被改写。

## 页面内容

- 左侧按决策轮和事件排列，可以搜索、筛选模型回复/动作/历史帧/异常；键盘左右箭头切换事件。
- 截图可放大，鼠标悬停显示原图像素坐标。动作标记只画在对应的**模型输入截图**上，不把旧坐标画在执行后的新画面上。
- 模型文字、供应商实际返回的思考或摘要、工具名称/参数分别显示。没有思考文本时明确标注，不能把它解释成模型没思考。
- 历史帧显示请求时刻、实际取帧时刻及 `ok/not_ready/error`。没有取到的帧不会补造图片。
- 执行动作显示 VM 开始/结束时刻、请求时长、实际耗时，方便核对 WAIT 是否按要求执行。
- 每次模型请求的完整消息历史可展开，user、assistant、工具调用和工具结果的顺序照原日志显示。
- 评分使用原日志里的 evaluation 状态及 pass@1/pass@3；模型提交 DONE 不会被自行解释成游戏成功。
- 用量显示供应商的原始 usage 字段，不自动换算费用，也不混加不同协议的缓存/思考 token。

“日志时间”是宿主机记录事件的时间；“截图/历史帧时间”是 VM 录屏时间轴；模型请求耗时包含网络、排队与生成时间，不是纯思考耗时。

时间显示统一保留小数点后三位，包括展开的工具结果、JSON 时间字段和历史消息。毫秒以下的原始小数不会铺在页面上；坐标、用量等非时间数值不受影响。新请求里的截图时间提示和工具反馈时间也保留三位精度。计时、动作执行、取帧查找和原始 JSONL 测量值保持原精度；重导出旧轨迹只更新 HTML 显示，不改写历史记录或已发送给模型的消息。

## 转换已有轨迹

从 OSWorld 仓库根目录执行：

```bash
python scripts/python/render_realtime_trajectory.py /path/to/task/trajectory.jsonl
```

也可传任务目录，自动读取其中的 `trajectory.jsonl`，没有时尝试 `traj.jsonl`：

```bash
python scripts/python/render_realtime_trajectory.py /path/to/task
```

默认在任务目录生成 `trajectory.html`。指定其他输出位置：

```bash
python scripts/python/render_realtime_trajectory.py /path/to/task/trajectory.jsonl \
  --output /path/to/view.html
```

批量转换一个结果目录下已有的轨迹：

```bash
python scripts/python/render_realtime_trajectory.py /path/to/results --recursive
```

优先支持本项目 RealtimeAgent 的事件格式。其他格式的 `traj.jsonl` 会尽量显示步骤和原始字段，但不能保证能还原所有截图与时间信息。

## 图片与历史消息

HTML 内嵌已有截图，所以单独复制 HTML 也能查看图片和文字。相同图片和相同历史消息只存一份内容，各次请求引用它们；**显示时每个请求仍保留原来的全部消息、重复次数和顺序**，不删除或合并用户能看到的历史。比如截图 A 出现在第 2、3 次请求中，展开两次请求都能看到截图 A。

原日志中只有图片哈希时，会尝试与结果目录的截图匹配；文件缺失、超出目录或与记录哈希不一致时会提示，不将另一张图片冒充原图。HTML 的“事件字段”省略图片 base64 和不可读签名的长字符串，保留其引用/哈希/长度；完整原始记录仍在 JSONL 中。

旧日志存在动作结果 decision_id 偏移时，仅在查看器中按工具调用 ID 关联到对应决策，并保留原日志标注。中途截断或损坏的 JSONL 行会提示行号，其他可读事件继续展示。

`recording.mp4` 不嵌入 HTML，避免页面变成巨大的视频副本。要播放或定位录屏，请保留原结果目录中的录像；缺少录像不影响截图与文字浏览。不同浏览器的视频编码支持可能不同。

## 验证

Python 导出器使用标准库。浏览器端不依赖网络、CDN 或第三方脚本。测试覆盖历史引用不丢失、图片匹配、旧调用 ID 关联、缺失文件、损坏 JSONL、HTML 文本安全及自动导出不影响评分。

```bash
python -m pytest -q tests/test_realtime_trajectory_viewer.py tests/test_realtime_runner.py
```

浏览器交互测试需要 Playwright Chromium；可用 `REALTIME_VIEWER_CHROMIUM` 指定已有 Chromium 可执行文件，否则使用 Playwright 默认路径。
