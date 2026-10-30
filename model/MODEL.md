# 模型与推理配置

| 项目 | 值 |
| --- | --- |
| 基础模型 | Qwen/Qwen3.6-35B-A3B |
| 量化仓库 | unsloth/Qwen3.6-35B-A3B-GGUF |
| revision | a483e9e6cbd595906af30beda3187c2663a1118c |
| 量化 | UD-Q4_K_M |
| 文件 | Qwen3.6-35B-A3B-UD-Q4_K_M.gguf |
| 字节数 | 22134528992 |
| SHA-256 | ac0e2c1189e055faa36eff361580e79c5bd6f8e76bffb4ce547f167d53e31a61 |
| 后端 | llama.cpp b1-f114f91，ROCm |
| 上下文 | 32768 |
| thinking | Agent 所有阶段和官方基线均为 true |
| Agent 输出上限 | 路由 4096；首稿与修复不超过 16384，受实际上下文及剩余时间约束 |
| 官方基线 | 原始实现，输出上限 16384，temperature 0、top_p 1，单次调用 |
| 微调与辅助模型 | 无 |

精确后端 argv、二进制 SHA、模板 SHA、模型属性在 `model-runtime-lock.json`；权重索引在 `models.lock.json`。启动前会逐项检查，实际 API 属性也必须与锁一致。锁文件描述的是所需环境，不是对未知环境的验证结果。

权重公开地址：https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/tree/a483e9e6cbd595906af30beda3187c2663a1118c 。模型按上游许可证使用。权重必须提前准备，离线启动不下载。

最近一次同权重已加载、无生成请求的实例观测为22.992 GB（22991556608字节，2026-10-30 22:52北京时间）。该观测来自前一服务会话，精确运行指纹写在manifest.json；当前成品的实时值由GET /v1/health返回。实例物理显存为48 GiB，这条空闲观测不代表32 GB物理卡实测，也不证明生成过程峰值。单卡32 GB限制仍按赛事要求，未用未测的峰值替代实际记录。
