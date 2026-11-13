"""把模型回复解析成 JSON 对象（容忍代码围栏、思考标签和前后说明文字）。"""
import json
import re

_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class ParseError(Exception):
    pass


def _balanced_objects(text: str):
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break
        start = text.find("{", start + 1)


def parse_json_object(text: str) -> dict:
    text = _THINK.sub("", text or "").strip()
    candidates = [text] + [m.group(1).strip() for m in _FENCE.finditer(text)]
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    for chunk in _balanced_objects(text):
        try:
            obj = json.loads(chunk)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ParseError("模型回复中未找到可解析的 JSON 对象")


def unwrap_record(obj: dict) -> dict:
    """兼容模型多包一层 medical_record 的情况。"""
    if "case_features" not in obj and isinstance(obj.get("medical_record"), dict):
        return obj["medical_record"]
    return obj
