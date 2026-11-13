"""提示词加载与用户消息构造。"""
import hashlib
import json
from pathlib import Path

PROMPT_FILES = ("common_rules.txt", "system_baseline.txt", "system_extract.txt",
                "system_generate.txt", "system_repair.txt", "manifest.json")


class PromptSet:
    def __init__(self, prompt_dir: Path):
        raw = {name: (prompt_dir / name).read_text(encoding="utf-8") for name in PROMPT_FILES}
        self.hashes = {n: hashlib.sha256(t.encode("utf-8")).hexdigest()[:16] for n, t in raw.items()}
        self.version = json.loads(raw["manifest.json"]).get("prompt_version", "unknown")
        rules = raw["common_rules.txt"].strip()
        self.baseline = raw["system_baseline.txt"].strip()
        self.extract = raw["system_extract.txt"].strip()
        self.generate = raw["system_generate.txt"].replace("{COMMON_RULES}", rules).strip()
        self.repair = raw["system_repair.txt"].replace("{COMMON_RULES}", rules).strip()


def case_block(case: dict) -> str:
    lines = [f"病例编号：{case['case_id']}",
             f"资料截止点：{case['record_cutoff']}（以下资料均为截止点及之前已可获得的信息）",
             "资料片段："]
    for src in case["sources"]:
        lines.append(f"[{src['source_id']}] {src['content'].strip()}")
    return "\n".join(lines)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


def generate_user(case: dict, extraction=None, measurements=None, missing=None) -> str:
    parts = [case_block(case)]
    if extraction:
        parts.append("【结构化事实（上一步抽取，供组织内容参考；如与原文不一致，以原文为准）】\n" + _dump(extraction))
    if measurements:
        rows = [f"{r['source_id']}：" + "；".join(r["values"]) for r in measurements]
        parts.append("【资料中的数值原文（引用时必须与此完全一致，不得出现此外的检验或体征数值）】\n" + "\n".join(rows))
    if missing:
        parts.append("【资料明确未提供的项目（只能写\u201c未提供/不详\u201d，不得写成正常或否认）】\n" + "、".join(f"{i}（{s}）" for i, s in missing))
    parts.append("请按要求生成该病例的首次病程记录 JSON。")
    return "\n\n".join(parts)


def repair_user(case: dict, previous, issues) -> str:
    prev = previous if isinstance(previous, str) else _dump(previous)
    problems = "\n".join(f"{n}. [{i['code']}] {i['field']}：{i['detail']}" for n, i in enumerate(issues, 1))
    return "\n\n".join([case_block(case), "【上一版输出】\n" + prev[:6000], "【需要修正的问题】\n" + problems,
                        "请返回修正后的完整 JSON 对象。"])
