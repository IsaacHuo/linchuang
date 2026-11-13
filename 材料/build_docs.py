"""生成校赛支撑材料：项目说明.docx、声明文件.docx。

跑完真实模型实验后重新执行本脚本，会自动把最新 experiments/*/metrics.json 填进实验结果表：
  python build_docs.py
"""
import json
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent


def setup(doc):
    for name in ("Normal", "Heading 1", "Heading 2", "Title"):
        st = doc.styles[name]
        st.font.name = "Times New Roman"
        st.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体" if name == "Normal" else "黑体")
    doc.styles["Normal"].font.size = Pt(11)
    for s in doc.sections:
        s.left_margin = s.right_margin = Pt(72)


def table(doc, header, rows, widths=None):
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    for c, h in zip(t.rows[0].cells, header):
        c.text = h
        for r in c.paragraphs[0].runs:
            r.bold = True
    for row in rows:
        cells = t.add_row().cells
        for c, v in zip(cells, row):
            c.text = str(v)
    for row in t.rows:
        for c in row.cells:
            for p in c.paragraphs:
                for r in p.runs:
                    r.font.size = Pt(9.5)
    doc.add_paragraph()
    return t


def bullets(doc, items):
    for it in items:
        doc.add_paragraph(it, style="List Bullet")


