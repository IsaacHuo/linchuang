"""运行参数集中配置。

赛题规定 .env 只放 MODEL_NAME 和 ENDPOINT，其余参数固定在这里，
保证"代码 + 提示词 + 参数"与提交结果一一对应、可复现。
主办方公布正式的生成参数、调用预算、并发和超时后，只需修改本文件。
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # main.py 所在目录
ENV_FILE = BASE_DIR / ".env"
INPUT_FILE = BASE_DIR / "input.json"
OUTPUT_FILE = BASE_DIR / "output.json"
RUN_LOG_FILE = BASE_DIR / "run_log.json"
PROMPT_DIR = BASE_DIR / "prompts"

# 流水线模式：improved = 事实抽取 → 生成 → 规则校验 → 定向修复；baseline = 单次调用
PIPELINE_MODE = "improved"

# 固定生成参数（temperature=0 + 固定 seed 以确保可复现）
GEN_PARAMS = {"temperature": 0, "top_p": 1, "seed": 42, "max_tokens": 3000}

REQUEST_TIMEOUT_S = 120     # 单次请求超时
MAX_RETRIES = 2             # 单次调用失败后的最大重试次数（不含首次）
RETRY_BACKOFF_S = 2.0       # 重试退避基数（秒），第 n 次重试等待 n * 该值
MAX_CALLS_PER_CASE = 6      # 每例调用上限（含重试）：抽取 1 + 生成 1 + 修复 1，余量留给重试
MAX_REPAIR_ROUNDS = 1       # 校验未通过时的定向修复轮数
CONCURRENCY = 1             # 并发病例数；按主办方上限调整
MAX_REVIEW_ITEMS = 10       # 待核事项上限，避免规则补充后过长
