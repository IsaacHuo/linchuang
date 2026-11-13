"""提交前自检：核对 output.json 与 input.json 是否符合输出协议，并列出内容规则提示。

  python tools/check_output.py [--input input.json] [--output output.json]
退出码 0 = 协议合格；1 = 存在协议错误。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.validate import RECORD_FIELDS, audit_record  # noqa: E402


def check(cases, outputs):
    errors, notes = [], []
    if not isinstance(outputs, list):
        return ["output.json 顶层不是数组"], notes
    if len(outputs) != len(cases):
        errors.append(f"输出条数 {len(outputs)} 与输入 {len(cases)} 不一致")
    for i, (case, out) in enumerate(zip(cases, outputs)):
        cid = case.get("case_id")
        if not isinstance(out, dict):
            errors.append(f"第 {i} 条不是对象")
            continue
        if out.get("case_id") != cid:
            errors.append(f"第 {i} 条 case_id 为 {out.get('case_id')}，应为 {cid}")
        if set(out) != {"case_id", "status", "medical_record", "error"}:
            errors.append(f"{cid}: 字段应恰为 case_id/status/medical_record/error，实际 {sorted(out)}")
        st = out.get("status")
        if st == "success":
            rec = out.get("medical_record")
            if out.get("error") is not None:
                errors.append(f"{cid}: 成功记录的 error 应为 null")
            if not isinstance(rec, dict) or set(rec) != set(RECORD_FIELDS):
                errors.append(f"{cid}: medical_record 字段应恰为 {list(RECORD_FIELDS)}")
                continue
            for iss in audit_record(rec, case):
                line = f"{cid}: [{iss['severity']}:{iss['code']}] {iss['field']} {iss['detail']}"
                if iss["code"] in ("SCHEMA", "SECTION_EMPTY", "SOURCE_ID_INVALID"):
                    errors.append(line)
                else:
                    notes.append(line)
        elif st == "failed":
            err = out.get("error")
            if out.get("medical_record") is not None:
                errors.append(f"{cid}: 失败记录的 medical_record 应为 null")
            if not (isinstance(err, dict) and isinstance(err.get("code"), str) and isinstance(err.get("message"), str)):
                errors.append(f"{cid}: 失败记录的 error 须含字符串 code 与 message")
            notes.append(f"{cid}: 失败 {err}")
        else:
            errors.append(f"{cid}: status 必须是 success 或 failed")
    return errors, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(ROOT / "input.json"))
    ap.add_argument("--output", default=str(ROOT / "output.json"))
    a = ap.parse_args()
    cases = json.loads(Path(a.input).read_text(encoding="utf-8-sig"))
    try:
        outputs = json.loads(Path(a.output).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"output.json 不是合法 JSON：{exc}")
        return 1
    errors, notes = check(cases, outputs)
    for n in notes:
        print("提示 ", n)
    for e in errors:
        print("错误 ", e)
    print(f"病例 {len(cases)} 例；协议错误 {len(errors)} 项；内容提示 {len(notes)} 项")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