def latest_metrics():
    files = sorted((PROJ / "experiments").glob("*/metrics.json"))
    files = [f for f in files if "mock" not in f.parent.name]
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def build_description():
    doc = Document()
    setup(doc)
    doc.add_heading("“临床智录”住院首次病程记录智能生成 —— 项目说明", level=0)
    doc.add_paragraph("队伍名称：________　　队伍编号：________　　指导教师：________")

    doc.add_heading("一、任务与目标", level=1)
    doc.add_paragraph(
        "本项目读取统一格式的入院资料（主诉、现病史、既往史、体格检查、辅助检查及入院初步判断，均限于资料截止点），"
        "调用主办方指定的 Qwen 大模型，生成包含“病例特点、拟诊讨论、诊疗计划”三段正文的首次病程记录草稿，"
        "并给出可溯源到 source_id 的依据说明和需要医生补充确认的待核事项。设计目标按赛题考察点排序："
        "①事实忠实，不虚构、不把“未提供”写成“正常”、不把计划写成已实施；②拟诊有依据、鉴别有针对性、正确处理信息不足；"
        "③工程可复现，可批量运行、配置规范、日志完整。产出仅用于教学比赛，不用于真实诊疗。")

    doc.add_heading("二、总体流程", level=1)
    doc.add_paragraph("系统采用“提取 → 整理 → 生成 → 校验 → 修复”的流水线，每例最多 3 次有效模型调用：")
    table(doc, ["步骤", "实现", "解决的问题"], [
        ["1 事实抽取", "模型调用 1：把资料归类为阳性、阴性、时序、缺失、冲突、初步判断、待解决问题", "防止遗漏关键事实，显式暴露缺失与冲突"],
        ["2 规则整理", "正则抽取带单位数值原文和“未提供/不详”条目，作为硬约束写入生成提示", "数值抄错、缺失项被补写为正常"],
        ["3 首程生成", "模型调用 2：原文 + 抽取结果 + 规则提示 → 五字段 JSON", "结构完整、重点清楚、个体化"],
        ["4 确定性修正", "去掉不存在的 source_id、evidence 截断为 5 条、规范 kind、补全缺失项待核", "格式不合规导致整例 0 分"],
        ["5 规则校验", "Schema、source_id、数值回溯、时序回溯、已实施表述、过度确诊、缺失写成正常", "把可判定的错误在提交前拦截"],
        ["6 定向修复", "模型调用 3（仅在有错误时）：逐条反馈问题，择优保留", "自动纠正，且修复变差时不采用"],
    ])

    doc.add_heading("三、提示词设计", level=1)
    doc.add_paragraph("共享书写规则（prompts/common_rules.txt）逐条对应评分细则，生成与修复两阶段共用，保证口径一致：")
    table(doc, ["评分维度", "对应的提示词约束"], [
        ["事实准确与忠实性", "只用截止点前资料；数值、单位、时间与原文一字不差；“未见/未提供/不详”原样保留"],
        ["拟诊讨论与临床分析", "初步判断括注依据和编号；围绕本例列 2—3 个鉴别方向，说明支持点和缺失证据；资料不足写“病因待查”"],
        ["诊疗计划针对性", "按“补充核实 → 拟完善检查及目的 → 处理原则 → 观察复查”组织；只用“拟/建议/复查”，禁止“已予/已完成”"],
        ["结构规范与个体化", "病例特点按“重要阳性 → 有价值阴性 → 病程 → 关键缺失”排列；禁止教科书式清单"],
        ["来源说明与待核提示", "evidence 3—5 条，source_id 真实存在，区分 fact / inference；缺失、冲突列入 review_items"],
    ])
    doc.add_paragraph("提示词版本记录在 prompts/manifest.json，各文件 SHA-256 写入每次运行的 run_log.json，保证提交结果与提示词对应。")

    doc.add_heading("四、生成后校验规则", level=1)
    table(doc, ["规则代码", "检查内容", "处理"], [
        ["SECTION_EMPTY / SCHEMA", "三段正文非空、字段类型正确", "修复；仍失败则记 failed"],
        ["SOURCE_ID_INVALID", "evidence 引用的编号必须属于当前病例", "确定性移除"],
        ["NUMBER_NOT_IN_SOURCE", "输出中带单位的数值必须出现在资料中", "反馈修复"],
        ["TIME_NOT_IN_SOURCE", "病例特点中的病程时间必须来自资料", "反馈修复"],
        ["PLAN_AS_DONE", "诊疗计划中出现“已予/已行/已完成”等（院外治疗除外）", "反馈修复"],
        ["OVERCONFIDENT_DX", "“确诊为/诊断明确”等，否定语境除外", "反馈修复"],
        ["MISSING_AS_NORMAL", "资料写“未提供”的项目被写成“正常/阴性/否认”", "反馈修复"],
    ])

    doc.add_heading("五、工程实现与可复现性", level=1)
    bullets(doc, [
        "统一入口 python main.py，无参数、无交互；仅使用 Python 标准库，requires.txt 无第三方依赖，降低封闭环境安装失败风险。",
        ".env 仅含 MODEL_NAME 与 ENDPOINT，不硬编码模型；不发送 Authorization 头，不读取系统代理。",
        "output.json 先写临时文件再整体替换；输入为空写 []；退出码 0/1/2 严格按技术规范。",
        "单例失败（资料无效、超时、无法解析）记 failed 并继续，不影响其他病例。",
        "固定 temperature=0、top_p=1、seed=42；开发试跑关闭推理。日志记录源码与提示词指纹、输入输出 SHA-256、调用时间戳、结束原因和 token 用量，打包前逐例核对。",
        "自动测试覆盖内容校验、修复后仍有明确事实错误、空输入、配置缺失、重复 case_id、单例无效、模型不可达和响应截断等情形。",
    ])

    doc.add_heading("六、改进验证", level=1)
    doc.add_paragraph("在同一批自编教学病例、同一模型和参数下对比基线与改进版，并用同一套规则统计自动指标。"
                      "临床五维评分待医学人员独立评阅。")
    table(doc, ["版本", "内容"], [
        ["baseline", "单次调用，系统提示取自赛题技术规范示例"],
        ["extract", "+ 事实抽取 + 数值/缺失项规则提示 + 确定性修正"],
        ["improved", "+ 规则校验 + 定向修复（提交版本）"],
    ])
    m = latest_metrics()
    if m:
        mm = m["metrics"]
        modes = list(mm)
        doc.add_paragraph(f"开发验证通过 OpenRouter 调用 {m['meta']['model']}，处理 {m['meta']['cases']} 例，"
                          f"实验快照指纹 {m['meta']['fingerprint'][:16]}。正式评测在赛方环境单独复现。")
        rows = [["成功例数"] + [f"{mm[x]['success']}/{mm[x]['cases']}" for x in modes],
                ["无规则错误例数"] + [mm[x]["success_without_rule_errors"] for x in modes]]
        for k in mm[modes[0]]["issue_counts"]:
            rows.append([f"问题数 {k}"] + [mm[x]["issue_counts"][k] for x in modes])
        rows += [["evidence 3—5 条占比"] + [mm[x]["evidence_3_5_rate"] for x in modes],
                 ["平均调用次数/例"] + [mm[x]["avg_calls_per_case"] for x in modes],
                 ["平均耗时 s/例"] + [mm[x]["avg_seconds_per_case"] for x in modes]]
        table(doc, ["指标"] + modes, rows)
    else:
        p = doc.add_paragraph("【待填写】接入 Qwen 模型后运行 python tools/run_experiment.py，再重新执行 build_docs.py，结果表会自动填入此处。")
        p.runs[0].bold = True
    doc.add_paragraph("自动指标反映生成成功率和可由规则识别的错误。五维临床评分尚未完成。")

    doc.add_heading("七、局限与后续", level=1)
    bullets(doc, [
        "规则校验只能拦截可模式化的错误，鉴别诊断是否合理仍依赖提示词与人工评审。",
        "数值回溯按“数值存在于资料中”判断，同一数值被挂到错误的检查项上时无法识别。",
        "改进版通常每例调用 2 次，规则发现错误时可追加 1 次修复，传输重试另计且受病例调用上限约束。",
        "未使用检索增强：赛题要求检索资料须由赛方提供或登记，后续获得许可资料后可加入鉴别诊断检索。",
        "本轮使用团队自编教学病例，医学要点需指导教师审定。官方练习病例发布后将在允许的环境重做对比。",
    ])

    doc.add_heading("八、团队分工", level=1)
    table(doc, ["成员", "负责内容"], [["", "流水线与工程实现"], ["", "提示词设计与迭代"], ["", "练习病例编写与医学审校"], ["", "实验评测与文档"]])
    out = HERE / "项目说明.docx"
    doc.save(out)
    return out


