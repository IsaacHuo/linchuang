"""打包提交文件：<队伍编号>_algorithm.zip 与 <队伍编号>_results.zip。

  python tools/pack.py --team T001
算法包根目录直接包含 main.py 等文件（不套目录）；结果包含 results.json（即 output.json 原样）与 run_log.json。
"""
import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALGO_FILES = ["main.py", "requires.txt", ".env.example", "README.md"]
ALGO_DIRS = ["src", "prompts", "tools", "tests", "data"]
SKIP = {"__pycache__", ".pytest_cache"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", required=True, help="队伍编号")
    a = ap.parse_args()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)

    algo = dist / f"{a.team}_algorithm.zip"
    with zipfile.ZipFile(algo, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ALGO_FILES:
            z.write(ROOT / name, name)
        for d in ALGO_DIRS:
            for p in sorted((ROOT / d).rglob("*")):
                if p.is_file() and not (SKIP & set(p.parts)) and p.suffix != ".pyc":
                    z.write(p, p.relative_to(ROOT).as_posix())
    print(f"已生成 {algo}")

    out, log = ROOT / "output.json", ROOT / "run_log.json"
    if out.is_file() and log.is_file():
        json.loads(out.read_text(encoding="utf-8"))
        res = dist / f"{a.team}_results.zip"
        with zipfile.ZipFile(res, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(out, "results.json")
            z.write(log, "run_log.json")
        print(f"已生成 {res}")
    else:
        print("未找到 output.json / run_log.json，跳过结果包（先运行 python main.py）")
    if (ROOT / ".env").exists():
        print("注意：.env 未打入算法包（主办方会自行放入）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
