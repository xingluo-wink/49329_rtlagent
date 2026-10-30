# 操作说明

## 1. 先确认部署环境

本方案使用赛事 Linux 镜像及 `model/` 中锁定的模型和后端。模型文件约 22.1 GB，不能提交到 Git；应在比赛断网前准备到运行锁指定的路径。`/opt/python/bin/python3`、`/opt/llamacpp/llama-server`、对应 ROCm 库及 `/tools/Xilinx/2026.1/Vivado` 必须可用。启动器会拒绝缺失或哈希不符的文件，不自动换版本。

此目录是一份独立分发内容。切换正在使用的服务前，先等已有任务结束并确认服务关闭。不要在同一块 GPU 上并行启动另一套模型。

## 2. 配置仓库外的数据

将 `serve/deployment.example.json` 复制到仓库外，例如 `/workspace/rtlagent/deployment.local.json`，填入已经核对的主机名、boot ID、执行身份和持久盘目录。主机名可用 `hostname` 读取，boot ID 可用 `cat /proc/sys/kernel/random/boot_id` 读取。重建实例后必须重新核对，不要直接改成新值来绕过身份异常。

```bash
export RTL_DEPLOYMENT_FILE=/workspace/rtlagent/deployment.local.json
export RTL_STATE_DIR=/workspace/rtlagent/state
export RTL_SESSION_DIR=/var/tmp/rtlagent
```

`RTL_STATE_DIR` 保存任务与幂等记录；`RTL_SESSION_DIR` 保存进程与 EDA 临时文件。它们都必须在源码目录之外。启动脚本不会删除旧任务记录。已有启动 intent、未关闭任务或未释放 lease 时，会停止并要求先检查。

令牌通过环境变量 `FPGACHINA_TOKEN` 提供，或使用仓库外的私密环境文件。默认文件为 `/workspace/.fpgachina-49329/token.env`；可用 `FPGACHINA_TOKEN_FILE` 指定其他绝对路径。文件内容为 `export FPGACHINA_TOKEN=实际令牌`，权限应仅允许文件所有者读写。令牌不写入仓库、命令示例、截图或日志。

## 3. 启动并检查

```bash
/opt/python/bin/python3 -B tools/check_release.py
chmod +x run.sh run_baseline.sh serve/start.sh
./serve/start.sh
curl --noproxy '*' -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7860/v1/health
curl --noproxy '*' -s -H "Authorization: Bearer ${FPGACHINA_TOKEN}" http://127.0.0.1:7860/v1/health
```

第一条 curl 应为 401，第二条应返回 `ready: true`、`track: rtl` 和匹配的模型。启动器以独立会话运行服务，正常关闭终端不会带走服务。重复启动只允许检查同一健康进程；旧记录存在但进程已退出时不会擅自创建第二套服务。

用私密文件而没有导出环境变量时，启动器仍能读取令牌，但手动 curl 之前需在当前终端加载该文件。不得打印令牌检查是否正确。

## 4. 开赛方隧道

先保证本地健康检查通过，再按赛方说明安装和 expose：

```bash
/var/run/secrets/frp-self-service/install
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  "$HOME/.local/bin/rc-tunnel" expose --port 7860
"$HOME/.local/bin/rc-tunnel" status
```

从自己的电脑携带令牌访问公网地址的 `/v1/health`，确认 HTTP 200、`ready: true`、`track: rtl`。只暴露 7860，不暴露模型端口 8000。服务中断或实例重启可能导致隧道失效；恢复服务后查询状态，必要时按赛方流程重新 expose 并记录新 URL。网址报送按赛方最新通知办理。

## 5. 把题目交给系统

将完整题面写入题目目录的 `prompt.txt`。题面应包含模块名、端口、位宽、时序、复位和要求的行为，不需要把测试台或参考答案交给模型。另有公开接口文件时才放 `interface.txt`。

当前源码策略仅接受单个、大小写精确为 `TopModule` 的模块。其他模块名或多模块交付会被归为策略拒绝，不是编译器确认的语法错误，也不会据此开放编译修复。此限制来自现有执行器，不能把操作说明当成支持任意模块接口的承诺。

```bash
./run.sh /path/to/problem /path/to/unused-output
```

程序会提交一次请求并等待同一 job 的结果。`solution.v` 是最终交付的 RTL，`trace.jsonl` 是执行记录，`accepted.json` 中保存 job_id。查看 `result.json` 可区分任务状态；文件为空或请求状态未知，都不能作为答题成功。

官方基线使用同一题目、另一空输出目录，并在模型空闲时串行运行：

```bash
./run_baseline.sh /path/to/problem /path/to/unused-baseline-output
```

对 API 直接投题时，正文包含 `task_id`、`nonce`、`mode`、`prompt`、`interface` 和 `deadline_s`；`mode` 为 `agent` 或 `baseline`。POST 返回 202 后用同一 job_id 查询，不要因等待较久而再次投题。正式赛方投题由平台完成，参赛者保持服务运行即可。

## 6. 出错时怎么处理

| 现象 | 处理 |
| --- | --- |
| 401 | 检查令牌是否注入，勿关闭鉴权 |
| 不 ready / 503 | 查状态目录的 service.log 和启动记录；不要重复起模型 |
| startup intent 已存在 | 先确认原服务及其任务是否仍运行，保留记录 |
| POST 超时或返回格式异常 | 保存 nonce 与 intent；先查原 job，不能直接换 nonce 补发 |
| done 但 solution 为空 | 这是交付失败，保留 trace；不要改成成功 |
| 编译过但仿真不对 | 以独立功能评判为准，前端编译不能证明功能 |
| 主机/boot/权重校验失败 | 停止启动并核对实际环境，不删除校验门 |

本分发版没有自动修复重启状态的命令。人工恢复必须先核清任务、模型请求、工具和监督进程的退出事实；不能通过删除 state 目录来让已执行任务重新运行。

## 7. 交付检查

仓库默认分支应包含本目录的完整文件，shell 文件使用 LF 换行。不要提交仓库外的令牌、部署身份、运行目录、权重或开发记录。代码冻结前应完成一次本分发版在赛事镜像上的启动、文件入口、鉴权与任务闭合验收；仅文件校验成功不能替代该验收。

报告里的 U 编号由本人按实际经历填写，D 编号用于补充真实测试和验收结果。可以直接编辑 `REPORT.md`，也可以用随交付的离线填写表导出同名报告替换它。运行校验只对这一份 UTF-8 文档允许补写；模型、代码、知识和启动脚本的校验仍然有效。报告终稿需移除占位符并检查匿名性与页数；内部填写表不上传为报告。
