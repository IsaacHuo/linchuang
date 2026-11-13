"""开发期本地转发代理：让"无密钥调用"的主程序也能连上需要 API Key 的 Qwen 服务（如阿里云百炼）。

主程序按赛题要求不发送 Authorization；本代理在本机为请求补上密钥再转发。
仅用于赛前开发调试，正式评测时主办方提供无密钥接口，不需要本代理。

  set DASHSCOPE_API_KEY=你的密钥
  python tools/dev_proxy.py --upstream https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
  .env 中写：ENDPOINT=http://127.0.0.1:18081/v1/chat/completions   MODEL_NAME=qwen-plus（按实际可用模型）
"""
import argparse
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_handler(upstream, key):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            req = urllib.request.Request(upstream, data=body, method="POST", headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            try:
                with opener.open(req, timeout=300) as resp:
                    code, data = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                code, data = exc.code, exc.read()
            except Exception as exc:
                code, data = 502, str(exc).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            print("proxy", fmt % args)

    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--port", type=int, default=18081)
    ap.add_argument("--key-env", default="DASHSCOPE_API_KEY")
    a = ap.parse_args()
    key = os.environ.get(a.key_env)
    if not key:
        raise SystemExit(f"请先设置环境变量 {a.key_env}")
    print(f"转发 http://127.0.0.1:{a.port} -> {a.upstream}")
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.upstream, key)).serve_forever()
