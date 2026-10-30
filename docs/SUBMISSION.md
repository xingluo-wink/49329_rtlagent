# 代码封存与正式投题

依据官方[提交说明](https://gitee.com/Vickyiii/rtlagent2026/blob/master/docs/SUBMISSION.md)和[接口契约](https://gitee.com/Vickyiii/rtlagent2026/blob/master/docs/API_CONTRACT.md)，以下均为北京时间。

## 10月31日10:00前

1. 代码放在私有仓库`49329_rtlagent`的**默认分支**。赛方拉取时的版本就是封存版本，其他分支上的修复不计入。
2. GitHub赛事账号`vickyiii`至少有只读权限。仓库地址为`https://github.com/xingluo-wink/49329_rtlagent`。
3. 云端运行与默认分支一致的版本，先`bash serve/start.sh`，再`bash serve/run_api.sh start`，最后开赛方隧道。步骤见[QUICKSTART](QUICKSTART.md)。
4. 报送`rc-tunnel status`显示的完整公网URL和仓库链接。自己的电脑带令牌访问`/v1/health`，应返回HTTP 200、`ready: true`、`track: rtl`。
5. 保持实例、模型和投题服务运行，停止个人自测。赛方自动投题、查询答案和判分，不需要逐题手工上传。

方案包包含`Dockerfile`、`manifest.json`、`model/MODEL.md`、`agent/`、`skill/`、`serve/start.sh`、`run.sh`以及官方原始`run_baseline.sh`与`baseline.py`。权重、令牌、私钥、运行输出和EDA工作目录不提交Git。

## 赛方接口

| 入口 | 作用 |
| --- | --- |
| `GET /v1/health` | 返回真实就绪状态、rtl赛道、模型名和当前显存观测 |
| `POST /v1/solve` | 接收题面，立即返回202及任务号；mode区分agent和baseline |
| `GET /v1/jobs/<job_id>` | 返回queued/running/done，完成时给出代码、trace和耗时 |

所有公网接口校验赛事Bearer令牌。服务绑定`127.0.0.1:7860`，只把7860暴露给隧道。单题900秒包含排队；相同nonce不重复解题。RTL题的`interface`可以为空，模块接口已经在`prompt`里。

容器重建退路：赛方运行一次`serve/start.sh`，随后逐题调用`run.sh INPUT_DIR OUTPUT_DIR`或`run_baseline.sh INPUT_DIR OUTPUT_DIR`。文件入口不依赖公网服务、赛事令牌或手工填写部署身份。镜像和模型须事先准备好，离线启动不下载依赖。

## 11月4日18:00前

另行在大赛系统提交设计报告、演示视频和同一个仓库链接。REPORT.md可在代码封存时保留草稿；报告中的个人经历和协作记录由参赛者填写，不能删掉官方要求的章节。本次代码发布不代表这些材料已经完成。

正式成绩以赛方测评为准。启动和健康接口通过只证明服务已就绪，不能代替题目功能成绩或整套模型收益结论。
