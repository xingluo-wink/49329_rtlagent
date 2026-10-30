# RTL Agent

面向 RTL 赛道的本地代码生成系统。根据题面选择零至两张相关知识卡，生成完整 SystemVerilog 模块；仅对有效编译失败执行一次有界修复。未选择知识卡时，使用官方基线的系统提示和原始题面直接生成。

模型为 Qwen3.6-35B-A3B / UD-Q4_K_M。Agent 与基线共用同一模型、32768 上下文和 thinking 配置。模型来源、权重 revision、SHA-256 和依赖见 [model/MODEL.md](model/MODEL.md)。

## 文件接口

依赖与权重准备完成后，在仓库根目录执行：

```bash
bash serve/start.sh
bash run.sh /path/to/input /path/to/empty-output
```

输入目录包含 `prompt.txt` 和可选的 `interface.txt`；输出为 `solution.v` 与 `trace.jsonl`。`run.sh` 调用 `agent/main.py`，由同一个常驻引擎完成读取、排队、生成与交付。基线入口为 `run_baseline.sh INPUT_DIR OUTPUT_DIR`，保留官方原始实现，须与 Agent 串行运行。

## HTTP 接口

```bash
bash serve/run_api.sh start
```

服务绑定 `127.0.0.1:7860`，使用环境变量 `FPGACHINA_TOKEN` 或 `FPGACHINA_TOKEN_FILE` 指定的私有文件进行鉴权。模型服务仅监听 `127.0.0.1:8000`。

| 接口 | 返回内容 |
| --- | --- |
| `GET /v1/health` | 模型、赛道、就绪状态和显存观测 |
| `POST /v1/solve` | HTTP 202 与任务编号，支持 agent / baseline |
| `GET /v1/jobs/<job_id>` | 任务状态、完成后的代码及执行记录 |

文件接口与 HTTP 接口共享串行队列。单题总预算为900秒，包含排队时间；相同 nonce 不重复执行。部署参数、状态目录和生命周期说明见 [docs/OPERATING.md](docs/OPERATING.md)。

## 目录

| 路径 | 内容 |
| --- | --- |
| `agent/` | 解题控制器、模型适配器、编译适配器与服务 |
| `skill/` | 知识目录、卡片与内容锁 |
| `model/` | 模型说明和运行参数锁 |
| `serve/` | 引擎、模型与 HTTP 服务入口 |
| `tools/` | 单题文件工具、公开示例与分发检查 |
| `selftest/` | 官方自测判定器 |
| `examples/` | 公开示例题面及独立判分材料 |
| `licenses/` | 第三方来源与许可 |

## 约束

Agent 最多进行路由、首稿和一次有效编译修复，共三次模型尝试。空正文、源码策略拒绝或工具未知不触发额外修复。当前源码策略只接受一个大小写精确为 `TopModule` 的模块。内部编译采用 xvlog 前端，不代表功能仿真或综合通过。

`python3 tools/check_release.py` 只检查分发文件、模型声明、知识锁和官方基线的一致性，不运行解题任务。公开示例的独立判分入口见 [selftest/README.md](selftest/README.md)。参考实现和测试台不进入模型提示。

## 来源与许可

官方基线、自测判定器与公开示例来自 [rtlagent2026](https://gitee.com/Vickyiii/rtlagent2026)，许可证见 `licenses/`。模型及运行工具分别遵循各自许可证。仓库不包含权重、令牌、私钥、运行日志或生成结果。
