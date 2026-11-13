"""input.json 读取校验与 output.json 原子写入。"""
import json
import os
import tempfile
from pathlib import Path


class InputError(Exception):
    """输入不符合基本协议，对应退出码 2。"""


def load_input(path: Path) -> list:
    if not path.is_file():
        raise InputError(f"未找到输入文件 {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InputError(f"input.json 无法解析：{exc}") from exc
    if not isinstance(data, list):
        raise InputError("input.json 顶层必须是 JSON 数组")
    seen = set()
    for idx, case in enumerate(data):
        if not isinstance(case, dict):
            raise InputError(f"第 {idx} 条记录不是对象")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise InputError(f"第 {idx} 条记录缺少非空 case_id")
        if case_id in seen:
            raise InputError(f"case_id 重复：{case_id}")
        seen.add(case_id)
    return data


def check_case(case: dict):
    """单条病例校验，返回错误信息；None 表示有效。无效时只记该条失败。"""
    cutoff = case.get("record_cutoff")
    if not isinstance(cutoff, str) or not cutoff.strip():
        return "record_cutoff 缺失或为空"
    sources = case.get("sources")
    if not isinstance(sources, list) or not sources:
        return "sources 缺失或为空数组"
    ids = set()
    for i, src in enumerate(sources):
        if not isinstance(src, dict):
            return f"sources[{i}] 不是对象"
        sid = src.get("source_id")
        if not isinstance(sid, str) or not sid.strip():
            return f"sources[{i}] 缺少非空 source_id"
        if sid in ids:
            return f"source_id 重复：{sid}"
        ids.add(sid)
        if not isinstance(src.get("content"), str):
            return f"sources[{i}].content 不是字符串"
    return None


def write_json_atomic(path: Path, data) -> None:
    """先写同目录临时文件，完成后整体替换，避免半成品被当作结果。"""
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
