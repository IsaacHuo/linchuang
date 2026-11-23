# 知识卡片审核记录

cards.jsonl 首版（v1，39 张）由团队依据规划教材与公开指南要点自编，不含真实患者信息、具体数值和药物剂量。
卡片只作为"鉴别参考（非本病例事实）"进入生成提示。审核通过后默认开启（src/config.py 中 USE_RAG=True），--no-rag 可关闭。

## 审核要求（医学队员）

逐张核对：

1. differentials 的方向是否合理、有无遗漏必须排除的重要诊断；support / against / must_check 是否准确。
2. red_flags 与 plan_hints 是否安全，有无可能误导为具体治疗方案的表述。
3. 不得加入具体数值、药物名称剂量。

审核通过后在下表登记，再把 USE_RAG 改为 True（或评测时加 --rag），重跑 `python tools/build_manifest.py --version <版本>` 更新哈希。

| 日期 | 审核人 | 范围（卡片 id） | 结论 / 修改 |
|---|---|---|---|
| 2026-11-23 | IsaacHuo（团队确认） | 全部 39 张 | 通过，未修改 |
