"""新增组件单元测试：输入适配、降级阶梯、新校验规则、保底记录、检索与对齐、子预算。"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.adapter import AdapterError, lenient_loads, record_to_sources, to_canonical  # noqa: E402
from src.fallback import build_fallback  # noqa: E402
from src.llm_client import CallBudget, ModelCallError, SubBudget  # noqa: E402
from src.pipeline import MODES, must_not_miss_coverage, plan_steps  # noqa: E402
from src.retrieval import BM25, Aligner, Retriever, format_cards, load_cards  # noqa: E402
from src.validate import audit_record  # noqa: E402

OFFICIAL = ROOT / "data" / "official_practice_cases.json"
PRACTICE = json.loads((ROOT / "data" / "practice_cases.json").read_text(encoding="utf-8"))

RECORD = {"入院记录": {
    "一般项目": {"姓名": "张××", "性别": "男", "年龄": "60岁", "现住址": "某地", "记录时间": "2026-01-01 10:00"},
    "主诉": "胸痛2小时。",
    "现病史": "患者2小时前出现胸痛。补充病史：既往服用阿司匹林。",
    "婚育史": "",
    "体格检查": {"生命体征": {"体温": "36.5℃", "血压": ""}, "心脏": "心律齐", "专科情况": "无"},
    "辅助检查": {"检查时间": "2026-01-01", "心电图": "窦性心律", "备注": "待完善肌钙蛋白"},
    "初步诊断": ["胸痛待查"],
}}


def good_record(case):
    ids = [s["source_id"] for s in case["sources"]]
    return {"case_features": "资料如上。" * 40, "diagnostic_discussion": "初步判断尚需进一步检查明确。" * 10,
            "plan": "拟完善相关检查，监测生命体征。" * 10,
            "evidence": [{"statement": case["sources"][i]["content"][:20], "source_ids": [ids[i]], "kind": "fact"}
                         for i in range(min(2, len(ids)))]
                        + [{"statement": "推断", "source_ids": [ids[0]], "kind": "inference"}],
            "review_items": ["核实"]}


class AdapterTest(unittest.TestCase):
    def test_lenient(self):
        self.assertEqual(lenient_loads('[{"a":1}')[0], [{"a": 1}])
        self.assertEqual(lenient_loads('{"a":1}]')[0], {"a": 1})
        self.assertEqual(lenient_loads('{"a":1}{"b":2}')[0], [{"a": 1}, {"b": 2}])
        self.assertIsNone(lenient_loads("[]")[1])
        with self.assertRaises(json.JSONDecodeError):
            lenient_loads("{broken")

    def test_record_to_sources(self):
        sources, smap, cutoff = record_to_sources(RECORD["入院记录"])
        text = "\n".join(s["content"] for s in sources)
        self.assertEqual(cutoff, "2026-01-01 10:00")
        self.assertNotIn("张××", text)
        self.assertNotIn("某地", text)
        self.assertNotIn("婚育史", text)
        self.assertIn("补充病史：既往服用阿司匹林。", [s["content"] for s in sources])
        self.assertIn("血压：未提供", text)
        self.assertIn("辅助检查（2026-01-01）心电图：窦性心律", text)
        self.assertTrue(sources[-1]["content"].startswith("入院初步诊断（初步判断"))
        self.assertEqual([s["source_id"] for s in sources], [f"S{i:02d}" for i in range(1, len(sources) + 1)])
        self.assertEqual(set(smap), {s["source_id"] for s in sources})

    def test_shapes(self):
        for data in (RECORD, [RECORD], [[RECORD]], {"cases": [RECORD]}, RECORD["入院记录"]):
            cases, _ = to_canonical(data)
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0]["case_id"], "CASE001")
            self.assertTrue(cases[0]["sources"])
        cases, notes = to_canonical(PRACTICE)
        self.assertEqual([c["case_id"] for c in cases], [c["case_id"] for c in PRACTICE])
        self.assertEqual(notes, [])
        self.assertEqual(to_canonical([]), ([], []))
        with self.assertRaises(AdapterError):
            to_canonical(42)


class PipelineLogicTest(unittest.TestCase):
    def test_ladder(self):
        f = MODES["improved"]
        self.assertEqual(plan_steps(6, f), {"extract": True, "repair": True})
        self.assertEqual(plan_steps(4, f), {"extract": True, "repair": True})
        self.assertEqual(plan_steps(3, f), {"extract": True, "repair": False})
        self.assertEqual(plan_steps(2, f), {"extract": False, "repair": True})
        self.assertEqual(plan_steps(1, f), {"extract": False, "repair": False})
        self.assertEqual(plan_steps(6, MODES["baseline"]), {"extract": False, "repair": False})

    def test_sub_budget(self):
        parent = CallBudget(3)
        sub = SubBudget(parent, 1)
        sub.consume()
        self.assertEqual(sub.remaining, 0)
        with self.assertRaises(ModelCallError):
            sub.consume()
        self.assertEqual(parent.remaining, 2)

    def test_coverage(self):
        ext = {"problem_list": [{"problem": "惊厥", "must_not_miss": ["中枢神经系统感染", "低血糖"]}]}
        cov = must_not_miss_coverage(ext, {"diagnostic_discussion": "需排除中枢神经系统感染"})
        self.assertEqual((cov["total"], cov["covered"], cov["missed"]), (2, 1, ["低血糖"]))
        self.assertIsNone(must_not_miss_coverage(None, {}))


class RuleTest(unittest.TestCase):
    def setUp(self):
        self.case = PRACTICE[0]

    def codes(self, rec):
        return {i["code"]: i["severity"] for i in audit_record(rec, self.case)}

    def test_good_record_clean(self):
        errs = [c for c, s in self.codes(good_record(self.case)).items() if s == "error"]
        self.assertEqual(errs, [])

    def test_dose_flagged(self):
        rec = good_record(self.case)
        rec["plan"] += "拟予某药 5mg 口服。"
        self.assertEqual(self.codes(rec).get("PLAN_DOSE_NOT_IN_SOURCE"), "error")

    def test_evidence_rules_are_errors(self):
        rec = good_record(self.case)
        rec["evidence"] = rec["evidence"][:1]
        self.assertEqual(self.codes(rec).get("EVIDENCE_COUNT"), "error")
        rec = good_record(self.case)
        for e in rec["evidence"]:
            e["kind"] = "fact"
        self.assertEqual(self.codes(rec).get("NO_INFERENCE"), "error")


class FallbackTest(unittest.TestCase):
    def _check(self, cases):
        for case in cases:
            rec = build_fallback(case)
            errs = [i for i in audit_record(rec, case) if i["severity"] == "error"]
            self.assertEqual(errs, [], case["case_id"])
            self.assertTrue(3 <= len(rec["evidence"]) <= 5 or len(case["sources"]) < 3)
            self.assertTrue(rec["review_items"])

    def test_practice(self):
        self._check(PRACTICE)

    @unittest.skipUnless(OFFICIAL.is_file(), "未生成官方练习集转换文件")
    def test_official(self):
        self._check(json.loads(OFFICIAL.read_text(encoding="utf-8")))


CARDS = [
    {"id": "seizure-infant", "topic": "婴幼儿惊厥", "triggers": ["抽搐", "惊厥"],
     "differentials": [{"name": "中枢神经系统感染", "support": "发热", "against": "", "must_check": "脑脊液"}],
     "red_flags": ["意识障碍"], "plan_hints": ["监测"]},
    {"id": "chest-pain", "topic": "急性胸痛", "triggers": ["胸痛", "胸闷"],
     "differentials": [{"name": "急性冠脉综合征"}, {"name": "主动脉夹层"}]},
    {"id": "edema", "topic": "水肿查因", "triggers": ["水肿", "白蛋白"], "differentials": [{"name": "肾病综合征"}]},
]


class RetrievalTest(unittest.TestCase):
    def test_bm25_fallback_search(self):
        r = Retriever(CARDS, None).prepare()
        self.assertEqual(r.method, "bm25")
        hits = r.search("主诉：呕吐腹泻3天，抽搐1次", 2)
        self.assertEqual(hits[0]["card"]["id"], "seizure-infant")
        self.assertEqual(r.search("", 3), [])
        block = format_cards(hits)
        self.assertIn("中枢神经系统感染", block)

    def test_bm25_scores(self):
        s = BM25(["急性胸痛", "下肢水肿"]).scores("胸痛")
        self.assertGreater(s[0], s[1])

    def test_aligner_lexical(self):
        case = {"sources": [{"source_id": "S01", "content": "主诉：胸痛2小时"}]}
        rec = {"evidence": [{"statement": "胸痛2小时", "source_ids": ["S01"], "kind": "fact"},
                            {"statement": "完全无关的内容描述", "source_ids": ["S01"], "kind": "fact"},
                            {"statement": "推断", "source_ids": ["S01"], "kind": "inference"}]}
        out = Aligner().check(rec, case)
        self.assertEqual([o["field"] for o in out], ["evidence[1]"])
        self.assertTrue(all(o["severity"] == "warn" for o in out))

    def test_load_cards_missing(self):
        self.assertEqual(load_cards(ROOT / "no_such.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
