"""本地模拟模型服务（仅用于开发自测，不参与正式运行）。

实现 Chat Completions 兼容接口，根据提示词中的【阶段】标记返回规则拼接的 JSON，
用来验证流水线、异常处理、日志与输出协议，不代表真实模型的医学质量。

  python tools/mock_server.py --port 18080            # 正常输出
  python tools/mock_server.py --port 18080 --faulty   # 生成阶段故意注入典型错误，验证校验与修复
"""
import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_SRC = re.compile(r"^\[(S\d+)\] (.*)$", re.M)
_MISSING = re.compile(r"未提供([^，；。]+)")


def _sources(user):
    return _SRC.findall(user)


def _missing(sources):
    items = []
    for _, text in sources:
        for m in _MISSING.finditer(text):
            for part in re.split(r"[、及和或]", m.group(1)):
                part = part.strip()
                if len(part) >= 2 and part not in items:
                    items.append(part)
    return items


def _impression(sources):
    for sid, text in sources:
        if "初步判断" in text:
            return sid, re.sub(r"^.*?初步判断[为：:]*", "", text)
    return None, ""


def extract_reply(sources):
    sid, imp = _impression(sources)
    return {
        "positives": [{"text": t, "source_ids": [s]} for s, t in sources if s != sid],
        "negatives": [], "timeline": [{"text": sources[0][1], "source_ids": [sources[0][0]]}],
        "missing": _missing(sources), "conflicts": [],
        "initial_impression": {"text": imp, "source_ids": [sid] if sid else []},
        "problem_list": [{"problem": imp or "病因待查", "key_findings": [], "source_ids": [sid] if sid else [],
                          "must_not_miss": ["中枢神经系统感染"] if "惊厥" in imp else []}],
    }


def embed_vector(text, dim=64):
    """确定性的字符二元组哈希向量：相同/相近文本余弦高，供测试检索与对齐逻辑。"""
    import hashlib
    vec = [0.0] * dim
    t = re.sub(r"\s+", "", text)
    for i in range(max(1, len(t) - 1)):
        h = int(hashlib.md5(t[i:i + 2].encode("utf-8")).hexdigest(), 16)
        vec[h % dim] += 1.0
    return vec


def generate_reply(sources, faulty=False):
    sid, imp = _impression(sources)
    facts = [(s, t) for s, t in sources if s != sid]
    missing = _missing(sources)
    feats = "；".join(t for _, t in facts) + "。"
    if missing:
        feats += "以下资料不详：" + "、".join(missing) + "。"
    ids = "、".join(s for s, _ in facts)
    disc = (f"现有资料（{ids}）支持“{imp or '病因待查'}”的初步判断，但仍需结合进一步检查明确。"
            "鉴别方面需结合病程、体征及辅助检查逐一分析，目前资料不足以确定病因。")
    plan = ("拟补充核实" + ("、".join(missing) if missing else "既往病史及用药史") +
            "；建议按鉴别需要完善相关检查；观察症状变化，复查关键异常指标，依据结果调整处理。")
    evidence = [{"statement": t[:60], "source_ids": [s], "kind": "fact"} for s, t in facts[:4]]
    if sid:
        evidence.append({"statement": "入院初步判断尚需进一步检查明确", "source_ids": [sid], "kind": "inference"})
    rec = {"case_features": feats, "diagnostic_discussion": disc, "plan": plan,
           "evidence": evidence[:5], "review_items": [f"{m}未提供" for m in missing]}
    if faulty:
        # 典型错误：抄错数值、计划写成已实施、确诊性表述、引用不存在的编号
        rec["case_features"] = re.sub(r"(\d+)(\.\d+)?(?=\s*(?:g/L|mmol/L|μmol/L|mmHg|次/分|℃|ng/mL|pg/mL|mg/L))",
                                      lambda m: str(int(m.group(1)) + 1) + (m.group(2) or ""), rec["case_features"], count=1)
        rec["plan"] = "已予补液及对症治疗；" + rec["plan"]
        rec["diagnostic_discussion"] = "诊断明确。" + rec["diagnostic_discussion"]
        rec["evidence"].append({"statement": "补充依据", "source_ids": ["S99"], "kind": "fact"})
    return rec


class Handler(BaseHTTPRequestHandler):
    faulty = False
    api_key = ""          # 非空时要求 Bearer 密钥一致；为空时拒绝任何 Authorization 头（旧协议）
    embed_down = False    # 模拟向量服务不可用

    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        auth = self.headers.get("Authorization")
        if self.api_key:
            if auth != f"Bearer {self.api_key}":
                return self._send(401, {"error": "invalid api key"})
        elif auth:
            return self._send(400, {"error": "Authorization header must not be sent"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
        if self.path.rstrip("/").endswith("/embeddings"):
            if self.embed_down:
                return self._send(503, {"error": "embedding service down"})
            inp = body.get("input")
            inp = [inp] if isinstance(inp, str) else list(inp or [])
            return self._send(200, {"object": "list", "model": body.get("model"),
                                    "data": [{"object": "embedding", "index": i, "embedding": embed_vector(t)}
                                             for i, t in enumerate(inp)]})
        msgs = body.get("messages", [])
        system = next((m["content"] for m in msgs if m["role"] == "system"), "")
        user = next((m["content"] for m in msgs if m["role"] == "user"), "")
        sources = _sources(user)
        if not sources:
            return self._send(400, {"error": "no sources"})
        if "【阶段：事实抽取】" in system:
            content = json.dumps(extract_reply(sources), ensure_ascii=False)
        elif "【阶段：校验修复】" in system:
            content = json.dumps(generate_reply(sources, faulty=False), ensure_ascii=False)
        else:
            rec = generate_reply(sources, faulty=self.faulty)
            content = "```json\n" + json.dumps(rec, ensure_ascii=False, indent=1) + "\n```"
        self._send(200, {
            "id": "mock", "object": "chat.completion", "model": body.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(system) + len(user), "completion_tokens": len(content)},
        })


def start(port=0, faulty=False, api_key="", embed_down=False):
    """后台线程启动，返回 (server, endpoint)。供测试使用。向量接口为同主机 /v1/embeddings。"""
    handler = type("H", (Handler,), {"faulty": faulty, "api_key": api_key, "embed_down": embed_down})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--faulty", action="store_true")
    args = ap.parse_args()
    Handler.faulty = args.faulty
    print(f"mock model at http://127.0.0.1:{args.port}/v1/chat/completions faulty={args.faulty}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
