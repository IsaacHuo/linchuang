"""端到端测试：把源码复制到临时目录，用本地模拟模型按统一入口 python main.py 运行。"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import mock_server  # noqa: E402
from tools.check_output import check  # noqa: E402
from src.validate import audit_record  # noqa: E402

CASES = json.loads((ROOT / "data" / "practice_cases.json").read_text(encoding="utf-8"))


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.endpoint = mock_server.start(0, faulty=False)
        cls.fserver, cls.fendpoint = mock_server.start(0, faulty=True)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.fserver.shutdown()

    def run_main(self, cases, endpoint=None, env=True, raw_input=None):
        tmp = Path(tempfile.mkdtemp(prefix="lczl_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy(ROOT / "main.py", tmp)
        for d in ("src", "prompts"):
            shutil.copytree(ROOT / d, tmp / d, ignore=shutil.ignore_patterns("__pycache__"))
        if env:
            (tmp / ".env").write_text(f"MODEL_NAME=mock-qwen\nENDPOINT={endpoint or self.endpoint}\n", encoding="utf-8")
        text = raw_input if raw_input is not None else json.dumps(cases, ensure_ascii=False)
        (tmp / "input.json").write_text(text, encoding="utf-8")
        proc = subprocess.run([sys.executable, "main.py"], cwd=tmp, capture_output=True, timeout=300)
        out = tmp / "output.json"
        output = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
        log = json.loads((tmp / "run_log.json").read_text(encoding="utf-8"))
        leftovers = [p.name for p in tmp.glob("*.tmp")]
        self.assertEqual(leftovers, [])
        return proc.returncode, output, log

    def test_practice_cases(self):
        code, out, log = self.run_main(CASES)
        self.assertEqual(code, 0)
        errors, _ = check(CASES, out)
        self.assertEqual(errors, [])
        self.assertTrue(all(r["status"] == "success" for r in out))
        self.assertEqual(log["summary"]["total_cases"], len(CASES))
        self.assertNotIn("Authorization", json.dumps(log))

    def test_faulty_model_is_repaired(self):
        code, out, log = self.run_main(CASES, endpoint=self.fendpoint)
        self.assertEqual(code, 0)
        for case, res in zip(CASES, out):
            self.assertEqual(res["status"], "success", res)
            errs = [i for i in audit_record(res["medical_record"], case) if i["severity"] == "error"]
            self.assertEqual(errs, [], case["case_id"])
        self.assertTrue(all(c["repair_rounds"] == 1 for c in log["cases"]))

    def test_empty_input(self):
        code, out, _ = self.run_main([])
        self.assertEqual((code, out), (0, []))

    def test_missing_env(self):
        code, out, log = self.run_main(CASES[:1], env=False)
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        self.assertEqual(log["exit_code"], 2)

    def test_duplicate_case_id(self):
        code, out, _ = self.run_main([CASES[0], CASES[0]])
        self.assertEqual(code, 2)
        self.assertIsNone(out)

    def test_not_array(self):
        code, _, _ = self.run_main(None, raw_input=json.dumps(CASES[0]))
        self.assertEqual(code, 2)
        code, _, _ = self.run_main(None, raw_input="{broken")
        self.assertEqual(code, 2)

    def test_invalid_single_case_keeps_going(self):
        bad = {"case_id": "BAD1", "record_cutoff": "T0", "sources": []}
        code, out, _ = self.run_main([bad, CASES[0]])
        self.assertEqual(code, 0)
        self.assertEqual([r["case_id"] for r in out], ["BAD1", "DEMO001"])
        self.assertEqual(out[0]["status"], "failed")
        self.assertEqual(out[0]["error"]["code"], "INVALID_CASE")
        self.assertIsNone(out[0]["medical_record"])
        self.assertEqual(out[1]["status"], "success")

    def test_unreachable_model(self):
        code, out, log = self.run_main(CASES[:1], endpoint="http://127.0.0.1:9/v1/chat/completions")
        self.assertEqual(code, 0)
        self.assertEqual(out[0]["status"], "failed")
        self.assertEqual(out[0]["error"]["code"], "MODEL_UNAVAILABLE")
        self.assertGreaterEqual(log["summary"]["failed_model_calls"], 1)


if __name__ == "__main__":
    unittest.main()
