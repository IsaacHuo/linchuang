"""读取模型配置：环境变量优先，其次 .env（不依赖第三方库）。"""
import os
from pathlib import Path


class ConfigError(Exception):
    """必要配置缺失，对应退出码 2。"""


def load_env(path: Path) -> dict:
    if not path.is_file():
        return {}
    values = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _pick(values: dict, key: str) -> str:
    return (os.environ.get(key) or values.get(key) or "").strip()


def _check_url(name: str, url: str) -> None:
    if not url.lower().startswith(("http://", "https://")):
        raise ConfigError(f"{name} 必须是完整的 http(s) 请求地址")


def load_runtime_config(path: Path) -> dict:
    """返回 {model_name, endpoint, api_key, embed_endpoint, embed_model, embed_api_key}。

    必填 MODEL_NAME、ENDPOINT；API_KEY 可选（有才发 Authorization）；
    EMBED_ENDPOINT/EMBED_MODEL 可选（BGE-M3），缺失时检索退回本地 BM25。
    """
    values = load_env(path)
    cfg = {
        "model_name": _pick(values, "MODEL_NAME"),
        "endpoint": _pick(values, "ENDPOINT"),
        "api_key": _pick(values, "API_KEY"),
        "embed_endpoint": _pick(values, "EMBED_ENDPOINT"),
        "embed_model": _pick(values, "EMBED_MODEL") or "bge-m3",
        "embed_api_key": _pick(values, "EMBED_API_KEY") or _pick(values, "API_KEY"),
    }
    if not cfg["model_name"] or not cfg["endpoint"]:
        where = ".env 或环境变量" if path.is_file() else f"环境变量（未找到 {path.name}）"
        raise ConfigError(f"{where}必须提供非空的 MODEL_NAME 和 ENDPOINT")
    _check_url("ENDPOINT", cfg["endpoint"])
    if cfg["embed_endpoint"]:
        _check_url("EMBED_ENDPOINT", cfg["embed_endpoint"])
    return cfg


def load_model_config(path: Path):
    """旧接口：返回 (model_name, endpoint)。"""
    cfg = load_runtime_config(path)
    return cfg["model_name"], cfg["endpoint"]