def build_declarations():
    doc = Document()
    setup(doc)
    doc.add_heading("声明文件", level=0)
    doc.add_paragraph("队伍名称：________　　队伍编号：________")

    doc.add_heading("一、所用模型及版本声明", level=1)
    table(doc, ["项目", "内容"], [
        ["正式评测模型", "Qwen3.8 27B，赛方 MODEL_NAME 与完整地址由赛方 .env 提供"],
        ["开发调试模型", "OpenRouter qwen/qwen3.8-27b，仅使用团队自编病例"],
        ["开发验证参数", "temperature=0，top_p=1，seed=42，max_tokens=3000，关闭推理"],
        ["调用方式", "正式评测使用赛方无密钥 Chat Completions 接口；开发验证经本机代理鉴权"],
        ["提示词版本", "v2.2（prompts/manifest.json）"],
    ])
    doc.add_heading("二、检索资料来源与版本声明", level=1)
    doc.add_paragraph("本作品未使用检索增强（RAG），未接入任何外部知识库或检索服务。")
    doc.add_heading("三、大模型使用声明", level=1)
    bullets(doc, [
        "算法运行阶段仅调用赛方公布的指定模型，不调用其他模型或外部服务。",
        "output.json / results.json 均由程序自动生成，未人工修改正文；run_log.json 为程序运行时自动记录，未作任何改动。",
        "开发过程中使用 AI 编程助手辅助编写代码、文档和部分合成教学病例，提交版本由团队确认。",
        "模型对照实验使用团队自编教学病例，赛题附件示例仅用于本机模拟验证。",
    ])
    doc.add_heading("四、数据使用承诺书", level=1)
    doc.add_paragraph("本队全体成员承诺：比赛病例资料仅在参赛期间用于本赛题，不对外传播，不用于其他用途；"
                      "生成结果仅供教学比赛评审，不作为真实诊疗依据；遵守模型使用限制，不对评测环境进行攻击、压测或越权访问，"
                      "不伪造运行记录或篡改日志。如有违反，自愿按赛事规则处理。")
    for label in ("队员签名：", "指导教师签名：", "日期："):
        p = doc.add_paragraph(label + "________________________")
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    out = HERE / "声明文件.docx"
    doc.save(out)
    return out


if __name__ == "__main__":
    for path in (build_description(), build_declarations()):
        d = Document(path)
        print(path.name, "paragraphs", len(d.paragraphs), "tables", len(d.tables))
