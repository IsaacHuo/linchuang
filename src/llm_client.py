"""Chat Completions 兼容接口调用（无密钥、不走系统代理）。"""
import hashlib
import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

RETRYABLE_HTTP = {408, 409, 425, 429, 500, 502, 503, 504}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ModelCallError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CallBudget:
    """每例调用预算（含重试），超出即停止对该例的调用。"""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def consume(self) -> None:
        if self.used >= self.limit:
            raise ModelCallError("CALL_BUDGET_EXCEEDED", f"已达到每例调用上限 {self.limit} 次")
        self.used += 1


class LLMClient:
    def __init__(self, model_name, endpoint, gen_params, timeout_s, max_retries, backoff_s):
        self.model_name = model_name
        self.endpoint = endpoint
        self.gen_params = dict(gen_params)
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        # 等价于 requests 的 trust_env=False：不读取系统代理配置
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _build_body(self, messages):
        body = {"model": self.model_name, "messages": messages, "stream": False}
        body.update(self.gen_params)
        return body

    def chat(self, messages, purpose: str, budget: CallBudget, call_log: list) -> str:
        """返回模型回复文本。每次 HTTP 尝试都写入 call_log（供 run_log 核查）。"""
        last = ModelCallError("MODEL_UNAVAILABLE", "未发起调用")
        attempt = 0
        while attempt <= self.max_retries:
            budget.consume()
            body = self._build_body(messages)
            payload = json.dumps(body, ensure_ascii=False)
            rec = {
                "call_no": len(call_log) + 1,
                "purpose": purpose,
                "attempt": attempt + 1,
                "started_at": now_iso(),
                "request_sha256": sha256_text(payload),
                "gen_params_sent": bool(self.gen_params),
            }
            t0 = time.monotonic()
            retryable = False
            try:
                req = urllib.request.Request(
                    self.endpoint,
                    data=payload.encode("utf-8"),
                    headers={"Content-Type": "application/json"},  # 不发送 Authorization
                    method="POST",
                )
                with self._opener.open(req, timeout=self.timeout_s) as resp:
                    status = resp.status
                    raw = resp.read().decode("utf-8")
                rec["http_status"] = status
                obj = json.loads(raw)
                rec["usage"] = obj.get("usage")
                choice = obj["choices"][0]
                rec["finish_reason"] = choice.get("finish_reason")
                content = choice["message"].get("content")
                if isinstance(content, str):
                    rec["response_sha256"] = sha256_text(content)
                if rec["finish_reason"] == "length":
                    raise ModelCallError("MODEL_OUTPUT_TRUNCATED", "模型达到生成长度上限，正文可能不完整")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("模型返回内容为空")
                rec.update(ok=True, finished_at=now_iso(),
                           duration_s=round(time.monotonic() - t0, 3))
                call_log.append(rec)
                return content
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                rec["http_status"] = exc.code
                last = ModelCallError("MODEL_HTTP_ERROR", f"HTTP {exc.code}: {detail}")
                retryable = exc.code in RETRYABLE_HTTP
            except ModelCallError as exc:
                last = exc
            except (socket.timeout, TimeoutError):
                last = ModelCallError("MODEL_TIMEOUT", "模型请求超时")
                retryable = True
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                    last = ModelCallError("MODEL_TIMEOUT", "模型请求超时")
                else:
                    last = ModelCallError("MODEL_UNAVAILABLE", f"无法连接模型服务：{exc.reason}")
                retryable = True
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                last = ModelCallError("MODEL_BAD_RESPONSE", f"模型响应结构异常：{exc}")
                retryable = True
            rec.update(ok=False, error_code=last.code, error=last.message[:300],
                       finished_at=now_iso(), duration_s=round(time.monotonic() - t0, 3))
            call_log.append(rec)
            if not retryable or budget.remaining <= 0:
                break
            attempt += 1
            if attempt <= self.max_retries:
                time.sleep(self.backoff_s * attempt)
        if last.code in ("MODEL_TIMEOUT", "MODEL_UNAVAILABLE", "MODEL_HTTP_ERROR", "MODEL_BAD_RESPONSE"):
            last = ModelCallError(last.code, last.message + "（已达到允许的重试次数）")
        raise last
