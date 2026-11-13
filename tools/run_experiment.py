"""基线版与改进版的同条件对比实验（同一批练习病例、同一模型、同一参数）。

  python tools/run_experiment.py --modes baseline,extract,improved
  python tools/run_experiment.py --endpoint http://127.0.0.1:18080/v1/chat/completions --model mock --tag mock

输出到 experiments/<时间>_<tag>/：各模式 output/run_log、metrics.json、report.md、人工评分表.csv。
自动指标只覆盖可由规则判定的忠实性与格式问题；临床分析质量须由人工评分表补充。
"""
import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402
from src.case_io import load_input, write_json_atomic  # noqa: E402
from src.envfile import load_model_config  # noqa: E402
from src.llm_client import LLMClient  # noqa: E402
from src.pipeline import MODES, Pipeline  # noqa: E402
from src.prompts import PromptSet  # noqa: E402
from src.runner import code_fingerprint, run_cases  # noqa: E402
from src.validate import TEXT_FIELDS, audit_record  # noqa: E402

MODE_DESC = {
    "baseline": "单次调用，赛题示例系统提示",
    "extract": "+事实抽取 +数值/缺失项规则提示 +结构修正",
    "improved": "+生成后规则校验 +定向修复（正式提交版本）",
}
TRACKED = ["NUMBER_NOT_IN_SOURCE", "TIME_NOT_IN_SOURCE", "PLAN_AS_DONE", "OVERCONFIDENT_DX",
           "MISSING_AS_NORMAL", "SOURCE_ID_INVALID", "EVIDENCE_COUNT", "UNIT_MISMATCH", "NO_INFERENCE"]


def metrics_for(cases, results, logs):
    n = len(cases)
    ok = [(c, r) for c, r in zip(cases, results) if r["status"] == "success"]
    issue_counter, clean = Counter(), 0
    ev_ok = inf_ok = rv_ok = 0
    lengths, per_case = [], {}
    for case, res in ok:
        rec = res["medical_record"]
        issues = audit_record(rec, case)
        per_case[case["case_id"]] = issues
        issue_counter.update(i["code"] for i in issues)
        if not any(i["severity"] == "error" for i in issues):
            clean += 1
        ev = rec.get("evidence") or []
        ev_ok += min(3, len(case["sources"])) <= len(ev) <= 5
        inf_ok += any(e.get("kind") == "inference" for e in ev if isinstance(e, dict))
        rv_ok += bool(rec.get("review_items"))
        lengths.append(sum(len(rec.get(f) or "") for f in TEXT_FIELDS))
    for case, res in zip(cases, results):
        if res["status"] != "success":
            per_case[case["case_id"]] = [{"code": res["error"]["code"], "severity": "failed", "field": "-",
                                         "detail": res["error"]["message"]}]
    calls = [len(lg["calls"]) for lg in logs]
    durs = [lg["duration_s"] for lg in logs]
    s = len(ok) or 1
    return {
        "cases": n,
        "success": len(ok),
        "failed_codes": dict(Counter(r["error"]["code"] for r in results if r["status"] != "success")),
        "success_without_rule_errors": clean,
        "issue_counts": {k: issue_counter.get(k, 0) for k in TRACKED},
        "evidence_3_5_rate": round(ev_ok / s, 3),
        "has_inference_rate": round(inf_ok / s, 3),
        "review_items_nonempty_rate": round(rv_ok / s, 3),
        "avg_text_chars": round(sum(lengths) / s, 1),
        "avg_calls_per_case": round(sum(calls) / max(n, 1), 2),
        "avg_seconds_per_case": round(sum(durs) / max(n, 1), 2),
    }, per_case


