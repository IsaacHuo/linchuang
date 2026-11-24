"""单病例处理流水线：事实与问题抽取 →（可选）鉴别知识检索 → 生成（可多候选）→ 确定性修正 → 规则校验
→ 定向修复 →（兜底）确定性保底记录。

调用预算按"降级阶梯"自动裁剪步骤（每例预算 = config.CALL_BUDGET，含重试）：
  预算 ≥ 4：抽取 + 生成 + 修复（全流程）
  预算 = 3：抽取 + 生成（去修复）
  预算 = 2：生成 + 修复（去抽取）
  预算 = 1：单次生成
"""
import time

from . import config
from .case_io import check_case
from .facts import find_missing_items, measurement_table
from .fallback import build_fallback
from .llm_client import CallBudget, ModelCallError, SubBudget, now_iso
from .parsing import ParseError, parse_json_object, unwrap_record
from .prompts import case_block, generate_user, repair_user
from .retrieval import Aligner, format_cards
from .validate import (RECORD_FIELDS, TEXT_FIELDS, audit_record, has_fact_errors, has_hard_errors,
                       issue_score, normalize_record, repairable)

# 各模式开关：用于基线与改进版、以及消融实验的同条件对比。rag=None 表示跟随 config.USE_RAG / 命令行
MODES = {
    "baseline":     dict(extract=False, hints=False, normalize=False, repair=False, rag=False),
    "extract":      dict(extract=True,  hints=True,  normalize=True,  repair=False, rag=False),
    "improved":     dict(extract=True,  hints=True,  normalize=True,  repair=True,  rag=None),
    "improved+rag": dict(extract=True,  hints=True,  normalize=True,  repair=True,  rag=True),
}
# 这些失败可由保底记录兜住；INVALID_CASE（输入本身不合法）不兜底。
# OUTPUT_FACT_ERROR 也兜底：编造事实封顶、失败为 0 分，而保底只摘录原文。
FALLBACK_CODES = {"OUTPUT_PARSE_ERROR", "OUTPUT_SCHEMA_ERROR", "OUTPUT_FACT_ERROR", "MODEL_TIMEOUT",
                  "MODEL_UNAVAILABLE", "MODEL_HTTP_ERROR", "MODEL_BAD_RESPONSE", "MODEL_OUTPUT_TRUNCATED",
                  "CALL_BUDGET_EXCEEDED", "INTERNAL_ERROR"}
TARGET_LENGTH = 1200


def _failed(case_id, code, message):
    return {"case_id": case_id, "status": "failed", "medical_record": None,
            "error": {"code": code, "message": message}}


def _codes(issues):
    return [f"{i['severity']}:{i['code']}" for i in issues]


def _length(rec):
    return sum(len(rec.get(f) or "") for f in TEXT_FIELDS) if isinstance(rec, dict) else 0


def _rank(rec, issues):
    """候选择优：先比规则扣分，同分取正文字数更接近 1200 的。"""
    if rec is None:
        return (10 ** 6, 0)
    return (issue_score(issues), abs(_length(rec) - TARGET_LENGTH))


def plan_steps(limit: int, flags: dict) -> dict:
    use_extract = flags["extract"] and limit >= 3
    use_repair = flags["repair"] and (limit >= 4 or (limit >= 2 and not use_extract))
    return {"extract": use_extract, "repair": use_repair}


def _rag_query(case, extraction):
    parts = []
    for s in case["sources"]:
        head = s["content"][:12]
        if any(k in head for k in ("主诉", "初步诊断", "初步判断")):
            parts.append(s["content"])
    if not parts:
        parts.append(" ".join(s["content"] for s in case["sources"][:3]))
    for p in (extraction or {}).get("problem_list") or []:
        if isinstance(p, dict):
            parts.append(str(p.get("problem", "")))
            parts.extend(map(str, p.get("must_not_miss") or []))
    return "\n".join(parts)[:1500]


def must_not_miss_coverage(extraction, record):
    """抽取出的 must_not_miss 有多少在拟诊讨论中被提及（取前 4 个字的子串粗匹配）。"""
    items = [str(x) for p in (extraction or {}).get("problem_list") or [] if isinstance(p, dict)
             for x in p.get("must_not_miss") or [] if str(x).strip()]
    if not items or not isinstance(record, dict):
        return None
    disc = record.get("diagnostic_discussion") or ""
    hit = [x for x in items if x.strip()[:4] in disc]
    return {"total": len(items), "covered": len(hit), "missed": [x for x in items if x not in hit]}


