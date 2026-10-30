# 部署与维护

普通使用看 [四步跑通一道题](QUICKSTART.md)。这份文档只说明部署条件和维护，不必每题重做。

## 启动约定

`bash serve/start.sh` 启动模型和本地解题引擎，以独立会话在后台运行；就绪才返回成功。重复执行不创建第二份模型。文件入口为 `bash run.sh 输入目录 空输出目录`，也可使用 `python3 tools/solve.py 输入目录` 自动创建输出目录。文件入口无需公网、隧道或赛事令牌。

`bash serve/run_api.sh start` 才开启 127.0.0.1:7860 公网投题适配器。它校验赛事令牌，将请求交给同一引擎；不会另启模型。对外三个端点严格采用官方 health/solve/jobs 契约。缺少或错误令牌返回401，未知任务404，nonce与不同请求冲突409，暂不可用503，正常受理202。

本地文件客户端会登记自己的 PID、启动时间和 boot 身份。Ctrl+C/SIGTERM 请求取消，客户端被强杀时引擎也会检测其退出；不会把相同 PID 的另一个进程误认成客户端。取消或超时只有在工作进程、子进程和模型请求确认结束后，才释放串行位置。关闭未知时保留证据并停止准入，不能删除状态目录强行继续。

## 环境与模型

使用官方 Dockerfile 中锁定的比赛镜像。它位于平台私有镜像仓库，官方参考文件已说明参赛实例不提供 Docker；容器重建由赛事方执行。本仓库没有把未执行的 Docker build 写成实测成功。

镜像需提供 `/opt/python/bin/python3`、Uvicorn、`/opt/llamacpp/llama-server` 及 ROCm 库。Vivado 路径读取 `XILINX_VIVADO`。许可证读取实例的 `XILINXD_LICENSE_FILE`；工程工作区使用 `EDA_TMP`，推荐 `/var/tmp/eda`，每次独占创建。完成后保留必要的 DUT、诊断和关闭记录，清理已确认关闭的工作目录。

权重必须在断网前准备好或绑定挂载到：

`/var/tmp/model-selection-20261019/weights/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf`

准确公开下载地址、revision、文件大小、SHA、量化与生成配置见 [MODEL.md](../model/MODEL.md)。启动会核对这些值以及后端和模板；不会联网下载、换模型或改生成参数。

Docker 退路遵循官方两步：先 `bash serve/start.sh`，再 `bash run.sh <task_dir> <out_dir>`。无需另起公网投题服务，无需提供赛事令牌或手工填写部署身份。`run_baseline.sh` 与 `baseline.py` 保持赛事方原字节，直接调用同一模型；官方串行运行时，按原约定调用即可，不要手工与 Agent 并发。

## 状态与令牌

状态默认 `/workspace/rtlagent/state`，临时会话默认 `/var/tmp/rtlagent`，可分别用 `RTL_STATE_DIR`、`RTL_SESSION_DIR` 指定其他仓库外绝对路径。任务、nonce、已启动记录与终态保留，重新启动不重放题目。正常停止且确认子进程全关闭后，start.sh 可创建新会话。

本地引擎使用自动生成、仅所有者可读写的内部凭据。赛事令牌仅供公网投题适配器使用：从 `FPGACHINA_TOKEN` 或私有环境文件读取。默认私有文件 `/workspace/.fpgachina-49329/token.env`，也可设置 `FPGACHINA_TOKEN_FILE`。不要提交令牌、密钥或真实状态目录。

维护命令：

```bash
bash serve/start.sh status
bash serve/run_api.sh status
```

确需停止时先等待题目完成，然后 `bash serve/run_api.sh stop`、`bash serve/start.sh stop`。重启后如赛方隧道已失效，应重新 expose 并更新报送网址。比赛正式投题期间保持服务持续运行。

## 结果如何理解

`solution.v` 是生成代码，内部 xvlog 只证明前端编译。`tools/grade.py` 调用官方原始判定器，L1为编译和详细描述通过、L2为测试台仿真通过、L3再加综合通过。工具授权异常单列，不算功能错误。只有 prompt.txt 的自写题不能凭空获得功能分数；正式隐藏测试由赛事方执行。

REPORT.md 中 U 编号保留给参赛者亲自填写，包含官方要求的协作记录；D 编号用于尚待实测的数据。报告可以补写，运行源码与知识文件仍须符合完整性清单。
