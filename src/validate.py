"""生成后校验：Schema、source_id、数值回溯、已实施/确诊/缺失改正常等规则。

同一套规则同时用于：①流水线内触发定向修复；②实验脚本对各版本输出做同口径统计。
"""
import re

from .facts import all_numbers, find_measurements, find_missing_items, find_time_spans

TEXT_FIELDS = ("case_features", "diagnostic_discussion", "plan")
RECORD_FIELDS = TEXT_FIELDS + ("evidence", "review_items")
KINDS = ("fact", "inference")


def issue(code, severity, field, detail):
    return {"code": code, "severity": severity, "field": field, "detail": detail}


# ---------------------------------------------------------------- 确定性修正
def normalize_record(rec: dict, case: dict, max_review_items: int = 10, augment_missing: bool = True):
    """不改变医学内容的结构性修正，返回 (新记录, 修正说明列表)。"""
    fixes = []
    valid_ids = {s["source_id"] for s in case["sources"]}
    out = {}
    for f in TEXT_FIELDS:
        v = rec.get(f)
        out[f] = v.strip() if isinstance(v, str) else v

    evidence, seen = [], set()
    raw_ev = rec.get("evidence")
    if isinstance(raw_ev, dict):
        raw_ev = [raw_ev]
    for item in raw_ev if isinstance(raw_ev, list) else []:
        if not isinstance(item, dict):
            continue
        stmt = item.get("statement")
        stmt = stmt.strip() if isinstance(stmt, str) else ""
        ids = item.get("source_ids")
        if isinstance(ids, str):
            ids = [ids]
        ids = [i.strip() for i in ids if isinstance(i, str)] if isinstance(ids, list) else []
        bad = [i for i in ids if i not in valid_ids]
        if bad:
            fixes.append(f"evidence 移除不存在的 source_id：{bad}")
        ids = list(dict.fromkeys(i for i in ids if i in valid_ids))
        if not stmt or not ids:
            fixes.append("evidence 移除缺少陈述或有效来源的条目")
            continue
        kind = str(item.get("kind", "")).strip().lower()
        if kind not in KINDS:
            fixes.append(f"evidence kind 非法（{kind}），按 inference 处理")
            kind = "inference"
        if stmt in seen:
            continue
        seen.add(stmt)
        evidence.append({"statement": stmt, "source_ids": ids, "kind": kind})
    if len(evidence) > 5:
        fixes.append(f"evidence {len(evidence)} 条，保留前 5 条并保留至少一条已有推断")
        first_inference = next((item for item in evidence if item["kind"] == "inference"), None)
        evidence = evidence[:5]
        if first_inference and all(item["kind"] == "fact" for item in evidence):
            evidence[-1] = first_inference
    out["evidence"] = evidence

    raw_rv = rec.get("review_items")
    if isinstance(raw_rv, str):
        raw_rv = [raw_rv]
    review = []
    for it in raw_rv if isinstance(raw_rv, list) else []:
        if isinstance(it, dict):
            it = it.get("item") or it.get("text") or ""
        if isinstance(it, str) and it.strip() and it.strip() not in review:
            review.append(it.strip())
    if augment_missing:
        joined = "".join(review)
        added = []
        for item, sid in find_missing_items(case["sources"]):
            if item not in joined and len(review) < max_review_items:
                review.append(f"{item}：资料未提供（{sid}），需补充核实")
                added.append(item)
        if added:
            fixes.append(f"review_items 补充资料中明确缺失的项目：{added}")
    out["review_items"] = review
    return out, fixes


