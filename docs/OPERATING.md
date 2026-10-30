# 运行环境与部署接口

## 依赖

使用 Dockerfile 指定的赛事 Linux 镜像。环境需提供 Python 3.11以上、Uvicorn、对应 ROCm 的 llama.cpp、Vivado 2026.1与有效许可证。

Python入口为 `/opt/python/bin/python3`，后端为 `/opt/llamacpp/llama-server`。Vivado安装路径读取 `XILINX_VIVADO`，许可证读取 `XILINXD_LICENSE_FILE`。EDA工作目录由 `EDA_TMP` 指定，默认使用 `/var/tmp/eda` 下独占创建的目录。

权重须事先准备在 `model/model-runtime-lock.json` 指定位置。来源、revision、大小和SHA-256见 [MODEL.md](../model/MODEL.md)。启动时核验权重、后端二进制、模板和实际模型属性，不下载依赖或权重。

## 生命周期

```bash
bash serve/start.sh
bash serve/start.sh status
bash serve/run_api.sh start
bash serve/run_api.sh status
```

引擎以独立后台会话运行；就绪后启动命令返回。文件入口不需要HTTP服务或赛事令牌。HTTP适配器复用同一个引擎，不另外加载模型。维护停止前应等待当前任务结束：

```bash
bash serve/run_api.sh stop
bash serve/start.sh stop
```

客户端取消、退出和超时进入资源关闭流程。仅在进程、后代与模型请求均确认结束后释放串行位置；关闭状态未知时保留证据并停止准入。

## 配置与状态

| 配置 | 用途 |
| --- | --- |
| `RTL_STATE_DIR` | 持久任务与会话状态，默认 `/workspace/rtlagent/state` |
| `RTL_SESSION_DIR` | 临时会话目录，默认 `/var/tmp/rtlagent` |
| `FPGACHINA_TOKEN` | HTTP接口的赛事Bearer令牌 |
| `FPGACHINA_TOKEN_FILE` | 可选的私有令牌文件 |
| `EDA_TMP` | 编译工作目录根 |

状态和凭据置于仓库之外。启动时采集当前会话身份；重启不重放已有任务。对结果未知或关闭未确认的记录，不自动重发。

HTTP接口绑定 `127.0.0.1:7860`。未提供或错误令牌返回401，未知任务404，nonce与不同请求冲突409，暂不可用503，正常受理202。模型端口8000不对公网暴露。

## 文件入口

```bash
bash run.sh INPUT_DIR EMPTY_OUTPUT_DIR
bash run_baseline.sh INPUT_DIR EMPTY_BASELINE_OUTPUT_DIR
```

输入包含 `prompt.txt` 和可选 `interface.txt`。输出为 `solution.v`、`trace.jsonl`，官方基线保持单次直接生成。两种入口须串行使用同一模型服务。

容器复现采用同一启动和文件接口；不需要额外启动HTTP服务或提供赛事令牌。基础镜像与权重需在断网运行前准备完毕。
