# 查看 Agent 轨迹

每次实时 Agent 运行结束后，任务结果目录会自动生成 `trajectory.html`（停止录屏之后生成，失败只记警告，不影响成绩或动作时序）。成功或中途出错都可以查看已有记录；原始 `trajectory.jsonl`、截图和录像不会被改写。

直接打开 HTML 即可离线查看截图和文字，不需要调用模型或启动 VM。

## 页面内容

- 左侧按决策轮和事件排列，可搜索、筛选模型回复/动作/历史帧/异常，键盘左右箭头切换事件。
- 截图可放大，鼠标悬停显示原图像素坐标。动作标记只画在对应的**模型输入截图**上，不把旧坐标画在执行后的新画面上；箭头是 HTML 的坐标标注，不是从截图里提取的真实鼠标位置（截图内原有鼠标保持原样，可关闭“动作坐标标注”看原图）。
- 模型文字、供应商实际返回的思考或摘要、工具名称与参数分别显示；没有思考文本时会明确标注，不能解释成"模型没思考"。
- 历史帧显示请求时刻、实际取帧时刻和 `ok/not_ready/error`，没有取到的帧不会补造图片；执行动作显示 VM 开始/结束时刻、请求时长与实际耗时，便于核对 WAIT 是否按要求执行。
- 每次模型请求的完整消息历史可展开（user、assistant、工具调用、工具结果按原日志顺序）；评分显示原日志里的 evaluation 状态和 pass@1/pass@3，提交 DONE 不会被解释成游戏成功；用量显示供应商原始 usage 字段，不自动换算费用、不混加不同协议的缓存/思考 token。

“日志时间”是宿主机记录事件的时间，“截图/历史帧时间”是 VM 录屏时间轴，模型请求耗时包含网络与排队、不是纯思考时间。

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

HTML 内嵌已有截图，所以单独复制 HTML 也能看图看字。相同图片和历史消息只存一份、由各次请求引用，**显示时每个请求仍完整保留原来的消息、重复次数和顺序**（例如截图 A 出现在第 2、3 次请求，两次都能看到它）。

`recording.mp4` 不嵌入 HTML，避免页面变成巨大的视频副本。要播放或定位录屏，请保留原结果目录中的录像；缺少录像不影响截图与文字浏览。不同浏览器的视频编码支持可能不同。

查看器只用 Python 标准库导出，浏览器端不依赖网络、CDN 或第三方脚本，重导出不会改动原始 JSONL 或已发送给模型的消息。维护者的测试入口见 [AGENTS.md](AGENTS.md) 第 4 节。