# ---------------------------------------------------------------- Schema
def schema_issues(rec, case: dict) -> list:
    issues = []
    if not isinstance(rec, dict):
        return [issue("SCHEMA", "error", "medical_record", "不是 JSON 对象")]
    valid_ids = {s["source_id"] for s in case["sources"]}
    for f in TEXT_FIELDS:
        v = rec.get(f)
        if not isinstance(v, str) or not v.strip():
            issues.append(issue("SECTION_EMPTY", "error", f, "正文缺失或为空"))
    ev = rec.get("evidence")
    if not isinstance(ev, list):
        issues.append(issue("SCHEMA", "error", "evidence", "evidence 必须是数组"))
    else:
        for i, e in enumerate(ev):
            if not isinstance(e, dict):
                issues.append(issue("SCHEMA", "error", "evidence", f"第 {i+1} 条不是对象"))
                continue
            if not isinstance(e.get("statement"), str) or not e["statement"].strip():
                issues.append(issue("SCHEMA", "error", "evidence", f"第 {i+1} 条 statement 为空"))
            ids = e.get("source_ids")
            if not isinstance(ids, list) or not ids or not all(isinstance(x, str) for x in ids):
                issues.append(issue("SCHEMA", "error", "evidence", f"第 {i+1} 条 source_ids 必须是非空字符串数组"))
            else:
                bad = [x for x in ids if x not in valid_ids]
                if bad:
                    issues.append(issue("SOURCE_ID_INVALID", "error", "evidence", f"第 {i+1} 条引用了本病例不存在的编号 {bad}"))
            if e.get("kind") not in KINDS:
                issues.append(issue("SCHEMA", "error", "evidence", f"第 {i+1} 条 kind 必须是 fact 或 inference"))
    rv = rec.get("review_items")
    if not isinstance(rv, list) or not all(isinstance(x, str) for x in rv):
        issues.append(issue("SCHEMA", "error", "review_items", "review_items 必须是字符串数组"))
    return issues


# ---------------------------------------------------------------- 内容规则
_IMPLEMENTED = re.compile(r"已(?:予|给予|行|完成|使用|应用|静滴|静推|输注|输血|启动|开始|实施)|给予了")
_PRIOR_CONTEXT = re.compile(r"院外|门诊|急诊|入院前|此前|外院|曾|既往")
_CONFIRM = re.compile(r"确诊为|已确诊|诊断明确|明确诊断为|诊断成立")
_NEG_PREFIX = re.compile(r"(?:未|不|尚|待|需|以|能否|可否|无法|难以)[^，。；]{0,3}$")
_NORMAL_WORDS = r"(?:正常|无异常|未见异常|阴性|无明显异常|无特殊)"


