"""基于规则的事实抽取：数值、缺失项。用于给模型提示，也用于生成后校验。"""
import re

# 临床计量单位（长的放前面，避免被短单位截断）
MEASURE_UNITS = [
    r"×\s*10\s*\^?\s*[0-9⁹¹²]+\s*/\s*L", r"mmol/L", r"μmol/L", r"µmol/L", r"umol/L",
    r"mg/dL", r"mg/L", r"g/L", r"ng/mL", r"pg/mL", r"IU/L", r"U/L", r"mmHg",
    r"次/分", r"bpm", r"℃", r"°C", r"mL", r"ml", r"kg", r"mV", r"mm", r"cm",
    r"s(?![A-Za-z])", r"%", r"岁", r"级",
]
TIME_UNITS = [r"小时", r"分钟", r"天", r"日", r"周", r"个月", r"月", r"年"]

NUM = r"\d+(?:\.\d+)?"
_MEASURE_RE = re.compile(
    r"(?<![\d.])(" + NUM + r")(?:\s*/\s*(" + NUM + r"))?\s*(" + "|".join(MEASURE_UNITS) + r")"
)
_TIME_RE = re.compile(r"(?<![\d.])(" + NUM + r")\s*(" + "|".join(TIME_UNITS) + r")")
_ANY_NUM_RE = re.compile(NUM)


def norm_num(token: str) -> str:
    if "." in token:
        token = token.rstrip("0").rstrip(".")
    return token.lstrip("0") or "0"


def norm_unit(unit: str) -> str:
    u = re.sub(r"\s+", "", unit)
    u = u.replace("µ", "μ").replace("umol", "μmol").replace("°C", "℃").replace("ml", "mL")
    u = u.replace("⁹", "9").replace("^", "")
    return u


def find_measurements(text: str):
    """返回 [(数值, 单位, 原文片段)]；血压类 156/92 mmHg 拆成两个数值。"""
    out = []
    for m in _MEASURE_RE.finditer(text):
        unit = norm_unit(m.group(3))
        out.append((norm_num(m.group(1)), unit, m.group(0)))
        if m.group(2):
            out.append((norm_num(m.group(2)), unit, m.group(0)))
    return out


def find_time_spans(text: str):
    return [(norm_num(m.group(1)), m.group(2), m.group(0)) for m in _TIME_RE.finditer(text)]


def all_numbers(text: str) -> set:
    return {norm_num(t) for t in _ANY_NUM_RE.findall(text)}


_MISSING_AFTER = re.compile(r"(?:未提供|未记录|未描述|未注明)([^，,；;。：:（）()]+)")
_MISSING_BEFORE = re.compile(r"([^，,；;。：:（）()]+?)(?:均)?(?:未提供|不详|未记录)")
_SPLIT = re.compile(r"以及|[、及和与或/]")
_STOP = {"其他", "其余", "具体", "相关", "既往", "结果", "情况", "资料", "其他体征"}


def _clean_item(item: str) -> str:
    item = item.strip(" 　：:。")
    item = re.sub(r"^(?:其他|其余|具体|相关|现有记录)", "", item)
    item = re.sub(r"(?:结果|情况|资料|报告)$", "", item)
    return item.strip()


def find_missing_items(sources) -> list:
    """抽取资料中明确写出的"未提供/不详"项，返回 [(条目, source_id)]，去重保序。"""
    seen, out = set(), []
    for src in sources:
        text = src.get("content", "")
        chunks = [m.group(1) for m in _MISSING_AFTER.finditer(text)]
        chunks += [m.group(1) for m in _MISSING_BEFORE.finditer(text)]
        for chunk in chunks:
            for part in _SPLIT.split(chunk):
                item = _clean_item(part)
                if len(item) < 2 or item in _STOP or len(item) > 20:
                    continue
                if item not in seen:
                    seen.add(item)
                    out.append((item, src["source_id"]))
    return out


def measurement_table(sources) -> list:
    """[{source_id, values}]：每个片段中出现的带单位数值原文，供提示词使用。"""
    rows = []
    for src in sources:
        spans = []
        for _, _, raw in find_measurements(src.get("content", "")):
            raw = raw.strip()
            if raw not in spans:
                spans.append(raw)
        if spans:
            rows.append({"source_id": src["source_id"], "values": spans})
    return rows
