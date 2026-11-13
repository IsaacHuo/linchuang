"""校验规则与解析的单元测试：python -m unittest discover -s tests -v"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.facts import find_measurements, find_missing_items  # noqa: E402
from src.parsing import ParseError, parse_json_object  # noqa: E402
from src.validate import audit_record, normalize_record  # noqa: E402

CASE = json.loads((ROOT / "data" / "practice_cases.json").read_text(encoding="utf-8"))[0]  # DEMO001


def good_record():
    return {
        "case_features": "成人，近 2 周乏力、活动后心悸，结膜苍白、双下肢轻度水肿。血红蛋白 82 g/L、肌酐 185 μmol/L。" * 3,
        "diagnostic_discussion": "现有资料支持贫血及肾功能异常，病因待查，需进一步检查明确。" * 4,
        "plan": "拟补充既往检查及用药史，建议复查血常规及肾功能，观察症状变化。" * 4,
        "evidence": [
            {"statement": "乏力 2 周", "source_ids": ["S01"], "kind": "fact"},
            {"statement": "结膜苍白", "source_ids": ["S02"], "kind": "fact"},
            {"statement": "急慢性过程未明", "source_ids": ["S03", "S04"], "kind": "inference"},
        ],
        "review_items": ["血压、尿量未提供"],
    }


def codes(rec):
    return {i["code"] for i in audit_record(rec, CASE) if i["severity"] == "error"}


class RuleTests(unittest.TestCase):
    def test_good_record_has_no_errors(self):
        self.assertEqual(codes(good_record()), set())

    def test_number_not_in_source(self):
        rec = good_record()
        rec["case_features"] = rec["case_features"].replace("82 g/L", "92 g/L")
        self.assertIn("NUMBER_NOT_IN_SOURCE", codes(rec))

    def test_time_not_in_source(self):
        rec = good_record()
        rec["case_features"] = rec["case_features"].replace("2 周", "3 周")
        self.assertIn("TIME_NOT_IN_SOURCE", codes(rec))

    def test_plan_written_as_done(self):
        rec = good_record()
        rec["plan"] = "已予补液治疗。" + rec["plan"]
        self.assertIn("PLAN_AS_DONE", codes(rec))

    def test_prior_treatment_in_plan_allowed(self):
        rec = good_record()
        rec["plan"] = "院外已予口服药物，效果不详；" + rec["plan"]
        self.assertNotIn("PLAN_AS_DONE", codes(rec))

    def test_overconfident_diagnosis(self):
        rec = good_record()
        rec["diagnostic_discussion"] = "肾性贫血诊断明确。" + rec["diagnostic_discussion"]
        self.assertIn("OVERCONFIDENT_DX", codes(rec))

    def test_negated_diagnosis_allowed(self):
        rec = good_record()
        rec["diagnostic_discussion"] = "目前尚不能确诊为肾性贫血。" + rec["diagnostic_discussion"]
        self.assertNotIn("OVERCONFIDENT_DX", codes(rec))

    def test_missing_rewritten_as_normal(self):
        rec = good_record()
        rec["case_features"] += "血压正常。"
        self.assertIn("MISSING_AS_NORMAL", codes(rec))

    def test_invalid_source_id(self):
        rec = good_record()
        rec["evidence"][0]["source_ids"] = ["S99"]
        self.assertIn("SOURCE_ID_INVALID", codes(rec))

    def test_normalize_fixes_structure(self):
        rec = good_record()
        rec["evidence"].append({"statement": "x", "source_ids": ["S99"], "kind": "fact"})
        rec["evidence"] += [{"statement": f"e{i}", "source_ids": "S01", "kind": "FACT"} for i in range(5)]
        fixed, fixes = normalize_record(rec, CASE)
        self.assertLessEqual(len(fixed["evidence"]), 5)
        self.assertTrue(all(set(e["source_ids"]) <= {"S01", "S02", "S03", "S04"} for e in fixed["evidence"]))
        self.assertTrue(fixes)
        self.assertNotIn("SOURCE_ID_INVALID", codes(fixed))

    def test_measurements_and_missing(self):
        vals = {(n, u) for n, u, _ in find_measurements("血压 156/92 mmHg，白细胞 13.6×10^9/L")}
        self.assertIn(("156", "mmHg"), vals)
        self.assertIn(("92", "mmHg"), vals)
        self.assertIn(("13.6", "×109/L"), vals)
        items = [i for i, _ in find_missing_items(CASE["sources"])]
        for want in ("血压", "尿量", "网织红细胞"):
            self.assertIn(want, items)


class ParseTests(unittest.TestCase):
    def test_fenced_and_prefixed(self):
        fence = "```"
        text = "好的，以下是结果：\n" + fence + "json\n{\"a\": 1}\n" + fence
        self.assertEqual(parse_json_object(text), {"a": 1})

    def test_think_tag(self):
        self.assertEqual(parse_json_object("<think>{\"x\":2}</think>{\"a\": \"}\"}"), {"a": "}"})

    def test_invalid(self):
        with self.assertRaises(ParseError):
            parse_json_object("没有 JSON")


if __name__ == "__main__":
    unittest.main()
