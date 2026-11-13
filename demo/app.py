r"""临床智录 —— 答辩演示网页（Gradio）。

不属于提交包（tools/pack.py 不打包 demo/），正式评测仍用 python main.py。

  demo\.venv\Scripts\python demo\app.py     # 浏览器打开 http://127.0.0.1:7860

只监听本机 127.0.0.1，不做登录、不开 share 公网链接、关闭 Gradio 匿名统计。
"""
import html
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
# no_proxy 里的 IPv6 条目（::1、[::1]）会让 httpx 解析失败，import gradio 直接报错。
# 仅在本进程内去掉这些条目，并确保本机地址不走代理；不修改系统设置。
for _k in ("no_proxy", "NO_PROXY"):
    _items = [x.strip() for x in os.environ.get(_k, "").split(",") if x.strip() and ":" not in x]
    os.environ[_k] = ",".join(dict.fromkeys(_items + ["127.0.0.1", "localhost"]))
import gradio as gr  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import config  # noqa: E402
from src.case_io import check_case  # noqa: E402
from src.envfile import ConfigError, load_model_config  # noqa: E402
from src.llm_client import LLMClient  # noqa: E402
from src.pipeline import MODES, Pipeline  # noqa: E402
from src.prompts import PromptSet  # noqa: E402
from src.validate import audit_record  # noqa: E402

PROMPTS = PromptSet(config.PROMPT_DIR)
MODE_LABELS = {"baseline": "baseline 单次调用", "extract": "extract 抽取+规则提示",
               "improved": "improved 校验+修复（提交版）"}
SECTIONS = [("case_features", "病例特点"), ("diagnostic_discussion", "拟诊讨论"), ("plan", "诊疗计划")]
KIND_ZH = {"fact": "事实", "inference": "推断"}


def load_samples():
    cases, labels = {}, {}
    for name in ("practice_cases", "test_cases_500"):
        f = ROOT / "data" / f"{name}.json"
        if f.exists():
            for c in json.loads(f.read_text(encoding="utf-8")):
                cases[c["case_id"]] = c
        mf = ROOT / "data" / f"{name}_meta.json"
        if mf.exists():
            for m in json.loads(mf.read_text(encoding="utf-8")).get("cases", []):
                labels[m["case_id"]] = m.get("focus", "")
    return cases, labels


SAMPLES, LABELS = load_samples()
CHOICES = [(f"{cid}  {LABELS.get(cid, '')}".strip(), cid) for cid in SAMPLES]


def default_model():
    try:
        return load_model_config(config.ENV_FILE)
    except ConfigError:
        return "", ""


# ---------------------------------------------------------------- 病例 <-> 文本
def case_to_text(case):
    return "\n".join(f"{s['source_id']}｜{s['content']}" for s in case["sources"])


LINE_RE = re.compile(r"^\s*(S\d+)\s*[｜|:：]\s*(.*)$")


def text_to_case(text, case_id):
    sources = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = LINE_RE.match(line)
        if m:
            sid, content = m.group(1), m.group(2).strip()
        else:
            sid, content = f"S{len(sources) + 1:02d}", line.strip()
        sources.append({"source_id": sid, "content": content})
    case = {"case_id": (case_id or "DEMO").strip() or "DEMO", "record_cutoff": "T0", "sources": sources}
    bad = check_case(case)
    if bad:
        raise gr.Error(f"病例资料无效：{bad}")
    return case


# ---------------------------------------------------------------- 渲染
def sources_html(case, record=None):
    refs = {}
    for i, ev in enumerate((record or {}).get("evidence") or [], 1):
        for sid in ev.get("source_ids") or []:
            refs.setdefault(sid, []).append(f"依据{i}（{KIND_ZH.get(ev.get('kind'), ev.get('kind'))}）")
    rows = []
    for s in case["sources"]:
        sid = s["source_id"]
        cited = refs.get(sid)
        badge = (f'<span class="lz-badge">被引用：{html.escape("、".join(cited))}</span>' if cited
                 else '<span class="lz-badge lz-muted">未被引用</span>' if record else "")
        rows.append(f'<div class="lz-src{" lz-cited" if cited else ""}"><b>{html.escape(sid)}</b> '
                    f'{html.escape(s["content"])}<br>{badge}</div>')
    return '<div class="lz-srcs">' + "".join(rows) + "</div>"


def record_md(result):
    if result["status"] != "success":
        err = result.get("error") or {}
        return f"**生成失败**：{err.get('code')} —— {err.get('message')}"
    rec = result["medical_record"]
    parts = [f"### {zh}\n\n{rec.get(key) or '（空）'}" for key, zh in SECTIONS]
    items = rec.get("review_items") or []
    parts.append("### 待核事项\n\n" + ("\n".join(f"- {x}" for x in items) if items else "（无）"))
    return "\n\n".join(parts)


def evidence_rows(result):
    rec = result.get("medical_record") or {}
    return [[i, ev.get("statement", ""), "、".join(ev.get("source_ids") or []),
             KIND_ZH.get(ev.get("kind"), ev.get("kind"))] for i, ev in enumerate(rec.get("evidence") or [], 1)]


def issue_rows(result, case):
    if result["status"] != "success":
        return []
    return [[i["severity"], i["code"], i["field"], i["detail"]] for i in audit_record(result["medical_record"], case)]


def stats_md(result, log, issues):
    errors = sum(1 for i in issues if i[0] == "error")
    return (f"结果病例 **{html.escape(result['case_id'])}** ｜ 模式 **{log['mode']}** ｜ 状态 **{result['status']}** ｜ "
            f"模型调用 {log['model_calls']} 次 ｜ "
            f"耗时 {log['duration_s']} s ｜ 规则错误 {errors} 条、提示 {len(issues) - errors} 条")


