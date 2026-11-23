"""输入适配：把各种形态的病例 JSON 统一成 [{case_id, record_cutoff, sources[{source_id, content}]}]。

支持：
1. 赛题技术规范的标准数组（原样通过）；
2. 练习集形态：单个 {"入院记录": {...}}、其数组、数组嵌套数组、或直接是入院记录对象；
3. 外层包一层 {"cases": [...]} 之类的容器对象；
4. 括号不配对等常见笔误（练习集 07/08 号文件即此情况）：做有限修复并记录。

转换只做切分和拼接，不改写任何原文措辞与数值；姓名、住址不进入模型提示（最小必要）。
"""
import json
import re

RECORD_KEY = "入院记录"
ID_KEYS = ("case_id", "病例编号", "病例ID", "病例id", "id", "ID")
DROP_GENERAL = {"姓名", "现住址", "住址", "联系电话", "电话", "身份证号", "住院号", "病案号"}
HISTORY_KEYS = ("主诉", "现病史", "既往史", "个人史", "婚育史", "月经史生育史", "月经史", "生育史", "家族史")
VITAL_KEY = "生命体征"
SPECIALTY_KEY = "专科情况"
SKIP_KEYS = {"记录医师"}
SUPPLEMENT = re.compile(r"补充病史[：:]")


class AdapterError(ValueError):
    """输入整体无法识别。"""


# ---------------------------------------------------------------- 容错解析
def lenient_loads(text: str):
    """返回 (对象, 修复说明或 None)。先严格解析，失败后只尝试括号配对类修复。"""
    text = text.lstrip("\ufeff")
    try:
        return json.loads(text), None
    except json.JSONDecodeError as first:
        err = first
    s = text.strip()
    candidates = []
    if s.startswith("[") and not s.endswith("]"):
        candidates.append((s + "\n]", "补全缺失的结尾 ]"))
    if s.endswith("]") and not s.startswith("["):
        candidates.append((s[:-1], "去掉多余的结尾 ]"))
        candidates.append(("[" + s, "补全缺失的开头 ["))
    if s.endswith("}") and s.startswith("{"):
        candidates.append(("[" + s + "]", "多个顶层对象合并为数组"))
        candidates.append(("[" + re.sub(r"\}\s*\{", "},{", s) + "]", "多个顶层对象合并为数组"))
    for cand, note in candidates:
        try:
            return json.loads(cand), note
        except json.JSONDecodeError:
            continue
    raise err


# ---------------------------------------------------------------- 拍平
def _is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, dict)) and not v)


def flatten(v) -> str:
    if isinstance(v, dict):
        parts = []
        for k, x in v.items():
            if _is_empty(x):
                continue
            parts.append(f"{k}：{flatten(x)}")
        return "；".join(parts)
    if isinstance(v, list):
        items = [flatten(x) for x in v if not _is_empty(x)]
        if len(items) <= 1:
            return "".join(items)
        return "；".join(f"{i}. {t}" for i, t in enumerate(items, 1))
    return str(v).strip()


def _end(text: str) -> str:
    return text if text.endswith(("。", "；", "）", ".")) else text


