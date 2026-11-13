"""读取 .env（不依赖第三方库）。"""
from pathlib import Path


class ConfigError(Exception):
    """必要配置缺失，对应退出码 2。"""


def load_env(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"未找到配置文件 {path.name}")
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


def load_model_config(path: Path):
    values = load_env(path)
    model_name = (values.get("MODEL_NAME") or "").strip()
    endpoint = (values.get("ENDPOINT") or "").strip()
    if not model_name or not endpoint:
        raise ConfigError(".env 必须提供非空的 MODEL_NAME 和 ENDPOINT")
    if not endpoint.lower().startswith(("http://", "https://")):
        raise ConfigError("ENDPOINT 必须是完整的 http(s) 请求地址")
    return model_name, endpoint
