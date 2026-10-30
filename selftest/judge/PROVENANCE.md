# 这些文件是复制进来的，不要就地修改

| | |
| --- | --- |
| 来源仓库 | `RTL-HLS-Local-Agent-Track` |
| 来源路径 | `tools/veval-vivado/` |
| 文件 | veval-judge, l3_synth.tcl, sv-xsim-analyze |
| commit | `c9f153a`（2026-09-11） |

**与赛事方正式评测用的是同一份判定器。** 自测判出来的级别，就是正式评测会给的
级别，前提是此处的副本未发生偏离。

## 为什么是复制而不是引用

学生 clone 下来就要能跑，不能再要求他们去取另一个仓库。代价是这里会随时间与
上游分叉，所以：

- **改判分逻辑请改上游**，然后重新复制过来，并更新上面的 commit。
- 就地修改此处文件会使队伍自测口径与实际评分不一致，而这正是引入
  `../judge.py` 那层适配要避免的事。

## 同步后请验一次

同一份解分别用这里和上游判一遍，级别必须一致：

```bash
# 本仓库
cd selftest && ./run_selftest.sh --reference

# 上游（在 RTL-HLS-Local-Agent-Track 里）
tools/veval-vivado/veval-judge --dut <解> ... --json /tmp/up.json
```

RTL track 的分级定义见 `../../SCORING.md`。
