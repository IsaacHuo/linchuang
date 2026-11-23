"""按《材料提交通道》打包：dist/<团队名称>_<算法版本>.zip。

  python tools/pack.py --team 医智先锋队 --version v1          # zip 内顶层为 <团队>_<版本>/ 目录
  python tools/pack.py --team 医智先锋队 --version v1 --flat   # 必备文件直接位于 zip 根目录
  python tools/pack.py ... --no-selftest                         # 跳过包内自测

打包后自检：必备文件齐全、不含 .env/缓存/虚拟环境、resource_manifest 哈希与卡片一致；
并解压到临时目录，用本地模拟模型按 python main.py --input --output 实跑一遍。
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REQUIRED = ["main.py", "requirements.txt", "README.md", "resource_manifest.json"]
EXTRA_FILES = ["requires.txt", ".env.example"]
DIRS = ["src", "prompts", "resources"]
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".venv", ".git"}
FORBIDDEN = {".env", "input.json", "output.json", "run_log.json"}


def collect():
    files = [ROOT / n for n in REQUIRED + EXTRA_FILES]
    for d in DIRS:
        if (ROOT / d).is_dir():
            files += [p for p in sorted((ROOT / d).rglob("*"))
                      if p.is_file() and not (SKIP_PARTS & set(p.parts)) and p.suffix != ".pyc"]
    missing = [p.name for p in files if not p.is_file()]
    if missing:
        sys.exit(f"缺少文件：{missing}")
    return files


def selftest(zip_path: Path, prefix: str) -> None:
    from tools import mock_server
    tmp = Path(tempfile.mkdtemp(prefix="lczl_pack_"))
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        app = tmp / prefix if prefix else tmp
        server, endpoint = mock_server.start(0)
        try:
            cases = json.loads((ROOT / "data" / "official_practice_cases.json").read_text(encoding="utf-8"))[:2]
            io = tmp / "io"
            io.mkdir()
            (io / "input.json").write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
            (app / ".env").write_text(f"MODEL_NAME=mock-qwen\nENDPOINT={endpoint}\n", encoding="utf-8")
            proc = subprocess.run([sys.executable, str(app / "main.py"), "--input", str(io / "input.json"),
                                   "--output", str(io / "output.json")], cwd=tmp, capture_output=True, timeout=300)
            if proc.returncode != 0:
                sys.exit(f"包内自测失败，退出码 {proc.returncode}：{proc.stderr.decode('utf-8', 'replace')[-800:]}")
            out = json.loads((io / "output.json").read_text(encoding="utf-8"))
            from tools.check_output import check
            errors, _ = check(cases, out)
            if errors or not all(r["status"] == "success" for r in out):
                sys.exit(f"包内自测输出不合规：{errors[:5]}")
            print(f"包内自测通过：{len(out)} 例，退出码 0，输出协议检查无错误")
        finally:
            server.shutdown()
    finally:
        shutil.rmtree(tmp, True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", required=True, help="团队名称（与邮件标题一致）")
    ap.add_argument("--version", default="v1", help="算法版本号，如 v1")
    ap.add_argument("--flat", action="store_true", help="不套顶层目录")
    ap.add_argument("--no-selftest", action="store_true")
    a = ap.parse_args()

    from tools.build_manifest import build
    manifest = build(a.version)
    (ROOT / "resource_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                                 encoding="utf-8")

    name = f"{a.team}_{a.version}"
    prefix = "" if a.flat else name
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    zip_path = dist / f"{name}.zip"
    files = collect()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            rel = p.relative_to(ROOT).as_posix()
            z.write(p, f"{prefix}/{rel}" if prefix else rel)

    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    base = {n[len(prefix) + 1:] if prefix else n for n in names}
    lacking = [n for n in REQUIRED if n not in base]
    bad = [n for n in base if Path(n).name in FORBIDDEN or SKIP_PARTS & set(Path(n).parts)]
    if lacking or bad:
        sys.exit(f"打包自检失败：缺少 {lacking}，不应包含 {bad}")
    size = zip_path.stat().st_size
    print(f"已生成 {zip_path}（{len(names)} 个文件，{size / 1024:.1f} KB）")
    kr = manifest["knowledge_resources"]
    print("知识资源：" + ("无" if kr == "无" else "；".join(f"{r['name']} {r['records']} 条 sha256={r['sha256'][:12]}" for r in kr)))
    if not a.no_selftest:
        selftest(zip_path, prefix)
    if (ROOT / ".env").exists():
        print("注意：本地 .env 未打入包（赛方环境自行配置 MODEL_NAME/ENDPOINT 等）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
