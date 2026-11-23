"""运行参数集中配置。

输入输出路径由命令行 --input/--output 传入（无参数时回落到源码根目录 input.json/output.json，
兼容赛题技术规范的旧入口）。模型地址来自环境变量或 .env，不写死在代码里。
主办方公布正式的生成参数、调用预算、并发和超时后，只需修改本文件。
"""
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

# 固定生成参数（temperature=0 + 固定 seed 以确保可复现）
GEN_PARAMS = {
    "temperature": 0,
    "top_p": 1,
    "seed": 42,
    "max_tokens": 3000,
    # OpenRouter Qwen 开发验证使用此设置。正式接口以赛方参数为准。
    "reasoning": {"enabled": False},
}
RANDOM_SEED = 42

REQUEST_TIMEOUT_S = 120     # 单次请求超时
MAX_RETRIES = 2             # 单次调用失败后的最大重试次数（不含首次）
RETRY_BACKOFF_S = 2.0       # 重试退避基数（秒），第 n 次重试等待 n * 该值
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
