"""批量运行与 run_log 组装。"""
import hashlib
import platform
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from .llm_client import now_iso


def materialize(results, cases) -> list:
    """把尚未完成的槽位填成明确的中间态记录，保证任何时刻落盘的文件都符合输出协议。"""
    out = []
    for case, res in zip(cases, results):
        out.append(res if res is not None else {
            "case_id": case.get("case_id"),
            "status": "failed",
            "medical_record": None,
            "error": {"code": "NOT_PROCESSED", "message": "该病例尚未生成（本次落盘的中间状态）"},
        })
    return out


def run_cases(cases, pipeline, concurrency: int = 1, on_result=None, deadline=None):
    """按输入顺序返回 (results, case_logs)；单例失败不影响其他病例。

    on_result(partial)：每完成一例回调一次，用于增量落盘（进程被中断也留得下结果）；
    deadline：time.monotonic() 截止点，超过后剩余病例直接走保底，不再调用模型。
    """
    if not cases:
        return [], []
    results, logs = [None] * len(cases), [None] * len(cases)

    def run_one(case):
        if deadline is not None and time.monotonic() > deadline:
            return pipeline.deadline_result(case)
        return pipeline.process(case)

    def record(idx, pair):
        res, lg = pair
        lg["index"] = idx
        results[idx] = res
        logs[idx] = lg
        if on_result is not None:
            on_result(materialize(results, cases))

    if concurrency <= 1:
        for idx, case in enumerate(cases):
            record(idx, run_one(case))
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(run_one, case): idx for idx, case in enumerate(cases)}
            for fut in as_completed(futures):
                record(futures[fut], fut.result())
    return results, logs


def code_fingerprint() -> dict:
    """源码与提示词的 SHA-256，用于核对"代码 + 提示词"与结果的对应关系。"""
    files = [config.BASE_DIR / "main.py"]
    files += sorted((config.BASE_DIR / "src").glob("*.py"))
    files += sorted(config.PROMPT_DIR.glob("*"))
    files += sorted((config.BASE_DIR / "resources").rglob("*")) if (config.BASE_DIR / "resources").is_dir() else []
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
            "call_budget_per_case": config.CALL_BUDGET,
            "max_repair_rounds": config.MAX_REPAIR_ROUNDS,
            "n_candidates": config.N_CANDIDATES,
            "concurrency": config.CONCURRENCY,
        },
        "random_seed": config.RANDOM_SEED,
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
        "fallback": sum(bool(lg.get("fallback")) for lg in logs),
        "total_model_calls": len(calls),
        "failed_model_calls": sum(not c.get("ok") for c in calls),
        "case_ids": [r["case_id"] for r in results],
    }
