"""把赛方练习集（每例一个入院记录 JSON 文件）合并转换为标准 input 数组。

  python tools/convert_official.py [--src DIR] [--out FILE]
默认读 data/official_practice/临床智录_练习病历数据集JSON/*.json，写 data/official_practice_cases.json。
case_id 取文件名前缀编号（OFF01…），转换规则与正式运行时的 src/adapter.py 完全相同。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.adapter import lenient_loads, to_canonical  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(ROOT / "data" / "official_practice" / "临床智录_练习病历数据集JSON"))
    ap.add_argument("--out", default=str(ROOT / "data" / "official_practice_cases.json"))
    a = ap.parse_args()
    cases, meta = [], {}
    for f in sorted(Path(a.src).glob("*.json")):
        raw, fix = lenient_loads(f.read_text(encoding="utf-8-sig"))
        items, _ = to_canonical(raw)
        for n, case in enumerate(items, 1):
            cid = f"OFF{f.name[:2]}" + (f"_{n}" if len(items) > 1 else "")
            case["case_id"] = cid
            meta[cid] = {"file": f.name, "json_fix": fix, "source_map": case.pop("_source_map", {})}
            cases.append(case)
        print(f"{f.name}: {len(items)} 例" + (f"（已{fix}）" if fix else ""))
    out = Path(a.out)
    out.write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"共 {len(cases)} 例 -> {out}")


if __name__ == "__main__":
    main()
