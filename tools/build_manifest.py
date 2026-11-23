"""生成 resource_manifest.json（知识资源登记清单）。卡片内容变动后重跑；pack.py 打包前也会自动重跑。

  python tools/build_manifest.py [--version v1]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402
from src.retrieval import load_cards  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def build(version: str) -> dict:
    resources = []
    kb = config.KB_FILE
    if kb.is_file():
        cards = load_cards(kb)
        resources.append({
            "name": "鉴别诊断知识卡片",
            "path": kb.relative_to(ROOT).as_posix(),
            "type": "团队自编知识库（JSONL，每行一张卡片）",
            "version": version,
            "records": len(cards),
            "sha256": sha256(kb),
            "source": "团队成员依据《内科学》《外科学》《妇产科学》《儿科学》等规划教材及公开临床指南要点自行整理编写，"
                      "不含任何真实患者信息、不含具体数值与药物剂量",
            "license": "团队自有，仅用于本次比赛",
            "usage": "按主诉/初步诊断/问题清单检索 top-k 卡片，作为\"鉴别参考（非本病例事实）\"注入生成提示；"
                     "由 config.USE_RAG 或命令行 --rag/--no-rag 开关控制",
            "review": "resources/kb/REVIEW.md（医学队员审核记录）",
        })
    return {
        "manifest_version": "1.0",
        "algorithm_version": version,
        "knowledge_resources": resources or "无",
        "models": [
            {"name": "Qwen（赛事内网 API）", "usage": "事实与问题清单抽取、首次病程记录生成、按校验问题定向修复",
             "config": "MODEL_NAME / ENDPOINT / API_KEY（环境变量或 .env）"},
            {"name": "BGE-M3（赛事内网 API）", "usage": "知识卡片与查询的向量检索；evidence 与所引资料的语义一致性检查（仅记录告警）",
             "config": "EMBED_ENDPOINT / EMBED_MODEL / EMBED_API_KEY；未配置或不可用时自动退回本地字符二元组 BM25"},
        ],
        "external_network": "不访问公网及任何外部 API，只调用赛事内网 Qwen 与 BGE-M3",
        "random_seed": config.RANDOM_SEED,
        "generation_params": config.GEN_PARAMS,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="v1")
    a = ap.parse_args()
    out = ROOT / "resource_manifest.json"
    out.write_text(json.dumps(build(a.version), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写出 {out}")


if __name__ == "__main__":
    main()
