"""批量运行与 run_log 组装。"""
import hashlib
import platform
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import config
from .llm_client import now_iso


def run_cases(cases, pipeline, concurrency: int = 1):
    """按输入顺序返回 (results, case_logs)；单例失败不影响其他病例。"""
    if not cases:
        return [], []
    if concurrency <= 1:
        pairs = [pipeline.process(c) for c in cases]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            pairs = list(pool.map(pipeline.process, cases))
    results, logs = [], []
    for idx, (res, lg) in enumerate(pairs):
        lg["index"] = idx
        results.append(res)
        logs.append(lg)
    return results, logs


def code_fingerprint() -> dict:
    """源码与提示词的 SHA-256，用于核对"代码 + 提示词"与结果的对应关系。"""
    files = [config.BASE_DIR / "main.py"]
    files += sorted((config.BASE_DIR / "src").glob("*.py"))
    files += sorted(config.PROMPT_DIR.glob("*"))
    total = hashlib.sha256()
    per_file = {}
    for p in files:
        if p.is_file():
            data = p.read_bytes().replace(b"\r\n", b"\n")  # 跨平台换行一致
            rel = p.relative_to(config.BASE_DIR).as_posix()
            per_file[rel] = hashlib.sha256(data).hexdigest()[:16]
            total.update(rel.encode() + b"\0" + data)
    return {"sha256": total.hexdigest(), "files": per_file}


def new_run_log(mode: str) -> dict:
    return {
        "run_id": uuid.uuid4().hex,
        "started_at": now_iso(),
        "finished_at": None,
        "exit_code": None,
        "pipeline_mode": mode,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "model_name": None,
        "endpoint": None,
        "gen_params": config.GEN_PARAMS,
        "limits": {
            "request_timeout_s": config.REQUEST_TIMEOUT_S,
            "max_retries": config.MAX_RETRIES,
            "max_calls_per_case": config.MAX_CALLS_PER_CASE,
            "max_repair_rounds": config.MAX_REPAIR_ROUNDS,
            "concurrency": config.CONCURRENCY,
        },
        "prompt_version": None,
        "code_fingerprint": code_fingerprint(),
        "summary": {},
        "error": None,
        "cases": [],
    }


def summarize(results, logs) -> dict:
    calls = [c for lg in logs for c in lg.get("calls", [])]
    return {
        "total_cases": len(results),
        "success": sum(r["status"] == "success" for r in results),
        "failed": sum(r["status"] == "failed" for r in results),
        "total_model_calls": len(calls),
        "failed_model_calls": sum(not c.get("ok") for c in calls),
        "case_ids": [r["case_id"] for r in results],
    }
