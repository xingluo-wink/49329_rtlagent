# 跑通一道题

本页针对已经部署好成品的 Radeon Cloud 实例。成品入口是 `/workspace/rtlagent/current`；云端另有保留的旧版本目录，不要混用。

## 1. 进入终端

在 Radeon Cloud 打开实例的 **Notebook**。在 JupyterLab 左上角点击 **File → New → Terminal**。

## 2. 进入成品目录并启动

```bash
cd /workspace/rtlagent/current
bash serve/start.sh
```

等到显示“引擎已就绪”。已经启动时，这条命令不会再加载一份模型。关闭这个终端不会关闭引擎。

## 3. 完整跑一次官方示例

```bash
python3 tools/demo.py
```

程序会读取官方的 8 位计数题，调用模型生成代码，然后用赛事官方判定器依次做编译、仿真、综合。终端会显示进度和最终的 `L0`～`L3`，以及保存路径。`L3` 表示这次自测的三个阶段均通过；这不是正式比赛成绩。

输出目录中：`solution.v` 是答案，`trace.jsonl` 是调用记录，`grading/result.json` 是本地判分结果。可在 JupyterLab 左侧文件栏打开它们。

## 4. 换成自己的题

在文件栏打开或新建一个目录，把题面保存为其中的 **prompt.txt**。题面要写明模块名、端口和功能；官方题通常已包含这些内容，不需要另写接口文件。

例如，你之前已建好的题目直接这样运行：

```bash
python3 tools/solve.py /workspace/my_test/input
```

程序自动创建新的输出目录，不会覆盖之前的失败记录。无需再写 curl、创建随机目录、手动给模型贴题，也不用提供赛事令牌。

只有题面时，可以生成代码，但不能凭空判定功能分数。对于带 `task.json`、参考实现和测试台的自测题，可以对答案判分：

```bash
python3 tools/grade.py examples/popcount8 /实际输出目录/solution.v
```

## 交给赛事方测评

本地做题和正式投题使用同一个引擎。正式投题额外启动 HTTP 适配器，然后开赛方隧道：

```bash
cd /workspace/rtlagent/current
source /workspace/env.sh
bash serve/start.sh
bash serve/run_api.sh start
/var/run/secrets/frp-self-service/install
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY "$HOME/.local/bin/rc-tunnel" expose --port 7860
```

令牌应已放在环境文件或实例私有凭据文件中，不要写进代码仓库。等约 15 秒，再运行 `"$HOME/.local/bin/rc-tunnel" status` 取得 **URL**。比赛期间保持实例和服务运行；赛事方发送题面、轮询答案并自行判分，无需你逐题操作。

若服务提示未就绪或出现工具授权异常，按终端给出的具体日志路径查看，不要反复重投同一任务。正常使用不需要停止服务。需要维护时，先等当前题结束，再运行 `bash serve/run_api.sh stop` 和 `bash serve/start.sh stop`。