# ---------------------------------------------------------------- 入院记录 → sources
def record_to_sources(rec: dict):
    """返回 (sources, source_map, record_cutoff)。source_map: source_id → 原字段路径。"""
    segs = []  # (path, content)

    general = rec.get("一般项目")
    if isinstance(general, dict):
        kept = []
        for k, v in general.items():
            if k in DROP_GENERAL or _is_empty(v):
                continue
            kept.append(f"{k}：{flatten(v)}")
        if kept:
            segs.append(("一般项目", "基本信息：" + "；".join(kept)))

    for key in HISTORY_KEYS:
        if key not in rec:
            continue
        val = rec[key]
        if _is_empty(val):
            continue  # 空字段（如婴幼儿的婚育史）不入资料，避免被当作"未提供"写进待核事项
        text = flatten(val)
        if key == "现病史":
            m = SUPPLEMENT.search(text)
            if m and m.start() > 0:
                segs.append(("现病史", "现病史：" + text[:m.start()].strip()))
                segs.append(("现病史.补充病史", text[m.start():].strip()))
                continue
        segs.append((key, f"{key}：{text}"))

    exam = rec.get("体格检查")
    if isinstance(exam, dict):
        vit = exam.get(VITAL_KEY)
        if isinstance(vit, dict) and vit:
            parts = [f"{k}：{flatten(v) if not _is_empty(v) else '未提供'}" for k, v in vit.items()
                     if not (_is_empty(v) and k in ("BMI",))]
            segs.append(("体格检查.生命体征", "生命体征：" + "；".join(parts)))
        elif not _is_empty(vit):
            segs.append(("体格检查.生命体征", "生命体征：" + flatten(vit)))
        others = {k: v for k, v in exam.items() if k not in (VITAL_KEY, SPECIALTY_KEY)}
        body = flatten(others)
        if body:
            segs.append(("体格检查", "体格检查：" + body))
        if not _is_empty(exam.get(SPECIALTY_KEY)):
            segs.append(("体格检查.专科情况", "专科情况：" + flatten(exam[SPECIALTY_KEY])))
    elif not _is_empty(exam):
        segs.append(("体格检查", "体格检查：" + flatten(exam)))

    aux = rec.get("辅助检查")
    if isinstance(aux, dict):
        when = aux.get("检查时间")
        prefix = f"辅助检查（{flatten(when)}）" if not _is_empty(when) else "辅助检查"
        for k, v in aux.items():
            if k == "检查时间" or _is_empty(v):
                continue
            if k == "备注":
                segs.append(("辅助检查.备注", "辅助检查备注：" + flatten(v)))
            else:
                segs.append((f"辅助检查.{k}", f"{prefix}{k}：{flatten(v)}"))
    elif not _is_empty(aux):
        segs.append(("辅助检查", "辅助检查：" + flatten(aux)))

    dx = rec.get("初步诊断")
    if not _is_empty(dx):
        segs.append(("初步诊断", "入院初步诊断（初步判断，尚待进一步检查明确）：" + flatten(dx)))

    handled = {"一般项目", "体格检查", "辅助检查", "初步诊断", *HISTORY_KEYS, *SKIP_KEYS}
    for k, v in rec.items():  # 未知字段兜底：不丢信息
        if k in handled or _is_empty(v):
            continue
        segs.append((k, f"{k}：{flatten(v)}"))

    sources, smap = [], {}
    for i, (path, content) in enumerate(segs, 1):
        sid = f"S{i:02d}"
        sources.append({"source_id": sid, "content": content})
        smap[sid] = path

    cutoff = "T0"
    if isinstance(general, dict):
        for k in ("记录时间", "入院时间"):
            if not _is_empty(general.get(k)):
                cutoff = str(general[k]).strip()
                break
    return sources, smap, cutoff


# ---------------------------------------------------------------- 形态识别
def _looks_like_record(obj: dict) -> bool:
    return any(k in obj for k in ("主诉", "现病史", "一般项目")) and "sources" not in obj


def _unwrap_container(obj: dict):
    """{"cases": [...]} 一类的容器：唯一的列表值且元素为对象。"""
    lists = [v for v in obj.values() if isinstance(v, list) and v and all(isinstance(x, (dict, list)) for x in v)]
    if len(lists) == 1 and RECORD_KEY not in obj and "sources" not in obj and not _looks_like_record(obj):
        return lists[0]
    return None


def _flatten_items(data):
    if isinstance(data, dict):
        inner = _unwrap_container(data)
        if inner is not None:
            return _flatten_items(inner)
        return [data]
    if isinstance(data, list):
        out = []
        for x in data:
            if isinstance(x, list):
                out.extend(_flatten_items(x))
            else:
                out.append(x)
        return out
    raise AdapterError("输入既不是 JSON 数组也不是对象")


def _case_id(obj: dict):
    for k in ID_KEYS:
        v = obj.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    return None


def to_canonical(data):
    """返回 (cases, notes)。cases 中转换得到的病例带 _source_map（只写入日志，不进输出）。"""
    notes = []
    if isinstance(data, list) and not data:
        return [], notes
    items = _flatten_items(data)
    if not isinstance(data, list):
        notes.append("顶层不是数组，已按单个病例/容器对象读取")
    cases = []
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise AdapterError(f"第 {idx} 条记录不是对象")
        cid = _case_id(item)
        if "sources" in item:
            case = dict(item)
            if cid is not None:
                case["case_id"] = cid
        else:
            rec = item.get(RECORD_KEY)
            if not isinstance(rec, dict) and _looks_like_record(item):
                rec = item
            if isinstance(rec, dict):
                sources, smap, cutoff = record_to_sources(rec)
                case = {"case_id": cid or _case_id(rec), "record_cutoff": cutoff,
                        "sources": sources, "_source_map": smap}
                notes.append(f"第 {idx} 条为入院记录格式，已切分为 {len(sources)} 个资料片段")
            else:
                case = dict(item)  # 无法识别：交给单例校验记 INVALID_CASE，不影响其他病例
        if not case.get("case_id"):
            case["case_id"] = f"CASE{idx:03d}"
            notes.append(f"第 {idx} 条缺少 case_id，已按顺序编号为 {case['case_id']}")
        cases.append(case)
    return cases, notes
