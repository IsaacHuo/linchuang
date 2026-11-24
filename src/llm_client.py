"""Chat Completions 兼容接口调用（默认无密钥、不走系统代理）。"""
import hashlib
import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

RETRYABLE_HTTP = {408, 409, 425, 429, 500, 502, 503, 504}
# 接口拒绝未知字段（vLLM 的 extra="forbid"）时的报错特征；用于自动摘掉不被接受的参数后重试
UNSUPPORTED_HINTS = ("extra_forbidden", "extra fields", "unknown", "unrecognized", "unexpected",
                     "not permitted", "invalid_request_error", "extra inputs")
# 这些字段是 OpenAI 兼容层的核心字段，永不自动摘除
STRICT_KEYS = {"model", "messages", "stream", "temperature", "max_tokens"}
# 允许被自动摘除的可选字段（接口版本差异主要在这里）
OPTIONAL_KEYS = ("chat_template_kwargs", "reasoning", "top_p", "seed", "response_format")


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


class SubBudget:
    """从父预算中划出的子额度：用于给某一步设上限，保证后续步骤仍有调用次数。"""

    def __init__(self, parent: CallBudget, limit: int):
        self.parent = parent
        self.limit = limit
        self.used = 0

    @property
    def remaining(self) -> int:
        return min(self.limit - self.used, self.parent.remaining)

    def consume(self) -> None:
        if self.used >= self.limit:
            raise ModelCallError("CALL_BUDGET_EXCEEDED", f"该步骤已达到调用上限 {self.limit} 次")
        self.parent.consume()
        self.used += 1


class LLMClient:
    def __init__(self, model_name, endpoint, gen_params, timeout_s, max_retries, backoff_s, api_key=""):
        self.api_key = api_key or ""
        self.model_name = model_name
        self.endpoint = endpoint
        self.gen_params = dict(gen_params)
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        # 等价于 requests 的 trust_env=False：不读取系统代理配置
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._drop_keys = set()          # 被接口 400 拒掉的参数，后续请求不再发送
        self.stripped_params = []        # 写入 run_log，便于事后核对实际发出的参数

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:  # 仅在配置了密钥时发送；密钥不写入任何日志
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _strip_unsupported(self, detail: str) -> bool:
        """接口对未知字段报 400 时，摘掉被点名的可选字段并返回 True（下次请求生效）。"""
        low = detail.lower()
        if not any(h in low for h in UNSUPPORTED_HINTS):
            return False
        removable = [k for k in self.gen_params if k not in STRICT_KEYS and k not in self._drop_keys]
        named = [k for k in removable if k.lower() in low]
        drop = named or [k for k in removable if k in OPTIONAL_KEYS]
        if not drop:
            return False
        self._drop_keys.update(drop)
        self.stripped_params = sorted(self._drop_keys)
        return True

    def _build_body(self, messages, params=None):
        body = {"model": self.model_name, "messages": messages, "stream": False}
        body.update({k: v for k, v in self.gen_params.items() if k not in self._drop_keys})
        if params:
            body.update(params)
        return body

    def chat(self, messages, purpose: str, budget: CallBudget, call_log: list, params=None) -> str:
        """返回模型回复文本。每次 HTTP 尝试都写入 call_log（供 run_log 核查）。"""
        last = ModelCallError("MODEL_UNAVAILABLE", "未发起调用")
        attempt = 0
        while attempt <= self.max_retries:
            budget.consume()
            body = self._build_body(messages, params)
            payload = json.dumps(body, ensure_ascii=False)
            rec = {
                "call_no": len(call_log) + 1,
                "purpose": purpose,
                "attempt": attempt + 1,
                "started_at": now_iso(),
                "request_sha256": sha256_text(payload),
                "gen_params_sent": bool(self.gen_params or params),
            }
            t0 = time.monotonic()
            retryable = False
            try:
                req = urllib.request.Request(
                    self.endpoint,
                    data=payload.encode("utf-8"),
                    headers=self._headers(),
                    method="POST",
                )
                with self._opener.open(req, timeout=self.timeout_s) as resp:
                    status = resp.status
                    raw = resp.read().decode("utf-8")
                rec["http_status"] = status
                obj = json.loads(raw)
                rec["usage"] = obj.get("usage")
                # Thinking 是否真的关闭：vLLM 会回报推理 token 数
                tokens = ((obj.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
                if tokens:
                    rec["reasoning_tokens"] = tokens
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
                if exc.code == 400 and self._strip_unsupported(detail):
                    # 参数不被该接口接受：摘掉后立刻重试一次，不等待退避
                    rec["stripped_params"] = list(self.stripped_params)
                    call_log.append(rec)
                    attempt += 1
                    continue
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
