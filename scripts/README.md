# 脚本目录说明

本目录存放 OSWorld 的运行和辅助脚本。所有脚本都应从仓库根目录执行，不要先进入 `scripts/`。

## 目录结构

```text
scripts/
├── python/          # Python 运行脚本
│   ├── run_*.py     # 单模型或单功能入口
│   └── run_multienv_*.py  # 多环境运行入口
└── bash/            # Shell 启动脚本
    └── run_*.sh
```

## 实时 GUI 项目入口

四组 Agent 使用同一个入口：

```bash
python scripts/python/run_multienv.py \
  --agent_variant agent1 \
  --model claude-fable-5 \
  --path_to_vm docker_vm_data/Ubuntu-realtime-gui-fmp4-v1.1-final.qcow2 \
  --api_base_url https://www.packyapi.ai \
  --run_id exp001 \
  --action_space computer_13 \
  --observation_type screenshot \
  --test_all_meta_path evaluation_examples/test_realtime_gui_bench.json
```

真实 Docker 运行还需要 provider、VM 镜像、模型、凭据和其他参数。完整模板见仓库根目录的 `REALTIME_PROJECT_DESIGN.md` 和 `README_CN.md`。

## 其他脚本

- `python/` 下的 `run_*.py` 用于其他模型或 OSWorld 运行模式。
- `bash/` 下的 `run_*.sh` 是预设参数示例。
- `validate_realtime_gui_bench.py` 用于检查实时任务接入和评分路径。
- `install_realtime_server.py` 用于将实时录像与历史帧服务安装到当前 VM。
- `verify_realtime_runtime.py` 用模拟模型回复顺序验证两协议四组 Agent 和真实 VM，不调用付费 API。

## 开发约定

脚本必须从项目根目录运行；入口会自行处理项目模块导入。新增脚本时，应复用现有参数命名、结果目录布局和日志格式，并同步更新中文文档和相关测试。
