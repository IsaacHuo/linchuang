"""确定性保底记录：模型不可用或输出始终无法通过硬校验时，按资料原文整理一份结构完整的首程草稿。

原则：只做摘录与拼接，不做任何新的医学推断；数值、时间、否定词全部来自原文且按完整分句截取，
保证不会触发"编造数值"类规则。run_log 中标记 fallback=true，评审可据此区分。
"""
import re

from .facts import find_missing_items

_CLAUSE = re.compile(r"[^。；]+[。；]?")
_DX = re.compile(r"初步诊断|初步判断|入院诊断")
_PREFIX = re.compile(r"^[^：:]{1,24}[：:]")


def _clip(text: str, limit: int) -> str:
    """按完整分句截取，不在数值中间断开。"""
    text = text.strip()
    if len(text) <= limit:
        return text
    out = ""
    for c in _CLAUSE.findall(text):
        if len(out) + len(c) > limit:
            break
        out += c
    if not out:  # 首句过长：退到中文逗号边界
        for c in re.findall(r"[^，]+，?", text):
            if len(out) + len(c) > limit:
                break
            out += c
    return (out or text[:0]).rstrip("，；") + ("……" if out else "")


def _body(text: str) -> str:
    return _PREFIX.sub("", text.strip(), count=1).strip()


def _pick(sources, *keys):
    for s in sources:
        head = s["content"][:12]
        if any(k in head for k in keys):
            return s
    return None


def build_fallback(case: dict, hits=None) -> dict:
    sources = case["sources"]
    dx = next((s for s in sources if _DX.search(s["content"][:30])), None)
    facts = [s for s in sources if s is not dx]
    missing = find_missing_items(sources)

    # 病例特点：逐条摘录（每条按分句截取）
    feat_parts = []
    budget = 900
    for s in facts:
        piece = _clip(s["content"], 160)
        if not piece:
            continue
        if budget - len(piece) < 0:
            break
        feat_parts.append(f"{piece}（{s['source_id']}）")
        budget -= len(piece)
    case_features = "根据现有资料整理本例特点如下：" + " ".join(feat_parts)
    if missing:
        case_features += " 以下资料不详或未提供：" + "、".join(dict.fromkeys(i for i, _ in missing)) + "。"

    # 拟诊讨论：只复述资料中的初步判断，不新增诊断
    if dx:
        disc = (f"资料中的入院初步判断为：{_clip(_body(dx['content']), 300)}（{dx['source_id']}）。"
                "上述均为初步判断，尚需进一步检查明确，不能视为确定诊断。")
    else:
        disc = "现有资料尚未给出初步判断，资料不足以形成明确结论，病因待查。"
    names = []
    for h in hits or []:
        for d in h["card"].get("differentials", []):
            if d.get("name") and d["name"] not in names:
                names.append(d["name"])
    if names:
        disc += "结合本例主要表现，鉴别时需考虑的方向包括：" + "、".join(names[:6]) + "，各方向的支持与不支持依据需结合补充资料逐一核对。"
    disc += "目前资料尚不足以确定病因，需结合进一步检查结果综合分析。"

    # 诊疗计划：原则性安排
    todo = _pick(sources, "备注", "待完善", "拟完善")
    plan = "①拟补充核实：" + ("、".join(dict.fromkeys(i for i, _ in missing)) if missing else "起病经过、既往史及用药史等病史细节") + "；"
    plan += "②拟完善检查：" + (_clip(_body(todo["content"]), 200).rstrip("。") if todo else "根据鉴别需要完善相关实验室及影像学检查") + "；"
    plan += "③处理原则：在诊断明确前以对症支持、维持生命体征稳定为原则，具体方案由主管医师根据检查结果确定；"
    plan += "④观察与复查：监测生命体征及主要症状变化，复查本次异常的检查指标，依据结果及时调整诊疗安排。"

    # 依据说明：3—5 条，至少 1 条推断
    evidence = []
    for keys in (("主诉",), ("现病史",), ("辅助检查",), ("专科情况", "体格检查", "生命体征")):
        s = _pick(facts, *keys)
        if s and all(s["source_id"] not in e["source_ids"] for e in evidence):
            evidence.append({"statement": _clip(_body(s["content"]), 80) or _clip(s["content"], 80),
                             "source_ids": [s["source_id"]], "kind": "fact"})
    for s in facts:
        if len(evidence) >= 3:
            break
        if all(s["source_id"] not in e["source_ids"] for e in evidence):
            evidence.append({"statement": _clip(s["content"], 80), "source_ids": [s["source_id"]], "kind": "fact"})
    evidence = [e for e in evidence if e["statement"]][:4]
    ref = dx or sources[0]
    evidence.append({"statement": "入院初步判断尚需进一步检查明确，现有资料不足以确定病因" if dx
                     else "现有资料不足以形成明确判断，病因待查",
                     "source_ids": [ref["source_id"]], "kind": "inference"})

    review = [f"{item}未提供（{sid}），需补充核实" for item, sid in missing][:9]
    review.append("本记录为模型不可用或输出未通过校验时按资料自动整理的保底草稿，需医生全面审核")
    return {"case_features": case_features, "diagnostic_discussion": disc, "plan": plan,
            "evidence": evidence, "review_items": review}
