"""单病例处理流水线：事实抽取 → 生成 → 确定性修正 → 规则校验 → 定向修复。"""
import time

from . import config
from .case_io import check_case
from .facts import find_missing_items, measurement_table
from .llm_client import CallBudget, ModelCallError, now_iso
from .parsing import ParseError, parse_json_object, unwrap_record
from .prompts import case_block, generate_user, repair_user
from .validate import (RECORD_FIELDS, audit_record, has_hard_errors, has_fact_errors, issue_score,
                       normalize_record, repairable)

# 各模式开关：用于基线与改进版、以及消融实验的同条件对比
MODES = {
    "baseline": dict(extract=False, hints=False, normalize=False, repair=False),
    "extract":  dict(extract=True,  hints=True,  normalize=True,  repair=False),
    "improved": dict(extract=True,  hints=True,  normalize=True,  repair=True),
}


def _failed(case_id, code, message):
    return {"case_id": case_id, "status": "failed", "medical_record": None,
            "error": {"code": code, "message": message}}


def _codes(issues):
    return [f"{i['severity']}:{i['code']}" for i in issues]


class Pipeline:
    def __init__(self, client, prompts, mode: str = "improved"):
        if mode not in MODES:
            raise ValueError(f"未知模式 {mode}")
        self.client = client
        self.prompts = prompts
        self.mode = mode
        self.flags = MODES[mode]

    def _chat(self, system, user, purpose, budget, log):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return self.client.chat(messages, purpose, budget, log["calls"])

    def process(self, case: dict):
        case_id = case.get("case_id")
        log = {"case_id": case_id, "mode": self.mode, "started_at": now_iso(), "calls": [], "steps": []}
        t0 = time.monotonic()
        try:
            result = self._process(case, log)
        except ModelCallError as exc:
            result = _failed(case_id, exc.code, exc.message)
        except Exception as exc:  # 单例异常不影响其他病例
            result = _failed(case_id, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"[:300])
        log.update(status=result["status"], error=result["error"], finished_at=now_iso(),
                   duration_s=round(time.monotonic() - t0, 3), model_calls=len(log["calls"]))
        return result, log

    def _process(self, case, log):
        case_id = case["case_id"]
        bad = check_case(case)
        if bad:
            log["steps"].append({"step": "check_case", "ok": False, "detail": bad})
            return _failed(case_id, "INVALID_CASE", bad)
        log["source_ids"] = [s["source_id"] for s in case["sources"]]
        budget = CallBudget(config.MAX_CALLS_PER_CASE)
        f = self.flags

        # 1. 事实抽取（失败时降级为仅用规则提示，不中断）
        extraction = None
        if f["extract"]:
            try:
                text = self._chat(self.prompts.extract, "请抽取以下资料中的结构化事实。\n\n" + case_block(case),
                                  "extract", budget, log)
                extraction = parse_json_object(text)
                log["steps"].append({"step": "extract", "ok": True})
            except (ModelCallError, ParseError) as exc:
                log["steps"].append({"step": "extract", "ok": False, "detail": str(exc)[:200]})

        # 2. 生成
        if f["hints"]:
            user = generate_user(case, extraction, measurement_table(case["sources"]),
                                 find_missing_items(case["sources"]))
            system = self.prompts.generate
        else:
            user = generate_user(case)
            system = self.prompts.baseline
        raw = self._chat(system, user, "generate", budget, log)
        best_record, best_issues = self._evaluate(raw, case, log, "generate")
        log["validation_initial"] = _codes(best_issues)

        # 3. 定向修复：只把 error 级问题反馈给模型，择优保留
        rounds = 0
        while (f["repair"] and rounds < config.MAX_REPAIR_ROUNDS and repairable(best_issues)
               and budget.remaining > 0):
            rounds += 1
            previous = best_record if best_record is not None else raw
            try:
                raw2 = self._chat(self.prompts.repair, repair_user(case, previous, repairable(best_issues)),
                                  "repair", budget, log)
            except ModelCallError as exc:
                log["steps"].append({"step": f"repair_{rounds}", "ok": False, "detail": exc.message[:200]})
                break
            rec2, iss2 = self._evaluate(raw2, case, log, f"repair_{rounds}")
            if rec2 is not None and issue_score(iss2) <= issue_score(best_issues):
                best_record, best_issues = rec2, iss2
        log["repair_rounds"] = rounds
        log["validation_final"] = _codes(best_issues)

        if best_record is None:
            return _failed(case_id, "OUTPUT_PARSE_ERROR", "模型输出无法解析为 JSON 对象")
        if has_hard_errors(best_issues):
            detail = "；".join(i["detail"] for i in best_issues if i["severity"] == "error")[:300]
            return _failed(case_id, "OUTPUT_SCHEMA_ERROR", detail)
        if f["repair"] and has_fact_errors(best_issues):
            detail = "；".join(i["detail"] for i in best_issues if i["severity"] == "error")[:300]
            return _failed(case_id, "OUTPUT_FACT_ERROR", detail)
        medical_record = {k: best_record[k] for k in RECORD_FIELDS}
        return {"case_id": case_id, "status": "success", "medical_record": medical_record, "error": None}

    def _evaluate(self, raw, case, log, step):
        try:
            record = unwrap_record(parse_json_object(raw))
        except ParseError as exc:
            log["steps"].append({"step": step, "ok": False, "detail": str(exc)})
            return None, [{"code": "SCHEMA", "severity": "error", "field": "medical_record",
                           "detail": "输出不是合法的 JSON 对象"}]
        fixes = []
        if self.flags["normalize"]:
            record, fixes = normalize_record(record, case, config.MAX_REVIEW_ITEMS)
        issues = audit_record(record, case)
        log["steps"].append({"step": step, "ok": True, "fixes": fixes, "issues": issues})
        return record, issues