def content_issues(rec: dict, case: dict) -> list:
    """在 schema 通过（至少字段类型正确）的记录上做内容规则检查。"""
    issues = []
    src_text = "\n".join(s.get("content", "") for s in case["sources"])
    src_nums = all_numbers(src_text)
    src_pairs = {(n, u) for n, u, _ in find_measurements(src_text)}
    src_times = {(n, u) for n, u, _ in find_time_spans(src_text)}

    texts = {f: rec.get(f) or "" for f in TEXT_FIELDS}
    for i, e in enumerate(rec.get("evidence") or []):
        if isinstance(e, dict) and isinstance(e.get("statement"), str):
            texts[f"evidence[{i}]"] = e["statement"]

    # 1) 数值回溯：带单位的数值必须能在资料中找到
    for field, text in texts.items():
        for num, unit, raw in find_measurements(text):
            if num not in src_nums:
                issues.append(issue("NUMBER_NOT_IN_SOURCE", "error", field, f"数值\u201c{raw.strip()}\u201d在资料中不存在"))
            elif (num, unit) not in src_pairs and unit not in ("岁", "级", "%"):
                issues.append(issue("UNIT_MISMATCH", "warn", field, f"数值\u201c{raw.strip()}\u201d的单位与资料不一致"))
    # 2) 时序回溯：病例特点中的病程时间须来自资料
    for num, unit, raw in find_time_spans(texts["case_features"]):
        if (num, unit) not in src_times:
            issues.append(issue("TIME_NOT_IN_SOURCE", "error", "case_features", f"时间表述\u201c{raw}\u201d在资料中不存在"))
    # 3) 计划不得写成已实施
    for clause in re.split(r"[，,。；;\n]", texts["plan"]):
        m = _IMPLEMENTED.search(clause)
        if m and not _PRIOR_CONTEXT.search(clause) and clause.strip() not in src_text:
            issues.append(issue("PLAN_AS_DONE", "error", "plan", f"计划中出现已实施表述：\u201c{clause.strip()[:40]}\u201d"))
    # 4) 初步判断不得写成确诊
    for field in ("case_features", "diagnostic_discussion"):
        for m in _CONFIRM.finditer(texts[field]):
            before = texts[field][max(0, m.start() - 6):m.start()]
            if _NEG_PREFIX.search(before) or m.group(0) in src_text:
                continue
            issues.append(issue("OVERCONFIDENT_DX", "error", field, f"出现确诊性表述：\u201c{texts[field][max(0, m.start()-10):m.end()+10]}\u201d"))
    # 5) "未提供"不得改写为"正常/否认"
    all_out = "\n".join(texts.values())
    for item, sid in find_missing_items(case["sources"]):
        pat = re.compile(re.escape(item) + r"([^，,。；;\n]{0,4})" + _NORMAL_WORDS + "|否认[^，,。；;\n]{0,6}" + re.escape(item))
        for m in pat.finditer(all_out):
            mid = m.group(1) or ""
            if re.search(r"[不未非勿无]", mid) or m.group(0) in src_text:
                continue
            issues.append(issue("MISSING_AS_NORMAL", "error", "medical_record", f"{sid} 中\u201c{item}\u201d未提供，输出写成了\u201c{m.group(0)}\u201d"))
    # 6) 结构与篇幅（提示性）
    ev = rec.get("evidence") or []
    if len(ev) > 5:
        issues.append(issue("EVIDENCE_COUNT", "error", "evidence", f"evidence {len(ev)} 条，超过 5 条"))
    elif len(ev) < min(3, len(case["sources"])):
        issues.append(issue("EVIDENCE_COUNT", "warn", "evidence", f"evidence 仅 {len(ev)} 条"))
    if ev and not any(isinstance(e, dict) and e.get("kind") == "inference" for e in ev):
        issues.append(issue("NO_INFERENCE", "warn", "evidence", "evidence 未区分推断（无 inference 条目）"))
    total = sum(len(texts[f]) for f in TEXT_FIELDS)
    if total < 300:
        issues.append(issue("LENGTH", "warn", "medical_record", f"三段正文合计 {total} 字，偏短"))
    elif total > 2200:
        issues.append(issue("LENGTH", "warn", "medical_record", f"三段正文合计 {total} 字，偏长"))
    if not rec.get("review_items") and find_missing_items(case["sources"]):
        issues.append(issue("REVIEW_EMPTY", "warn", "review_items", "资料存在明确缺失项，但待核事项为空"))
    return issues


def audit_record(rec, case: dict) -> list:
    issues = schema_issues(rec, case)
    if isinstance(rec, dict) and not any(i["code"] == "SCHEMA" for i in issues):
        issues += content_issues(rec, case)
    return issues


def issue_score(issues) -> int:
    score = 0
    for i in issues:
        if i["severity"] == "error":
            score += 100 if i["code"] in ("SCHEMA", "SECTION_EMPTY", "SOURCE_ID_INVALID") else 10
        else:
            score += 1
    return score


def has_hard_errors(issues) -> bool:
    return any(i["severity"] == "error" and i["code"] in ("SCHEMA", "SECTION_EMPTY", "SOURCE_ID_INVALID") for i in issues)


def has_fact_errors(issues) -> bool:
    return any(i["severity"] == "error" and i["code"] in ("NUMBER_NOT_IN_SOURCE", "MISSING_AS_NORMAL")
               for i in issues)


def repairable(issues) -> list:
    return [i for i in issues if i["severity"] == "error"]