# ---------------------------------------------------------------- 运行
def run_pipeline(case, mode, model, endpoint):
    if not (model or "").strip() or not (endpoint or "").strip():
        raise gr.Error("请填写模型名和接口地址（或在 .env 中配置）")
    client = LLMClient(model.strip(), endpoint.strip(), config.GEN_PARAMS, config.REQUEST_TIMEOUT_S,
                       config.MAX_RETRIES, config.RETRY_BACKOFF_S)
    return Pipeline(client, PROMPTS, mode).process(case)


def on_pick(cid):
    case = SAMPLES.get(cid)
    if not case:
        return gr.update(), gr.update(), gr.update()
    return cid, case_to_text(case), sources_html(case)


def clear_results():
    generated = ("", [], [], "", "")
    compared = ("", "", [], "", "", [])
    return (*generated, *compared)


def on_edit(cid, text):
    try:
        case = text_to_case(text, cid)
    except gr.Error:
        return "", *clear_results()
    return sources_html(case), *clear_results()


def on_generate(cid, text, mode, model, endpoint):
    case = text_to_case(text, cid)
    result, log = run_pipeline(case, mode, model, endpoint)
    issues = issue_rows(result, case)
    return (sources_html(case, result.get("medical_record")), record_md(result), evidence_rows(result),
            issues, stats_md(result, log, issues),
            json.dumps(result, ensure_ascii=False, indent=2))


def on_compare(cid, text, model, endpoint):
    case = text_to_case(text, cid)
    out = []
    for mode in ("baseline", "improved"):
        result, log = run_pipeline(case, mode, model, endpoint)
        issues = issue_rows(result, case)
        out += [stats_md(result, log, issues), record_md(result), issues]
    return out


CSS = """
.lz-srcs{display:flex;flex-direction:column;gap:6px}
.lz-src{border:1px solid #c8c8c8;border-left:4px solid #c8c8c8;border-radius:6px;padding:6px 10px;line-height:1.6}
.lz-cited{border-left-color:#1f6feb}
.lz-badge{font-size:12px;color:#1f4fa8}
.lz-muted{color:#6a6a6a}
"""
ISSUE_HEADERS = ["级别", "规则", "字段", "说明"]


def build_ui():
    model0, endpoint0 = default_model()
    first = CHOICES[0][1] if CHOICES else ""
    with gr.Blocks(title="临床智录 演示", css=CSS) as ui:
        gr.Markdown("# 临床智录 —— 住院首次病程记录生成演示\n"
                    "左侧为入院资料（每行一条，格式「S01｜内容」，可直接修改或粘贴），右侧为生成的首程草稿。"
                    "产出仅用于教学比赛，不用于真实诊疗。")
        with gr.Accordion("模型设置", open=not (model0 and endpoint0)):
            with gr.Row():
                model = gr.Textbox(label="MODEL_NAME", value=model0)
                endpoint = gr.Textbox(label="ENDPOINT（完整请求地址）", value=endpoint0, scale=2)
        with gr.Row():
            pick = gr.Dropdown(CHOICES, value=first, label="示例病例", scale=3)
            cid = gr.Textbox(label="case_id", value=first, scale=1)
        with gr.Tabs():
            with gr.Tab("生成演示"):
                with gr.Row():
                    with gr.Column(scale=1):
                        text = gr.Textbox(label="入院资料", lines=12,
                                          value=case_to_text(SAMPLES[first]) if first else "")
                        mode = gr.Radio([(MODE_LABELS[m], m) for m in MODES], value="improved", label="流水线模式")
                        go = gr.Button("生成首次病程记录", variant="primary")
                        src_view = gr.HTML(sources_html(SAMPLES[first]) if first else "", label="资料与引用")
                    with gr.Column(scale=1):
                        stats = gr.Markdown()
                        rec = gr.Markdown()
                        ev = gr.Dataframe(headers=["#", "依据", "来源", "类型"], label="可溯源依据 evidence",
                                          wrap=True, interactive=False)
                        iss = gr.Dataframe(headers=ISSUE_HEADERS, label="规则校验结果（对最终输出复检）",
                                           wrap=True, interactive=False)
                        with gr.Accordion("原始 JSON 输出", open=False):
                            raw = gr.Code(language="json")
                generation = go.click(on_generate, [cid, text, mode, model, endpoint],
                                      [src_view, rec, ev, iss, stats, raw], concurrency_id="case")
            with gr.Tab("基线 vs 改进 对比"):
                gr.Markdown("同一病例、同一模型分别运行 baseline 与 improved，用同一套规则复检。")
                cmp_go = gr.Button("运行对比", variant="primary")
                with gr.Row():
                    cols = []
                    for mode_name in ("baseline", "improved"):
                        with gr.Column():
                            gr.Markdown(f"## {MODE_LABELS[mode_name]}")
                            cols += [gr.Markdown(), gr.Markdown(),
                                     gr.Dataframe(headers=ISSUE_HEADERS, label="规则校验", wrap=True, interactive=False)]
                comparison = cmp_go.click(on_compare, [cid, text, model, endpoint], cols, concurrency_id="case")
        result_components = [rec, ev, iss, stats, raw, *cols]
        pending = [generation, comparison]
        pick.change(lambda value: (*on_pick(value), *clear_results()), pick,
                    [cid, text, src_view, *result_components], concurrency_id="case", cancels=pending)
        for field in (cid, text):
            field.input(on_edit, inputs=[cid, text], outputs=[src_view, *result_components],
                        concurrency_id="case", cancels=pending)
    return ui


if __name__ == "__main__":
    port = int(os.environ.get("DEMO_PORT", "7860"))
    build_ui().queue(default_concurrency_limit=1).launch(server_name="127.0.0.1", server_port=port, share=False)
