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
from src.case_io import read_cases, write_json_atomic  # noqa: E402
from src.envfile import load_runtime_config  # noqa: E402
from src.llm_client import CallBudget, LLMClient, ModelCallError  # noqa: E402
from src.parsing import ParseError, parse_json_object  # noqa: E402
from src.pipeline import MODES, Pipeline  # noqa: E402
from src.prompts import PromptSet, case_block  # noqa: E402
from src.retrieval import Aligner, Embedder, Retriever, load_cards  # noqa: E402
from src.runner import code_fingerprint, run_cases  # noqa: E402
from src.validate import TEXT_FIELDS, audit_record  # noqa: E402

MODE_DESC = {
    "baseline": "单次调用，赛题示例系统提示",
    "extract": "+事实与问题清单抽取 +数值/缺失项规则提示 +结构修正",
    "improved": "+生成后规则校验 +定向修复（正式提交版本）",
    "improved+rag": "+团队自编鉴别卡片检索（BGE-M3，退回 BM25）",
}
TRACKED = ["NUMBER_NOT_IN_SOURCE", "TIME_NOT_IN_SOURCE", "PLAN_AS_DONE", "OVERCONFIDENT_DX",
           "MISSING_AS_NORMAL", "SOURCE_ID_INVALID", "EVIDENCE_COUNT", "NO_INFERENCE",
           "PLAN_DOSE_NOT_IN_SOURCE", "UNIT_MISMATCH", "EVIDENCE_ALIGN"]
JUDGE_DIMS = ["事实准确与忠实性", "拟诊讨论与临床分析", "诊疗计划针对性", "结构规范与个体化", "来源说明与待核提示"]
JUDGE_SYSTEM = ("你是住院病历书写的教学评审。根据病例资料评价一份首次病程记录草稿，按以下五个维度各打 1—5 分"
                "（5 最好），并判断是否存在可能造成伤害的建议或编造关键事实。只返回 JSON："
                '{"scores": {"' + '": 0, "'.join(JUDGE_DIMS) + '": 0}, "serious_error": false, "comment": "一句话理由"}')


def metrics_for(cases, results, logs, aligner=None):
    n = len(cases)
    ok = [(c, r) for c, r in zip(cases, results) if r["status"] == "success"]
    issue_counter, clean = Counter(), 0
    ev_ok = inf_ok = rv_ok = 0
    lengths, per_case = [], {}
    aligner = aligner or Aligner()
    for case, res in ok:
        rec = res["medical_record"]
        issues = audit_record(rec, case) + aligner.check(rec, case)
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
    cov = [lg["must_not_miss"] for lg in logs if lg.get("must_not_miss")]
    cov_total = sum(c["total"] for c in cov)
    return {
        "cases": n,
        "success": len(ok),
        "fallback": sum(bool(lg.get("fallback")) for lg in logs),
        "must_not_miss_coverage": round(sum(c["covered"] for c in cov) / cov_total, 3) if cov_total else None,
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
            ("保底记录例数", lambda m: str(all_metrics[m]["fallback"])),
            ("must_not_miss 覆盖率", lambda m: str(all_metrics[m]["must_not_miss_coverage"])),
            ]
    rows += [(f"问题数：{k}", (lambda k: lambda m: str(all_metrics[m]["issue_counts"][k]))(k)) for k in TRACKED]
    rows += [("evidence 3—5 条占比", lambda m: str(all_metrics[m]["evidence_3_5_rate"])),
             ("含 inference 占比", lambda m: str(all_metrics[m]["has_inference_rate"])),
             ("待核事项非空占比", lambda m: str(all_metrics[m]["review_items_nonempty_rate"])),
             ("三段平均字数", lambda m: str(all_metrics[m]["avg_text_chars"])),
             ("平均调用次数/例", lambda m: str(all_metrics[m]["avg_calls_per_case"])),
             ("平均耗时 s/例", lambda m: str(all_metrics[m]["avg_seconds_per_case"])),
             ("失败原因", lambda m: json.dumps(all_metrics[m]["failed_codes"], ensure_ascii=False))]
    if any("judge" in all_metrics[m] for m in modes):
        rows += [(f"模型评审：{d}", (lambda d: lambda m: str((all_metrics[m].get("judge") or {}).get(d, "-")))(d))
                 for d in JUDGE_DIMS + ["serious_error_rate"]]
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


