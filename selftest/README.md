# 公开示例判分

```bash
bash selftest/run_selftest.sh
```

入口读取 `examples/popcount8/prompt.txt`，生成一次答案，再调用官方判定器编译、仿真与综合。与 `python3 tools/demo.py` 等价，不运行题库批处理或重复采样。

输出目录为 `/workspace/rtlagent/runs/popcount-.../`，终端打印完整路径：

- `solution.v`：生成代码。
- `trace.jsonl`：执行记录。
- `grading/result.json`：该公开示例的判定结果。
- `grading/logs/`：工具日志。

已有答案可直接交给判定工具：

```bash
python3 tools/grade.py examples/popcount8 /path/to/solution.v
```

L1为编译与展开通过，L2再加仿真通过，L3再加综合通过。许可证与工具故障单列。任务须提供 `task.json`、参考实现和测试台；仅有题面不能产生功能分数。

官方判定器逻辑保持原样，来源见 `judge/PROVENANCE.md`。参考实现和测试台仅用于独立判分，不进入模型提示。