class Pipeline:
    def __init__(self, client, prompts, mode: str = "improved", retriever=None, embedder=None,
                 use_rag=None, n_candidates=None, fallback=None, call_budget=None):
        if mode not in MODES:
            raise ValueError(f"未知模式 {mode}")
        self.client = client
        self.prompts = prompts
        self.mode = mode
        self.flags = dict(MODES[mode])
        if self.flags["rag"] is None:
            self.flags["rag"] = config.USE_RAG if use_rag is None else use_rag
        self.retriever = retriever if self.flags["rag"] else None
        self.aligner = Aligner(embedder, config.EVIDENCE_ALIGN_THRESHOLD)
        self.n_candidates = max(1, n_candidates or config.N_CANDIDATES)
        self.fallback = config.ENABLE_FALLBACK if fallback is None else fallback
        self.call_budget = call_budget or config.CALL_BUDGET

    def _chat(self, system, user, purpose, budget, log, params=None):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if params:  # 只有多候选才传 params，兼容不带该参数的客户端
            return self.client.chat(messages, purpose, budget, log["calls"], params=params)
        return self.client.chat(messages, purpose, budget, log["calls"])

    def process(self, case: dict):
        if self.client is None:  # 未配置模型接口：不调用模型，只产出保底草稿
            return self.fallback_only(case)
        case_id = case.get("case_id")
        log = {"case_id": case_id, "mode": self.mode, "started_at": now_iso(), "calls": [], "steps": [],
               "fallback": False}
        t0 = time.monotonic()
        try:
            result = self._process(case, log)
        except ModelCallError as exc:
            result = _failed(case_id, exc.code, exc.message)
        except Exception as exc:  # 单例异常不影响其他病例
            result = _failed(case_id, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"[:300])
        if result["status"] == "failed" and self.fallback and result["error"]["code"] in FALLBACK_CODES:
            result = self._fallback(case, log, result)
        log.pop("_hits", None)  # 卡片原文不写入日志，只保留 rag 中的 id 与分数
        log.update(status=result["status"], error=result["error"], finished_at=now_iso(),
                   duration_s=round(time.monotonic() - t0, 3), model_calls=len(log["calls"]))
        return result, log

    def _fallback(self, case, log, failed):
        try:
            rec = build_fallback(case, log.get("_hits"))
            issues = audit_record(rec, case)
        except Exception as exc:
            log["steps"].append({"step": "fallback", "ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]})
            return failed
        log["steps"].append({"step": "fallback", "ok": not has_hard_errors(issues), "issues": issues})
        if has_hard_errors(issues):
            return failed
        log.update(fallback=True, fallback_reason=failed["error"], validation_final=_codes(issues))
        return {"case_id": case["case_id"], "status": "success",
                "medical_record": {k: rec[k] for k in RECORD_FIELDS}, "error": None}

    def _no_model(self, case, code, message):
        """不发起任何模型调用，直接用资料原文拼保底草稿（配置缺失、超过总时限时使用）。"""
        case_id = case.get("case_id")
        log = {"case_id": case_id, "mode": self.mode, "started_at": now_iso(), "calls": [], "steps": [],
               "fallback": False}
        t0 = time.monotonic()
        bad = check_case(case)
        if bad:
            res = _failed(case_id, "INVALID_CASE", bad)
        else:
            res = self._fallback(case, log, {"error": {"code": code, "message": message}})
            if res["status"] == "failed":
                res = _failed(case_id, code, message + "；保底草稿仍未通过校验")
        log.update(status=res["status"], error=res["error"], finished_at=now_iso(),
                   duration_s=round(time.monotonic() - t0, 3), model_calls=0)
        return res, log

    def fallback_only(self, case):
        return self._no_model(case, "NO_MODEL_CONFIG", "未配置模型接口，按保底草稿输出")

    def deadline_result(self, case):
        return self._no_model(case, "TOTAL_TIME_BUDGET", "已达到总运行时限，按保底草稿输出")

    def _process(self, case, log):
        case_id = case["case_id"]
        bad = check_case(case)
        if bad:
            log["steps"].append({"step": "check_case", "ok": False, "detail": bad})
            return _failed(case_id, "INVALID_CASE", bad)
        log["source_ids"] = [s["source_id"] for s in case["sources"]]
        budget = CallBudget(self.call_budget)
        f = self.flags
        steps = plan_steps(budget.limit, f)
        log["plan"] = steps

        # 1. 事实与问题抽取（失败时降级为仅用规则提示，不中断）；给后续步骤预留调用次数
        extraction = None
        if steps["extract"]:
            reserve = 1 + (1 if steps["repair"] else 0)
            sub = SubBudget(budget, max(1, budget.remaining - reserve))
            try:
                text = self._chat(self.prompts.extract, "请抽取以下资料中的结构化事实。\n\n" + case_block(case),
                                  "extract", sub, log)
                extraction = parse_json_object(text)
                log["steps"].append({"step": "extract", "ok": True})
            except (ModelCallError, ParseError) as exc:
                log["steps"].append({"step": "extract", "ok": False, "detail": str(exc)[:200]})

        # 2. 鉴别知识检索（只注入通用鉴别思路，明确标注非本例事实）
        knowledge = None
        if self.retriever is not None:
            try:
                hits = self.retriever.search(_rag_query(case, extraction), config.RAG_TOP_K)
                log["_hits"] = hits
                log["rag"] = [{"id": h["card"]["id"], "score": h["score"], "method": h["method"]} for h in hits]
                knowledge = format_cards(hits) or None
            except Exception as exc:  # 检索失败不影响生成
                log["steps"].append({"step": "rag", "ok": False, "detail": str(exc)[:200]})

        # 3. 生成（可多候选，按规则评分择优）
        if f["hints"]:
            user = generate_user(case, extraction, measurement_table(case["sources"]),
                                 find_missing_items(case["sources"]), knowledge)
            system = self.prompts.generate
        else:
            user = generate_user(case)
            system = self.prompts.baseline
        raw = self._chat(system, user, "generate", budget, log)
        best_record, best_issues = self._evaluate(raw, case, log, "generate")
        best_raw = raw
        reserve = 1 if steps["repair"] else 0
        for n in range(1, self.n_candidates):
            if budget.remaining <= reserve:
                break
            try:
                raw_n = self._chat(system, user, f"generate_{n + 1}", SubBudget(budget, 1), log,
                                   params={"temperature": 0.7, "seed": config.RANDOM_SEED + n})
            except ModelCallError as exc:
                log["steps"].append({"step": f"generate_{n + 1}", "ok": False, "detail": exc.message[:200]})
                break
            rec_n, iss_n = self._evaluate(raw_n, case, log, f"generate_{n + 1}")
            if _rank(rec_n, iss_n) < _rank(best_record, best_issues):
                best_record, best_issues, best_raw = rec_n, iss_n, raw_n
                log["chosen_candidate"] = n + 1
        log["validation_initial"] = _codes(best_issues)

        # 4. 定向修复：只把 error 级问题反馈给模型，择优保留
        rounds = 0
        while (steps["repair"] and rounds < config.MAX_REPAIR_ROUNDS and repairable(best_issues)
               and budget.remaining > 0):
            rounds += 1
            previous = best_record if best_record is not None else best_raw
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

        if best_record is None:
            log["validation_final"] = _codes(best_issues)
            return _failed(case_id, "OUTPUT_PARSE_ERROR", "模型输出无法解析为 JSON 对象")
        if not has_hard_errors(best_issues):
            best_issues = best_issues + self.aligner.check(best_record, case)  # 仅记录，不改变结果
        log["validation_final"] = _codes(best_issues)
        cov = must_not_miss_coverage(extraction, best_record)
        if cov is not None:
            log["must_not_miss"] = cov
        if has_hard_errors(best_issues):
            detail = "；".join(i["detail"] for i in best_issues if i["severity"] == "error")[:300]
            return _failed(case_id, "OUTPUT_SCHEMA_ERROR", detail)
        if self.flags["repair"] and has_fact_errors(best_issues):
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
