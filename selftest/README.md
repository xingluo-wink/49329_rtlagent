# 公开示例自测

在成品仓库根目录执行：

```bash
bash selftest/run_selftest.sh
```

该入口与`python3 tools/demo.py`相同：启动或复用模型，读取`examples/popcount8/prompt.txt`，生成一份代码，再用官方判定器做编译、仿真和综合。一次调用只做这一道公开示例，不启动全题库、基线对比或重复采样。它会实际使用模型和Vivado，需由使用者主动执行。

输出在`/workspace/rtlagent/runs/popcount-…/`，终端给出准确路径：

- `solution.v`：生成答案。
- `trace.jsonl`：调用记录。
- `grading/result.json`：公开示例的本地判定。
- `grading/logs/`：官方工具日志。

L1为编译与详细描述通过，L2再加仿真通过，L3再加综合通过。许可证、工具故障单列。分数只属于本次公开示例，不是正式比赛成绩。

`judge.py`、`judge/`等判定实现来自官方参考仓库，判定逻辑保留。参考实现与测试台只用于生成结束后的独立判分，不发送给模型。Linux发布包使用LF换行。

只给模型一道自己的题：

```bash
python3 tools/solve.py /workspace/my_test/input
```

输入目录只需`prompt.txt`。有题面但没有测试台时，程序可以生成答案，不能凭空给出功能分数。对于另有`task.json`、测试台和参考实现的公开任务，可手动判定已经生成的文件：

```bash
python3 tools/grade.py examples/popcount8 /实际输出目录/solution.v
```

完整操作见[使用说明](../docs/QUICKSTART.md)。正式投题期间保持服务运行，停止个人自测，避免占用赛方任务的时限。
