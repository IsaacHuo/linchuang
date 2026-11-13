"""打包提交文件：<队伍编号>_algorithm.zip 与 <队伍编号>_results.zip。

  python tools/pack.py --team T001
算法包根目录直接包含 main.py 等文件（不套目录）；结果包含 results.json（即 output.json 原样）与 run_log.json。
"""
import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.case_io import InputError, load_input
from src.runner import code_fingerprint, summarize
from tools.check_output import check

ALGO_FILES = ["main.py", "requires.txt", ".env.example", "README.md"]
ALGO_DIRS = ["src", "prompts", "tools", "tests", "data"]
SKIP = {"__pycache__", ".pytest_cache"}


def submission_results():
    out = ROOT / "output.json"
    log = ROOT / "run_log.json"
    if not out.exists() and not log.exists():
        return None
    output_bytes, log_bytes = out.read_bytes(), log.read_bytes()
    results, run = json.loads(output_bytes), json.loads(log_bytes)
    if not isinstance(run, dict) or run.get("exit_code") != 0:
        raise ValueError("运行日志缺少成功退出记录")
    for key, data in (("output_sha256", output_bytes), ("input_sha256", (ROOT / "input.json").read_bytes())):
        if run.get(key) != hashlib.sha256(data).hexdigest():
            raise ValueError(f"运行日志的 {key} 与当前文件不一致")
    if run.get("code_fingerprint") != code_fingerprint():
        raise ValueError("运行日志的源码和提示词指纹与当前版本不一致")
    errors, _ = check(load_input(ROOT / "input.json"), results)
    if errors:
        raise ValueError(errors[0])
    logs = run["cases"]
    if [(r["case_id"], r["status"], r["error"]) for r in results] != [
            (c["case_id"], c["status"], c["error"]) for c in logs]:
        raise ValueError("病例结果与日志中的编号、状态或错误信息不一致")
    limit = run["limits"]["max_calls_per_case"]
    for case in logs:
        numbers = [call["call_no"] for call in case["calls"]]
        if numbers != list(range(1, case["model_calls"] + 1)) or case["model_calls"] > limit:
            raise ValueError(f"病例 {case['case_id']} 的调用次数或编号不一致")
    if run["summary"] != summarize(results, logs):
        raise ValueError("运行摘要与病例结果或调用日志不一致")
    return output_bytes, log_bytes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", required=True, help="队伍编号")
    a = ap.parse_args()
    try:
        artifacts = submission_results()
    except (OSError, ValueError, KeyError, TypeError, InputError) as exc:
        print(f"无法生成提交包：{exc}。请用当前源码和输入重新运行 python main.py。", file=sys.stderr)
        return 1
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

    if artifacts is not None:
        res = dist / f"{a.team}_results.zip"
        with zipfile.ZipFile(res, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("results.json", artifacts[0])
            z.writestr("run_log.json", artifacts[1])
        print(f"已生成 {res}")
    else:
        print("未找到 output.json / run_log.json，跳过结果包（先运行 python main.py）")
    if (ROOT / ".env").exists():
        print("注意：.env 未打入算法包（主办方会自行放入）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
