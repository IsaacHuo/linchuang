"""运行参数集中配置。

输入输出路径由命令行 --input/--output 传入（无参数时回落到源码根目录 input.json/output.json，
兼容赛题技术规范的旧入口）。模型地址来自环境变量或 .env，不写死在代码里。
主办方公布正式的生成参数、调用预算、并发和超时后，只需修改本文件。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # main.py 所在目录（代码自带资源的定位基准）
ENV_FILE = BASE_DIR / ".env"
DEFAULT_INPUT = BASE_DIR / "input.json"     # 仅在未传 --input 时使用
DEFAULT_OUTPUT = BASE_DIR / "output.json"   # 仅在未传 --output 时使用
RUN_LOG_NAME = "run_log.json"              # 写在 output 同目录
PROMPT_DIR = BASE_DIR / "prompts"
KB_FILE = BASE_DIR / "resources" / "kb" / "cards.jsonl"

# 流水线模式：improved = 事实抽取 → 检索 → 生成 → 规则校验 → 定向修复；baseline = 单次调用
PIPELINE_MODE = "improved"

# 关闭 Qwen Thinking 的写法：正式接口是自部署 vLLM 的 OpenAI 兼容层，用
# chat_template_kwargs；OpenRouter 等聚合网关用 reasoning。当天接口状态不明确时，
# 用环境变量 THINKING_OFF_STYLE=chat_template|reasoning|none 切换，不必改代码。
# 注意：vLLM 的 OpenAIBaseModel 为 extra="forbid"，未知字段会返回 400。
THINKING_OFF_STYLE = (os.environ.get("THINKING_OFF_STYLE") or "chat_template").strip().lower()
THINKING_OFF_PARAMS = {
    "chat_template": {"chat_template_kwargs": {"enable_thinking": False}},
    "reasoning": {"reasoning": {"enabled": False}},
    "none": {},
}.get(THINKING_OFF_STYLE, {})

# 固定生成参数（temperature=0 + 固定 seed 以确保可复现）。只放接口明确支持的字段。
# max_tokens 留足余量：Thinking 未被真正关闭时，推理 token 也计入该预算。
GEN_PARAMS = {
    "temperature": 0,
    "top_p": 1,
    "seed": 42,
    "max_tokens": 4000,
    **THINKING_OFF_PARAMS,
}
RANDOM_SEED = 42

REQUEST_TIMEOUT_S = 60      # 单次请求超时（赛方时限未公布，取保守值，由总时限兜底）
MAX_RETRIES = 1             # 单次调用失败后的最大重试次数（不含首次）；重试计入调用预算
RETRY_BACKOFF_S = 2.0       # 重试退避基数（秒），第 n 次重试等待 n * 该值
TOTAL_BUDGET_S = 900        # 总运行时限（秒）：到点后剩余病例直接走保底并落盘；0 = 不限制
INCREMENTAL_WRITE = True    # 每完成一例就原子重写 output.json，进程被掐也留得下结果
WRITE_RESULTS_ALIAS = True  # 同时写一份 results.json（规范与提交材料的两种命名都兼容）
CALL_BUDGET = 6             # 每例 Qwen 调用上限（含重试）；降级阶梯按剩余预算自动裁剪步骤
MAX_CALLS_PER_CASE = CALL_BUDGET  # 旧名，保留兼容
MAX_REPAIR_ROUNDS = 1       # 校验未通过时的定向修复轮数
N_CANDIDATES = 1            # 生成候选数；>1 时按规则评分择优（预算充足时再开）
CONCURRENCY = 1             # 并发病例数；按主办方上限调整
MAX_REVIEW_ITEMS = 10       # 待核事项上限，避免规则补充后过长

# 检索增强（团队自编鉴别卡片）。embedding 服务未配置或不可用时自动退回本地 BM25。
# 默认关闭：卡片需医学队员按 resources/kb/REVIEW.md 审核签字后再改为 True（或运行时加 --rag）
USE_RAG = True
RAG_TOP_K = 3
EMBED_TIMEOUT_S = 60
EVIDENCE_ALIGN_THRESHOLD = 0.45  # evidence 与所引资料的向量相似度低于此值记 warn（不触发修复）

# 保底记录：模型全挂或输出始终不合规时，用资料原文确定性拼出合规首程，避免整例 0 分
ENABLE_FALLBACK = True
