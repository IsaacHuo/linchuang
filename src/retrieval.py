"""鉴别知识卡片检索：BGE-M3 向量余弦为主，字符二元组 BM25 为退回。

- 卡片来自 resources/kb/cards.jsonl（团队自编，见 resource_manifest.json）；文件缺失时检索整体关闭。
- 卡片向量在启动时一次性批量计算；向量服务不可用时整体退回 BM25，单次查询失败时该次退回 BM25。
- 同一个 Embedder 也用于 evidence 与所引资料的语义对齐检查（仅记 warn，不触发修复）。
- 检索结果只作为"鉴别参考"注入提示，明确标注不是本病例事实。
"""
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from .llm_client import now_iso


class EmbedError(Exception):
    pass


def normalize_embed_endpoint(endpoint: str) -> str:
    ep = (endpoint or "").strip().rstrip("/")
    if ep and not ep.endswith("/embeddings"):
        ep += "/embeddings"
    return ep


class Embedder:
    """OpenAI 兼容 /embeddings 接口（BGE-M3）。带进程内缓存，线程安全。"""

    def __init__(self, endpoint, model="bge-m3", api_key="", timeout_s=60, batch=32):
        self.endpoint = normalize_embed_endpoint(endpoint)
        self.model = model
        self.api_key = api_key or ""
        self.timeout_s = timeout_s
        self.batch = batch
        self._cache = {}
        self._lock = threading.Lock()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.calls = []  # 调用记录（不含密钥与正文）

    def _post(self, texts):
        body = json.dumps({"model": self.model, "input": texts}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        t0 = time.monotonic()
        rec = {"started_at": now_iso(), "n": len(texts)}
        try:
            req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
            with self._opener.open(req, timeout=self.timeout_s) as resp:
                obj = json.loads(resp.read().decode("utf-8"))
            data = sorted(obj["data"], key=lambda d: d.get("index", 0))
            vecs = [list(map(float, d["embedding"])) for d in data]
            if len(vecs) != len(texts):
                raise EmbedError("向量条数与输入不一致")
            rec.update(ok=True)
            return vecs
        except EmbedError as exc:
            rec.update(ok=False, error=str(exc))
            raise
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
            rec.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:200])
            raise EmbedError(str(exc)) from exc
        finally:
            rec["duration_s"] = round(time.monotonic() - t0, 3)
            with self._lock:
                self.calls.append(rec)

    def embed(self, texts):
        with self._lock:
            todo = [t for t in dict.fromkeys(texts) if t not in self._cache]
        for i in range(0, len(todo), self.batch):
            chunk = todo[i:i + self.batch]
            vecs = self._post(chunk)
            with self._lock:
                self._cache.update(zip(chunk, vecs))
        with self._lock:
            return [self._cache[t] for t in texts]


def cosine(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


# ---------------------------------------------------------------- 词法退回
_KEEP = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")


def bigrams(text: str):
    out = []
    for seg in _KEEP.findall(text or ""):
        seg = seg.lower()
        if len(seg) == 1:
            out.append(seg)
        out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return out


def lexical_containment(statement: str, reference: str) -> float:
    """statement 的字符二元组有多少比例出现在 reference 中（0–1）。"""
    a = set(bigrams(statement))
    if not a:
        return 1.0
    return len(a & set(bigrams(reference))) / len(a)


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.toks = [Counter(bigrams(d)) for d in docs]
        self.lens = [sum(t.values()) for t in self.toks]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 0
        df = Counter(w for t in self.toks for w in t)
        n = len(docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query):
        q = Counter(bigrams(query))
        out = []
        for tf, ln in zip(self.toks, self.lens):
            s = 0.0
            for w in q:
                f = tf.get(w)
                if f:
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / (self.avg or 1)))
            out.append(s)
        return out


# ---------------------------------------------------------------- 卡片库
def load_cards(path: Path):
    """读取 JSONL 卡片；文件不存在返回 []。格式错误的行抛 ValueError（启动即暴露，不静默）。"""
    path = Path(path)
    if not path.is_file():
        return []
    cards = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            card = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} 第 {n} 行不是合法 JSON：{exc}") from exc
        for key in ("id", "topic", "triggers", "differentials"):
            if key not in card:
                raise ValueError(f"{path.name} 第 {n} 行缺少字段 {key}")
        cards.append(card)
    return cards


