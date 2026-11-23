"""临床智录 —— 住院首次病程记录生成：统一启动入口。

用法（材料提交通道规定的入口）：
    python main.py --input input.json --output output.json
兼容旧入口：不带参数时读取 main.py 同目录的 input.json，写出同目录 output.json。
运行日志 run_log.json 写在 output 同目录。

模型配置（环境变量优先，其次 main.py 同目录 .env）：MODEL_NAME、ENDPOINT 必填；
API_KEY、EMBED_ENDPOINT、EMBED_MODEL、EMBED_API_KEY 可选（未配置向量服务时检索自动退回 BM25）。

退出码：0 全部处理完成（允许单条失败）；1 任务级运行错误；2 配置缺失或输入不符合协议。
"""
import argparse
import hashlib
import logging
import random
import sys
import traceback
from pathlib import Path

from src import config
from src.case_io import InputError, read_cases, write_json_atomic
from src.envfile import ConfigError, load_runtime_config
from src.llm_client import LLMClient, now_iso
from src.pipeline import MODES, Pipeline
from src.prompts import PromptSet
from src.retrieval import Embedder, Retriever, load_cards
from src.runner import new_run_log, run_cases, summarize

log = logging.getLogger("linchuang")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="住院首次病程记录生成")
    ap.add_argument("--input", help="输入病例 JSON 文件（默认 main.py 同目录 input.json）")
    ap.add_argument("--output", help="输出 JSON 文件（默认 main.py 同目录 output.json）")
    ap.add_argument("--mode", choices=sorted(MODES), default=config.PIPELINE_MODE, help="流水线模式（实验用）")
    rag = ap.add_mutually_exclusive_group()
    rag.add_argument("--rag", dest="rag", action="store_true", default=None, help="启用鉴别知识检索")
    rag.add_argument("--no-rag", dest="rag", action="store_false", help="关闭鉴别知识检索")
    ap.add_argument("--candidates", type=int, default=None, help="生成候选数（默认见 config.N_CANDIDATES）")
    ap.add_argument("--no-fallback", action="store_true", help="关闭保底记录（实验用）")
    return ap.parse_args(argv)


def _build_retriever(rt, use_rag, run_log):
    """返回 (retriever, embedder)。卡片或向量服务任何一环缺失都只降级，不报错。"""
    embedder = None
    if rt.get("embed_endpoint"):
        embedder = Embedder(rt["embed_endpoint"], rt.get("embed_model") or "bge-m3",
                            rt.get("embed_api_key") or rt.get("api_key"), config.EMBED_TIMEOUT_S)
    info = {"enabled": bool(use_rag), "embed_endpoint": embedder.endpoint if embedder else None,
            "embed_model": embedder.model if embedder else None}
    retriever = None
    if use_rag:
        cards = load_cards(config.KB_FILE)
        info["cards"] = len(cards)
        if cards:
            retriever = Retriever(cards, embedder).prepare()
            info.update(method=retriever.method, prepare_error=retriever.prepare_error)
        else:
            info["method"] = "disabled（未找到知识卡片）"
    run_log["retrieval"] = info
    return retriever, embedder


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    args = parse_args(argv)
    input_path = Path(args.input).resolve() if args.input else config.DEFAULT_INPUT
    output_path = Path(args.output).resolve() if args.output else config.DEFAULT_OUTPUT
    run_log_path = output_path.with_name(config.RUN_LOG_NAME)
    random.seed(config.RANDOM_SEED)

    run_log = new_run_log(args.mode)
    run_log.update(input=str(input_path), output=str(output_path))
    exit_code = 1
    embedder = None
    try:
        run_log_path.unlink(missing_ok=True)  # 避免残留旧日志与本次输出混淆
        rt = load_runtime_config(config.ENV_FILE)
        run_log.update(model_name=rt["model_name"], endpoint=rt["endpoint"])
        input_hash = hashlib.sha256(input_path.read_bytes()).hexdigest() if input_path.is_file() else None
        cases, notes = read_cases(input_path)
        if notes:
            run_log["input_notes"] = notes
            for n in notes:
                log.info("输入适配：%s", n)
        prompts = PromptSet(config.PROMPT_DIR)
        run_log["prompt_version"] = {"version": prompts.version, "files": prompts.hashes}

        use_rag = args.rag if args.rag is not None else (config.USE_RAG or args.mode == "improved+rag")
        if args.mode in ("baseline", "extract"):
            use_rag = False
        retriever, embedder = _build_retriever(rt, use_rag, run_log) if cases else (None, None)
        fallback = False if args.no_fallback else config.ENABLE_FALLBACK
        run_log["options"] = {"use_rag": bool(use_rag), "fallback": fallback,
                              "n_candidates": args.candidates or config.N_CANDIDATES}
        log.info("模型 %s，病例 %d 例，模式 %s，检索 %s", rt["model_name"], len(cases), args.mode,
                 run_log.get("retrieval", {}).get("method", "off"))

        client = LLMClient(rt["model_name"], rt["endpoint"], config.GEN_PARAMS, config.REQUEST_TIMEOUT_S,
                           config.MAX_RETRIES, config.RETRY_BACKOFF_S, api_key=rt.get("api_key"))
        pipeline = Pipeline(client, prompts, args.mode, retriever=retriever, embedder=embedder,
                            use_rag=use_rag, n_candidates=args.candidates, fallback=fallback)
        results, case_logs = run_cases(cases, pipeline, config.CONCURRENCY)
        for lg in case_logs:
            log.info("%s -> %s%s（调用 %d 次，%.1fs）", lg["case_id"], lg["status"],
                     "（保底）" if lg.get("fallback") else "", lg["model_calls"], lg["duration_s"])
        write_json_atomic(output_path, results)
        run_log["input_sha256"] = input_hash
        run_log["output_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
        run_log["cases"] = case_logs
        run_log["summary"] = summarize(results, case_logs)
        exit_code = 0
    except (ConfigError, InputError) as exc:
        log.error("%s", exc)
        run_log["error"] = {"type": type(exc).__name__, "message": str(exc)}
        exit_code = 2
    except Exception as exc:
        log.error("任务级运行错误：%s", exc)
        run_log["error"] = {"type": type(exc).__name__, "message": str(exc),
                            "traceback": traceback.format_exc()[-2000:]}
        exit_code = 1
    finally:
        if embedder is not None:
            run_log["embed_calls"] = embedder.calls
        run_log["finished_at"] = now_iso()
        run_log["exit_code"] = exit_code
        try:
            write_json_atomic(run_log_path, run_log)
        except Exception as exc:
            log.error("run_log.json 写入失败：%s", exc)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
