"""端到端测试：把源码复制到临时目录，用本地模拟模型运行统一入口。

覆盖新入口 python main.py --input X --output Y 与旧入口（无参数）两种方式。
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import mock_server  # noqa: E402
from tools.check_output import check  # noqa: E402
from src.validate import audit_record  # noqa: E402
from src.llm_client import CallBudget, LLMClient, ModelCallError  # noqa: E402

CASES = json.loads((ROOT / "data" / "practice_cases.json").read_text(encoding="utf-8"))
OFFICIAL_FILE = ROOT / "data" / "official_practice_cases.json"
OFFICIAL_RAW = ROOT / "data" / "official_practice" / "临床智录_练习病历数据集JSON"


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.endpoint = mock_server.start(0, faulty=False)
        cls.fserver, cls.fendpoint = mock_server.start(0, faulty=True)
        cls.kserver, cls.kendpoint = mock_server.start(0, api_key="test-key")
        cls.dserver, cls.dendpoint = mock_server.start(0, embed_down=True)

    @classmethod
    def tearDownClass(cls):
        for s in (cls.server, cls.fserver, cls.kserver, cls.dserver):
            s.shutdown()

    def run_main(self, cases, endpoint=None, env=True, raw_input=None, args=None, extra_env="",
                 new_entry=True):
        tmp = Path(tempfile.mkdtemp(prefix="lczl_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        app = tmp / "app"
        app.mkdir()
        shutil.copy(ROOT / "main.py", app)
        for d in ("src", "prompts", "resources"):
            if (ROOT / d).is_dir():
                shutil.copytree(ROOT / d, app / d, ignore=shutil.ignore_patterns("__pycache__"))
        if env:
            (app / ".env").write_text(f"MODEL_NAME=mock-qwen\nENDPOINT={endpoint or self.endpoint}\n{extra_env}",
                                      encoding="utf-8")
        text = raw_input if raw_input is not None else json.dumps(cases, ensure_ascii=False)
        if new_entry:  # 输入输出放在与代码不同的目录，验证路径不硬编码
            io = tmp / "io"
            io.mkdir()
            inp, out = io / "input.json", io / "output.json"
            cmd = [sys.executable, str(app / "main.py"), "--input", str(inp), "--output", str(out)]
            cwd = tmp
        else:
            inp, out = app / "input.json", app / "output.json"
            cmd = [sys.executable, "main.py"]
            cwd = app
        inp.write_text(text, encoding="utf-8")
        proc = subprocess.run(cmd + list(args or []), cwd=cwd, capture_output=True, timeout=300)
        output = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
        alias = out.parent / "results.json"
        self.last_alias = json.loads(alias.read_text(encoding="utf-8")) if alias.exists() else None
        log = json.loads((out.parent / "run_log.json").read_text(encoding="utf-8"))
        leftovers = [p.name for p in out.parent.glob("*.tmp")]
        self.assertEqual(leftovers, [])
        return proc.returncode, output, log

    def test_practice_cases(self):
        code, out, log = self.run_main(CASES)
        self.assertEqual(code, 0)
        errors, _ = check(CASES, out)
        self.assertEqual(errors, [])
        self.assertTrue(all(r["status"] == "success" for r in out))
        self.assertEqual(log["summary"]["total_cases"], len(CASES))
        self.assertEqual(log["summary"]["fallback"], 0)
        self.assertEqual(len(log["input_sha256"]), 64)
        self.assertEqual(len(log["output_sha256"]), 64)
        self.assertNotIn("Authorization", json.dumps(log))
        # 评测预检可能读 results.json，两份内容必须一致
        self.assertEqual(self.last_alias, out)

    def test_legacy_entry(self):
        code, out, _ = self.run_main(CASES[:1], new_entry=False)
        self.assertEqual(code, 0)
        self.assertEqual(out[0]["status"], "success")

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
        # 配置缺失退出码仍为 2，但不能空手返回：必须落一份保底草稿
        code, out, log = self.run_main(CASES[:1], env=False)
        self.assertEqual(code, 2)
        self.assertIsNotNone(out)
        self.assertEqual([r["case_id"] for r in out], [CASES[0]["case_id"]])
        self.assertEqual(out[0]["status"], "success")
        self.assertTrue(out[0]["medical_record"])
        self.assertTrue(log["cases"][0]["fallback"])
        self.assertEqual(log["cases"][0]["fallback_reason"]["code"], "NO_MODEL_CONFIG")
        self.assertEqual(log["error"]["type"], "ConfigError")
        self.assertEqual(log["exit_code"], 2)

    def test_duplicate_case_id(self):
        code, out, _ = self.run_main([CASES[0], CASES[0]])
        self.assertEqual(code, 2)
        self.assertIsNone(out)

    def test_single_object_accepted_broken_rejected(self):
        code, out, log = self.run_main(None, raw_input=json.dumps(CASES[0], ensure_ascii=False))
        self.assertEqual(code, 0)
        self.assertEqual([r["case_id"] for r in out], [CASES[0]["case_id"]])
        self.assertTrue(log.get("input_notes"))
        code, _, _ = self.run_main(None, raw_input="{broken")
        self.assertEqual(code, 2)
        code, _, _ = self.run_main(None, raw_input="42")
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

    def test_unreachable_model_falls_back(self):
        code, out, log = self.run_main(CASES[:1], endpoint="http://127.0.0.1:9/v1/chat/completions")
        self.assertEqual(code, 0)
        self.assertEqual(out[0]["status"], "success")
        errs = [i for i in audit_record(out[0]["medical_record"], CASES[0]) if i["severity"] == "error"]
        self.assertEqual(errs, [])
        self.assertTrue(log["cases"][0]["fallback"])
        self.assertEqual(log["cases"][0]["fallback_reason"]["code"], "MODEL_UNAVAILABLE")
        self.assertEqual(log["summary"]["fallback"], 1)
        self.assertGreaterEqual(log["summary"]["failed_model_calls"], 1)

    def test_unreachable_model_no_fallback(self):
        code, out, _ = self.run_main(CASES[:1], endpoint="http://127.0.0.1:9/v1/chat/completions",
                                     args=["--no-fallback"])
        self.assertEqual(code, 0)
        self.assertEqual(out[0]["status"], "failed")
        self.assertEqual(out[0]["error"]["code"], "MODEL_UNAVAILABLE")

    def test_api_key_sent_when_configured(self):
        code, out, log = self.run_main(CASES[:1], endpoint=self.kendpoint, extra_env="API_KEY=test-key\n")
        self.assertEqual(code, 0)
        self.assertEqual(out[0]["status"], "success")
        self.assertFalse(log["cases"][0]["fallback"])
        self.assertNotIn("test-key", json.dumps(log))

    def test_rag_with_embeddings(self):
        embed = self.endpoint.replace("/chat/completions", "/embeddings")
        code, out, log = self.run_main(CASES[:2], extra_env=f"EMBED_ENDPOINT={embed}\n", args=["--rag"])
        self.assertEqual(code, 0)
        self.assertTrue(all(r["status"] == "success" for r in out))
        if (ROOT / "resources" / "kb" / "cards.jsonl").is_file():
            self.assertEqual(log["retrieval"]["method"], "bge-m3")
            self.assertTrue(log["embed_calls"])

    def test_rag_embed_down_falls_back_to_bm25(self):
        embed = self.dendpoint.replace("/chat/completions", "/embeddings")
        code, out, log = self.run_main(CASES[:1], endpoint=self.dendpoint,
                                       extra_env=f"EMBED_ENDPOINT={embed}\n", args=["--rag"])
        self.assertEqual(code, 0)
        self.assertEqual(out[0]["status"], "success")
        if (ROOT / "resources" / "kb" / "cards.jsonl").is_file():
            self.assertEqual(log["retrieval"]["method"], "bm25")

    @unittest.skipUnless(OFFICIAL_FILE.is_file(), "未生成官方练习集转换文件")
    def test_official_practice_cases(self):
        cases = json.loads(OFFICIAL_FILE.read_text(encoding="utf-8"))
        code, out, log = self.run_main(cases)
        self.assertEqual(code, 0)
        errors, _ = check(cases, out)
        self.assertEqual(errors, [])
        self.assertTrue(all(r["status"] == "success" for r in out), [r["error"] for r in out])

    @unittest.skipUnless(OFFICIAL_RAW.is_dir(), "未解压官方练习集")
    def test_official_raw_format_single_file(self):
        # 练习集原始格式（入院记录对象，且 08 号文件缺结尾括号）直接作为输入
        f = sorted(OFFICIAL_RAW.glob("08_*.json"))[0]
        code, out, log = self.run_main(None, raw_input=f.read_text(encoding="utf-8-sig"))
        self.assertEqual(code, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "success")
        self.assertTrue(any("格式问题" in n for n in log["input_notes"]))

    def test_truncated_response_keeps_usage_and_does_not_retry(self):
        from io import BytesIO

        class Reply(BytesIO):
            status = 200

        raw = b'{"choices":[{"finish_reason":"length","message":{"content":""}}],"usage":{"completion_tokens":3000}}'
        client = LLMClient("mock", self.endpoint, {"max_tokens": 3000}, 1, 2, 0)
        logs = []
        with patch.object(client._opener, "open", return_value=Reply(raw)) as opener:
            with self.assertRaises(ModelCallError) as caught:
                client.chat([], "generate", CallBudget(6), logs)
        self.assertEqual(caught.exception.code, "MODEL_OUTPUT_TRUNCATED")
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(logs[0]["finish_reason"], "length")
        self.assertEqual(logs[0]["usage"]["completion_tokens"], 3000)


if __name__ == "__main__":
    unittest.main()
