"""临床智录 —— 住院首次病程记录生成：统一启动入口。

用法（材料提交通道规定的入口）：
    python main.py --input input.json --output output.json
兼容旧入口：不带参数时读取 main.py 同目录的 input.json，写出同目录 output.json。
运行日志 run_log.json 写在 output 同目录。

模型配置（环境变量优先，其次 main.py 同目录 .env）：MODEL_NAME、ENDPOINT 必填；
API_KEY、EMBED_ENDPOINT、EMBED_MODEL、EMBED_API_KEY 可选（未配置向量服务时检索自动退回 BM25）。

退出码：0 全部处理完成（允许单条失败）；1 任务级运行错误；2 配置缺失或输入不符合协议。
配置缺失时不会空手返回：仍按保底草稿写出 output.json / results.json（run_log 标注 fallback）。
每完成一例即原子落盘一次，进程被外部时限中断也留得下可用结果。
"""
import argparse
import hashlib
import logging
import random
import sys
import time
import traceback
from pathlib import Path

from src import config
from src.case_io import InputError, read_cases, write_json_atomic, write_results_alias
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

    def flush(partial):
        """增量落盘：每完成一例就原子重写一次，进程被外部时限掐掉也留得下可用结果。"""
        if not config.INCREMENTAL_WRITE:
            return
        try:
            write_json_atomic(output_path, partial)
        except Exception as exc:
            log.error("增量落盘失败：%s", exc)

    try:
        run_log_path.unlink(missing_ok=True)  # 避免残留旧日志与本次输出混淆
        input_hash = hashlib.sha256(input_path.read_bytes()).hexdigest() if input_path.is_file() else None
        cases, notes = read_cases(input_path)
        if notes:
            run_log["input_notes"] = notes
            for n in notes:
                log.info("输入适配：%s", n)
        prompts = PromptSet(config.PROMPT_DIR)
        run_log["prompt_version"] = {"version": prompts.version, "files": prompts.hashes}

        # 模型配置缺失时仍按保底草稿产出结果（退出码保持 2），避免整场空手
        rt, client, retriever = None, None, None
        try:
            rt = load_runtime_config(config.ENV_FILE)
            run_log.update(model_name=rt["model_name"], endpoint=rt["endpoint"])
        except ConfigError as exc:
            log.error("%s；本次只输出保底草稿", exc)
            run_log["error"] = {"type": type(exc).__name__, "message": str(exc)}
            exit_code = 2

        use_rag = args.rag if args.rag is not None else (config.USE_RAG or args.mode == "improved+rag")
        if args.mode in ("baseline", "extract"):
            use_rag = False
        fallback = False if args.no_fallback else config.ENABLE_FALLBACK
        if rt is not None and cases:
            retriever, embedder = _build_retriever(rt, use_rag, run_log)
        run_log["options"] = {"use_rag": bool(use_rag), "fallback": fallback,
                              "n_candidates": args.candidates or config.N_CANDIDATES}
        log.info("模型 %s，病例 %d 例，模式 %s，检索 %s", (rt or {}).get("model_name") or "（未配置）",
                 len(cases), args.mode, run_log.get("retrieval", {}).get("method", "off"))

        if rt is not None:
            client = LLMClient(rt["model_name"], rt["endpoint"], config.GEN_PARAMS, config.REQUEST_TIMEOUT_S,
                               config.MAX_RETRIES, config.RETRY_BACKOFF_S, api_key=rt.get("api_key"))
        pipeline = Pipeline(client, prompts, args.mode, retriever=retriever, embedder=embedder,
                            use_rag=use_rag, n_candidates=args.candidates, fallback=fallback)
        deadline = time.monotonic() + config.TOTAL_BUDGET_S if config.TOTAL_BUDGET_S else None
        results, case_logs = run_cases(cases, pipeline, config.CONCURRENCY, on_result=flush, deadline=deadline)
        for lg in case_logs:
            log.info("%s -> %s%s（调用 %d 次，%.1fs）", lg["case_id"], lg["status"],
                     "（保底）" if lg.get("fallback") else "", lg["model_calls"], lg["duration_s"])
        write_json_atomic(output_path, results)
        if config.WRITE_RESULTS_ALIAS:
            try:
                alias = write_results_alias(output_path, results)
                if alias is not None:
                    run_log["results_alias"] = str(alias)
            except Exception as exc:
                log.error("results.json 写入失败（不影响 output.json）：%s", exc)
        if client is not None and getattr(client, "stripped_params", None):
            run_log["stripped_params"] = client.stripped_params
            log.warning("接口拒绝了这些参数，已自动摘除后重试：%s", client.stripped_params)
        run_log["input_sha256"] = input_hash
        run_log["output_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
        run_log["cases"] = case_logs
        run_log["summary"] = summarize(results, case_logs)
        if exit_code != 2:
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
            # 结果文件已经写好，日志写不进去不应把整体退出码改成失败
            log.error("run_log.json 写入失败（不影响结果文件）：%s", exc)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