def card_doc(card) -> str:
    names = "、".join(d.get("name", "") for d in card.get("differentials", []))
    return f"{card['topic']}。{'、'.join(card.get('triggers', []))}。鉴别：{names}"


class Retriever:
    VEC_MIN = 0.35   # BGE-M3 余弦下限：低于此值视为不相关
    TRIGGER_BONUS = 0.15

    def __init__(self, cards, embedder=None):
        self.cards = list(cards)
        self.embedder = embedder
        self.docs = [card_doc(c) for c in self.cards]
        self.bm25 = BM25(self.docs) if self.cards else None
        self.vectors = None
        self.method = "bm25"
        self.prepare_error = None

    def prepare(self):
        """一次性计算卡片向量；失败则整体使用 BM25。"""
        if not self.cards or self.embedder is None:
            return self
        try:
            self.vectors = self.embedder.embed(self.docs)
            self.method = "bge-m3"
        except EmbedError as exc:
            self.prepare_error = str(exc)[:200]
            self.vectors = None
            self.method = "bm25"
        return self

    def _trigger_hits(self, query):
        return [sum(1 for t in c.get("triggers", []) if t and t in query) for c in self.cards]

    def search(self, query: str, k: int = 3):
        """返回 [{card, score, method}]，按相关性降序，最多 k 条。"""
        if not self.cards or not query.strip():
            return []
        trig = self._trigger_hits(query)
        method = self.method
        scored = None
        if self.vectors is not None:
            try:
                qv = self.embedder.embed([query])[0]
                scored = [(cosine(qv, v) + self.TRIGGER_BONUS * min(t, 2), i, t) for i, (v, t) in enumerate(zip(self.vectors, trig))]
                scored = [x for x in scored if x[0] >= self.VEC_MIN or x[2] > 0]
            except EmbedError:
                scored, method = None, "bm25"
        if scored is None:
            raw = self.bm25.scores(query)
            top = max(raw) or 1.0
            scored = [(s / top + self.TRIGGER_BONUS * min(t, 2), i, t) for i, (s, t) in enumerate(zip(raw, trig))]
            scored = [x for x in scored if x[2] > 0 or x[0] >= 0.5]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [{"card": self.cards[i], "score": round(s, 4), "method": method} for s, i, _ in scored[:k]]


def format_cards(hits) -> str:
    """把检索到的卡片格式化成提示块。只含通用鉴别思路，不含任何病例数值。"""
    lines = []
    for h in hits:
        c = h["card"]
        lines.append(f"◆ {c['topic']}")
        for d in c.get("differentials", []):
            seg = f"  - {d.get('name', '')}"
            if d.get("support"):
                seg += f"；支持点：{d['support']}"
            if d.get("against"):
                seg += f"；不支持点：{d['against']}"
            if d.get("must_check"):
                seg += f"；需核查：{d['must_check']}"
            lines.append(seg)
        if c.get("red_flags"):
            lines.append("  危险信号：" + "、".join(c["red_flags"]))
        if c.get("plan_hints"):
            lines.append("  计划要点：" + "；".join(c["plan_hints"]))
    return "\n".join(lines)


class Aligner:
    """evidence.statement 与所引资料的一致性评分：有向量服务用余弦，否则用二元组覆盖率。"""

    LEXICAL_THRESHOLD = 0.3

    def __init__(self, embedder=None, threshold=0.45):
        self.embedder = embedder
        self.threshold = threshold

    def check(self, record, case):
        smap = {s["source_id"]: s.get("content", "") for s in case["sources"]}
        out = []
        for i, e in enumerate(record.get("evidence") or []):
            if not isinstance(e, dict) or e.get("kind") != "fact":
                continue  # 推断条目本就不应与原文逐字对应
            ref = "\n".join(smap.get(x, "") for x in e.get("source_ids") or [])
            st = e.get("statement") or ""
            if not ref or not st:
                continue
            method, score, thr = "lexical", lexical_containment(st, ref), self.LEXICAL_THRESHOLD
            if self.embedder is not None:
                try:
                    a, b = self.embedder.embed([st, ref])
                    method, score, thr = "bge-m3", cosine(a, b), self.threshold
                except EmbedError:
                    pass
            if score < thr:
                out.append({"code": "EVIDENCE_ALIGN", "severity": "warn", "field": f"evidence[{i}]",
                            "detail": f"与所引资料相似度 {score:.2f} 低于 {thr}（{method}）"})
        return out