def judge(client, cases, results):
    """可选：用同一模型按五维度打分，仅作人工评分的预筛参考，不能替代人工评分。"""
    rows, per = [], {}
    for case, res in zip(cases, results):
        if res["status"] != "success":
            continue
        user = case_block(case) + "\n\n【待评记录】\n" + json.dumps(res["medical_record"], ensure_ascii=False)
        try:
            text = client.chat([{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
                               "judge", CallBudget(3), [])
            obj = parse_json_object(text)
            rows.append(obj)
            per[case["case_id"]] = obj
        except (ModelCallError, ParseError):
            continue
    if not rows:
        return None, per
    agg = {}
    for d in JUDGE_DIMS:
        vals = [float(r.get("scores", {}).get(d)) for r in rows
                if isinstance(r.get("scores", {}).get(d), (int, float))]
        agg[d] = round(sum(vals) / len(vals), 2) if vals else None
    agg["serious_error_rate"] = round(sum(bool(r.get("serious_error")) for r in rows) / len(rows), 3)
    return agg, per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "data" / "official_practice_cases.json"))
    ap.add_argument("--modes", default="baseline,extract,improved,improved+rag")
    ap.add_argument("--fallback", action="store_true", help="实验中也启用保底记录（默认关闭，以如实统计模型失败）")
    ap.add_argument("--candidates", type=int, default=None)
    ap.add_argument("--judge", action="store_true", help="附加模型五维度评审（仅供预筛）")
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
        rt = {"model_name": a.model, "endpoint": a.endpoint, "api_key": "", "embed_endpoint": ""}
    else:
        rt = load_runtime_config(Path(a.env))
        rt["model_name"] = a.model or rt["model_name"]
        rt["endpoint"] = a.endpoint or rt["endpoint"]
    model, endpoint = rt["model_name"], rt["endpoint"]
    embedder = None
    if rt.get("embed_endpoint"):
        embedder = Embedder(rt["embed_endpoint"], rt.get("embed_model") or "bge-m3",
                            rt.get("embed_api_key") or rt.get("api_key"), config.EMBED_TIMEOUT_S)
    retriever = None
    if "improved+rag" in modes:
        cards = load_cards(config.KB_FILE)
        retriever = Retriever(cards, embedder).prepare() if cards else None
        print(f"知识卡片 {len(cards)} 条，检索方式 {retriever.method if retriever else '无'}")
    aligner = Aligner(embedder, config.EVIDENCE_ALIGN_THRESHOLD)
    cases, _ = read_cases(Path(a.cases))
    if a.limit > 0:
        cases = cases[:a.limit]
    prompts = PromptSet(config.PROMPT_DIR)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "experiments" / f"{stamp}_{a.tag}"
    out_dir.mkdir(parents=True, exist_ok=True)

    all_metrics, all_issues = {}, {}
    for m in modes:
        client = LLMClient(model, endpoint, config.GEN_PARAMS, config.REQUEST_TIMEOUT_S,
                           config.MAX_RETRIES, config.RETRY_BACKOFF_S, api_key=rt.get("api_key"))
        print(f"[{m}] 运行 {len(cases)} 例 ...", flush=True)
        pipe = Pipeline(client, prompts, m, retriever=retriever, embedder=embedder,
                        use_rag=(m == "improved+rag"), n_candidates=a.candidates, fallback=a.fallback)
        results, logs = run_cases(cases, pipe, config.CONCURRENCY)
        tag = m.replace("+", "_")
        write_json_atomic(out_dir / f"{tag}_output.json", results)
        write_json_atomic(out_dir / f"{tag}_case_logs.json", logs)
        all_metrics[m], all_issues[m] = metrics_for(cases, results, logs, aligner)
        if a.judge:
            all_metrics[m]["judge"], per = judge(client, cases, results)
            write_json_atomic(out_dir / f"{tag}_judge.json", per)
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