def write_report(out_dir, meta, all_metrics, all_issues, modes):
    L = [f"# 对比实验报告（{meta['tag']}）", "",
         f"- 时间：{meta['time']}", f"- 模型：{meta['model']}", f"- 病例：{meta['cases']} 例（{meta['case_file']}）",
         f"- 代码指纹：{meta['fingerprint'][:16]}", f"- 生成参数：{json.dumps(config.GEN_PARAMS, ensure_ascii=False)}", "",
         "自动指标由 src/validate.py 的同一套规则统计，只覆盖可规则判定的忠实性与格式问题。", "",
         "## 汇总", "", "| 指标 | " + " | ".join(modes) + " |", "|---|" + "---|" * len(modes)]
    rows = [("说明", lambda m: MODE_DESC.get(m, "")),
            ("成功例数", lambda m: f"{all_metrics[m]['success']}/{all_metrics[m]['cases']}"),
            ("无规则错误例数", lambda m: str(all_metrics[m]["success_without_rule_errors"])),
            ]
    rows += [(f"问题数：{k}", (lambda k: lambda m: str(all_metrics[m]["issue_counts"][k]))(k)) for k in TRACKED]
    rows += [("evidence 3—5 条占比", lambda m: str(all_metrics[m]["evidence_3_5_rate"])),
             ("含 inference 占比", lambda m: str(all_metrics[m]["has_inference_rate"])),
             ("待核事项非空占比", lambda m: str(all_metrics[m]["review_items_nonempty_rate"])),
             ("三段平均字数", lambda m: str(all_metrics[m]["avg_text_chars"])),
             ("平均调用次数/例", lambda m: str(all_metrics[m]["avg_calls_per_case"])),
             ("平均耗时 s/例", lambda m: str(all_metrics[m]["avg_seconds_per_case"])),
             ("失败原因", lambda m: json.dumps(all_metrics[m]["failed_codes"], ensure_ascii=False))]
    for name, fn in rows:
        L.append(f"| {name} | " + " | ".join(fn(m) for m in modes) + " |")
    L += ["", "## 逐例问题", ""]
    for m in modes:
        L.append(f"### {m}")
        for cid, issues in all_issues[m].items():
            bad = [i for i in issues if i["severity"] in ("error", "failed")]
            L.append(f"- {cid}：" + ("；".join(f"[{i['code']}] {i['detail']}" for i in bad) if bad else "无规则错误"))
        L.append("")
    (out_dir / "report.md").write_text("\n".join(L), encoding="utf-8")


def write_scoring_sheet(out_dir, cases, modes):
    """人工评分表：两名队员按附件 2 五个临床维度盲评（评分时隐藏模式列）。"""
    with open(out_dir / "人工评分表.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "mode", "评分人", "事实准确与忠实性", "拟诊讨论与临床分析", "诊疗计划针对性",
                    "结构规范与个体化", "来源说明与待核提示", "严重错误(是/否)", "备注"])
        for c in cases:
            for m in modes:
                w.writerow([c["case_id"], m, "", "", "", "", "", "", "", ""])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "data" / "practice_cases.json"))
    ap.add_argument("--modes", default="baseline,extract,improved")
    ap.add_argument("--env", default=str(config.ENV_FILE))
    ap.add_argument("--endpoint", help="覆盖 .env 中的 ENDPOINT（例如本地模拟服务）")
    ap.add_argument("--model", help="覆盖 .env 中的 MODEL_NAME")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 例（大测试集先小批量试跑）")
    a = ap.parse_args()

    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    for m in modes:
        if m not in MODES:
            sys.exit(f"未知模式 {m}，可选 {list(MODES)}")
    if a.endpoint and a.model:
        model, endpoint = a.model, a.endpoint
    else:
        model, endpoint = load_model_config(Path(a.env))
        model, endpoint = a.model or model, a.endpoint or endpoint
    cases = load_input(Path(a.cases))
    if a.limit > 0:
        cases = cases[:a.limit]
    prompts = PromptSet(config.PROMPT_DIR)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "experiments" / f"{stamp}_{a.tag}"
    out_dir.mkdir(parents=True, exist_ok=True)

    all_metrics, all_issues = {}, {}
    for m in modes:
        client = LLMClient(model, endpoint, config.GEN_PARAMS, config.REQUEST_TIMEOUT_S,
                           config.MAX_RETRIES, config.RETRY_BACKOFF_S)
        print(f"[{m}] 运行 {len(cases)} 例 ...", flush=True)
        results, logs = run_cases(cases, Pipeline(client, prompts, m), config.CONCURRENCY)
        write_json_atomic(out_dir / f"{m}_output.json", results)
        write_json_atomic(out_dir / f"{m}_case_logs.json", logs)
        all_metrics[m], all_issues[m] = metrics_for(cases, results, logs)
        print(f"[{m}] 成功 {all_metrics[m]['success']}/{len(cases)}，无规则错误 {all_metrics[m]['success_without_rule_errors']}")

    meta = {"tag": a.tag, "time": stamp, "model": model, "cases": len(cases),
            "case_file": Path(a.cases).name, "fingerprint": code_fingerprint()["sha256"],
            "prompt_version": prompts.version}
    write_json_atomic(out_dir / "metrics.json", {"meta": meta, "metrics": all_metrics})
    write_report(out_dir, meta, all_metrics, all_issues, modes)
    write_scoring_sheet(out_dir, cases, modes)
    print(f"报告：{out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
