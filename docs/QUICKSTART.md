# 自己做题与赛方投题

本页用于队伍49329的成品实例，统一目录为 `/workspace/rtlagent/current`。不要使用旧实验目录里的入口。程序主体是Python，`run.sh`是赛方要求的统一包装，两者读取相同的题目文件。

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

程序读取 `examples/popcount8/prompt.txt`：输入8位数，输出其中有几个1。它自动调用模型生成代码，再用赛事官方判定器做编译、仿真、综合。也可用 `bash selftest/run_selftest.sh` 启动同一个示例，两种命令只选一种。

终端显示保存路径和本次等级：L0表示未通过编译，L1为编译与详细描述通过，L2再加仿真通过，L3再加综合通过。许可证或工具故障单列。这是公开示例的本地自测，正式分数由赛方给出。

输出目录中：`solution.v` 是答案，`trace.jsonl` 是调用记录，`grading/result.json` 是本地判分结果。可在 JupyterLab 左侧文件栏打开它们。

## 4. 换成自己的题

在文件栏打开或新建一个目录，把题面保存为其中的 **prompt.txt**。题面要写明模块名、端口和功能；官方题通常已包含这些内容，不需要另写接口文件。

例如，你之前已建好的题目直接这样运行：

```bash
python3 tools/solve.py /workspace/my_test/input
```

程序自动创建新的输出目录，不会覆盖之前的失败记录。无需再写 curl、创建随机目录、手动给模型贴题，也不用提供赛事令牌。

希望自己指定输出目录时，官方入口与直接调用Python等价，任选其一：

```bash
bash run.sh /workspace/my_test/input /workspace/my_test/output_01
# 或：python3 agent/main.py /workspace/my_test/input /workspace/my_test/output_01
```

`output_01`须是新的空目录。程序完成后打开该目录的`solution.v`。运行期间不要重复提交；需要主动中断时按Ctrl+C，程序会请求取消并保留记录。

只有题面时，可以生成代码，但不能凭空判定功能分数。对于带 `task.json`、参考实现和测试台的自测题，可以对答案判分：

```bash
python3 tools/grade.py examples/popcount8 /实际输出目录/solution.v
```

## 正式比赛：让服务等候赛方投题

本地做题和正式投题使用同一个引擎。正式投题额外启动 HTTP 适配器，然后开赛方隧道：

```bash
cd /workspace/rtlagent/current
source /workspace/env.sh
bash serve/start.sh
bash serve/run_api.sh start
curl --noproxy '*' -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7860/v1/health
# 不带令牌应返回401，再执行下面的隧道命令。
/var/run/secrets/frp-self-service/install
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY "$HOME/.local/bin/rc-tunnel" expose --port 7860
```

令牌应已放在环境文件或实例私有凭据文件中，不要写进代码仓库。等约15秒，运行 `"$HOME/.local/bin/rc-tunnel" status` 取得**完整URL**，状态须为active。已有正常隧道时只查看status，不重复expose；重新expose可能更换网址。

把这个URL和GitHub私有仓库链接填入赛方表单。赛方先访问`/v1/health`，再向`/v1/solve`发题，通过`/v1/jobs/<job_id>`取回代码并自行判分。比赛期间保持实例和服务运行，停止个人自测，无需逐题点击或上传答案。`ready: true`表示服务可接题，不代表已获得功能分数。

需要从自己的电脑核对公网入口时，在PowerShell执行以下命令，按提示粘贴邮件令牌：

```powershell
$publicUrl = Read-Host '粘贴 rc-tunnel status 的完整 URL'
$secret = Read-Host '粘贴赛事令牌' -AsSecureString
$token = [System.Net.NetworkCredential]::new('', $secret).Password
try {
  Invoke-RestMethod -Uri ($publicUrl.TrimEnd('/') + '/v1/health') -Headers @{Authorization=('Bearer ' + $token)}
} finally {
  Remove-Variable token, secret -ErrorAction SilentlyContinue
}
```

应看到`ready = True`、`track = rtl`及模型名。正式提交清单见[SUBMISSION](SUBMISSION.md)。

若服务提示未就绪或出现工具授权异常，按终端给出的具体日志路径查看，不要反复重投同一任务。正常使用不需要停止服务。需要维护时，先等当前题结束，再运行 `bash serve/run_api.sh stop` 和 `bash serve/start.sh stop`。
