"""临床智录 —— 住院首次病程记录生成：统一启动入口。

用法：python main.py（无参数）。读取同目录 .env 与 input.json，写出 output.json 与 run_log.json。
退出码：0 全部处理完成（允许单条失败）；1 任务级运行错误；2 配置缺失或输入不符合协议。
"""
import hashlib
import logging
import sys
import traceback

from src import config
from src.case_io import InputError, load_input, write_json_atomic
from src.envfile import ConfigError, load_model_config
from src.llm_client import LLMClient, now_iso
from src.pipeline import Pipeline
from src.prompts import PromptSet
from src.runner import new_run_log, run_cases, summarize

log = logging.getLogger("linchuang")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    run_log = new_run_log(config.PIPELINE_MODE)
    exit_code = 1
    try:
        config.RUN_LOG_FILE.unlink(missing_ok=True)
        model_name, endpoint = load_model_config(config.ENV_FILE)
        run_log.update(model_name=model_name, endpoint=endpoint)
        input_hash = hashlib.sha256(config.INPUT_FILE.read_bytes()).hexdigest() if config.INPUT_FILE.is_file() else None
        cases = load_input(config.INPUT_FILE)
        prompts = PromptSet(config.PROMPT_DIR)
        run_log["prompt_version"] = {"version": prompts.version, "files": prompts.hashes}
        log.info("模型 %s，病例 %d 例，模式 %s", model_name, len(cases), config.PIPELINE_MODE)

        client = LLMClient(model_name, endpoint, config.GEN_PARAMS, config.REQUEST_TIMEOUT_S,
                           config.MAX_RETRIES, config.RETRY_BACKOFF_S)
        pipeline = Pipeline(client, prompts, config.PIPELINE_MODE)
        results, case_logs = run_cases(cases, pipeline, config.CONCURRENCY)
        for lg in case_logs:
            log.info("%s -> %s（调用 %d 次，%.1fs）", lg["case_id"], lg["status"], lg["model_calls"], lg["duration_s"])
        write_json_atomic(config.OUTPUT_FILE, results)
        run_log["input_sha256"] = input_hash
        run_log["output_sha256"] = hashlib.sha256(config.OUTPUT_FILE.read_bytes()).hexdigest()
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
        run_log["finished_at"] = now_iso()
        run_log["exit_code"] = exit_code
        try:
            write_json_atomic(config.RUN_LOG_FILE, run_log)
        except Exception as exc:
            log.error("run_log.json 写入失败：%s", exc)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
