# 临床智录 —— 住院首次病程记录智能生成

读取入院资料（input.json），调用赛事内网 Qwen（可选 BGE-M3 检索），生成包含"病例特点、拟诊讨论、诊疗计划"三段正文、可溯源依据（source_id）和待核事项的首程草稿，写出 output.json 与 run_log.json。产出仅用于比赛评审，不用于真实诊疗。

## 1. 算法思路

流水线：输入适配 → 事实与问题清单抽取 → （可选）知识卡片检索 → 首程生成 → 确定性修正 → 规则校验 → 定向修复 → 保底。

1. 输入适配（src/adapter.py）：接受标准数组 `{case_id, record_cutoff, sources[]}`，也兼容单个对象、嵌套数组、容器对象和练习集"入院记录"结构（自动拆成带 source_id 的资料片段，去除姓名与住址）；可修复首尾多/少括号等常见 JSON 格式问题，修复情况写入 run_log.input_notes。
2. 事实与问题清单抽取（prompts/system_extract.txt）：模型逐字保留数值与否定词，归类阳性、阴性、时序、缺失、冲突、初步判断，并给出临床问题清单 `problem_list[{problem, key_findings, source_ids, must_not_miss}]`。
3. 规则提示（src/facts.py）：正则抽出带单位的数值原文和"未提供/不详"条目，作为硬约束写入生成提示。
4. 知识卡片检索（src/retrieval.py，可选）：按主诉、初步诊断与问题清单检索团队自编鉴别卡片 top-3，以"鉴别参考（非本病例事实）"注入提示；BGE-M3 不可用时自动退回本地字符二元组 BM25。
5. 首程生成（prompts/system_generate.txt + common_rules.txt）：按问题清单组织拟诊讨论，每个问题 2—3 个鉴别方向并覆盖 must_not_miss；计划与问题一一对应，用"拟/建议/复查"；初步判断不写成确诊；evidence 3—5 条且含 fact 与 inference。
6. 确定性修正与规则校验（src/validate.py）：移除无效 source_id、截断多余 evidence、补齐资料中明确缺失的待核项；校验 Schema 与三段非空、数值与时间回溯原文、计划中资料外的具体剂量、"已予/已完成"等已实施表述、过度确诊、缺失写成正常、evidence 条数与 inference 存在；evidence 与所引资料语义一致性（BGE-M3 余弦，退回词法）仅记录告警。
7. 定向修复（prompts/system_repair.txt）：只把 error 级问题逐条反馈修正，按问题评分择优保留，避免越修越差。
8. 调用预算降级：每例预算 ≥4 次走全流程，=3 去修复，=2 去抽取，=1 单次生成。可选多候选（--candidates N）按规则评分择优。
9. 保底（src/fallback.py）：模型不可用或输出始终不合规时，生成只摘录原文、复述初步判断、给出原则性计划的确定性草稿，保证不缺段、不编造；run_log 标注 fallback=true。可用 --no-fallback 关闭。

## 2. 运行说明

```bash
python -m pip install -r requirements.txt     # 仅标准库，无第三方依赖
python main.py --input input.json --output output.json
```

- 路径均为命令行参数或相对程序目录，无硬编码；全程无人工交互。也兼容旧入口 `python main.py`（读写程序目录下 input.json / output.json）。
- 模型配置：环境变量优先，其次程序目录下 .env（模板见 .env.example）：

| 变量 | 必填 | 说明 |
|---|---|---|
| MODEL_NAME | 是 | 赛事 Qwen 模型名 |
| ENDPOINT | 是 | chat/completions 完整地址，程序不拼接路径 |
| API_KEY | 否 | 有则发送 `Authorization: Bearer`，无则不发 |
| EMBED_ENDPOINT / EMBED_MODEL / EMBED_API_KEY | 否 | BGE-M3 embeddings 地址、模型名（默认 bge-m3）、密钥 |

- 可选参数：`--mode baseline|extract|improved|improved+rag`（默认 improved）、`--rag / --no-rag`、`--candidates N`、`--no-fallback`。
- 输出：output.json（先写临时文件再整体替换；空输入写 []）；run_log.json 写在 output 同目录；同目录再写一份内容相同的 results.json（赛事预检两种命名都认，可用 config.WRITE_RESULTS_ALIAS=False 关闭）。
- 增量落盘：每处理完一例就原子重写一次 output.json（未完成的病例以 status=failed / error.code=NOT_PROCESSED 占位），进程被外部时限中断时仍留有可用结果，不会整份为零；可用 config.INCREMENTAL_WRITE=False 关闭。
- 退出码：0 全部处理完成（允许单条 failed）；1 任务级运行错误；2 配置缺失或输入不符合协议（无法解析、case_id 缺失或重复）。配置缺失时仍按保底草稿写出结果（run_log 标注 fallback 与 ConfigError），退出码保持 2。

| error.code | 含义 |
|---|---|
| INVALID_CASE | record_cutoff 或 sources 无效 |
| MODEL_TIMEOUT / MODEL_UNAVAILABLE / MODEL_HTTP_ERROR / MODEL_BAD_RESPONSE | 模型请求失败且已达重试上限（开启保底时改为输出保底草稿） |
| CALL_BUDGET_EXCEEDED | 超出每例调用上限 |
| MODEL_OUTPUT_TRUNCATED | 模型达到生成长度上限，本次正文不完整（不重试） |
| OUTPUT_PARSE_ERROR / OUTPUT_SCHEMA_ERROR | 模型输出无法解析或修复后仍不合规 |
| OUTPUT_FACT_ERROR | 修复后仍有数值来源错误或把缺失写成正常 |

开启保底时，除 INVALID_CASE 外的上述失败都改为输出保底草稿（status 为 success，run_log 标注 fallback 与原因）。
| INTERNAL_ERROR | 其他单例异常 |

## 3. 依赖环境

- Python 3（开发与测试环境 3.13），仅用标准库（urllib、json、re、argparse、concurrent.futures），无第三方包。
- 网络：只访问赛事内网 Qwen 与 BGE-M3 接口，不读取系统代理，不访问公网。
- 主要参数（src/config.py）：每例调用预算 6、修复 ≤1 轮、候选数 1、并发 1、请求超时 60 s、重试 1 次（另有全局总时限 TOTAL_BUDGET_S=900 s，到点未完成的病例直接走保底）。赛方公布限额后按需调整。

## 4. 知识资源

登记见 resource_manifest.json（由 tools/build_manifest.py 生成，含条数与 SHA-256）。

- resources/kb/cards.jsonl：团队自编鉴别诊断卡片，每行一张（id、topic、triggers、differentials[{name, support, against, must_check}]、red_flags、plan_hints、reference），依据规划教材与公开指南要点整理，不含真实患者信息、具体数值与药物剂量。
- 卡片只作为"鉴别参考"进入提示，不作为本病例事实；输出中的数值、时间仍必须回溯到本病例资料。
- 默认开启（config.USE_RAG=True）。39 张卡片已于 2026-11-23 经团队审核确认，记录见 resources/kb/REVIEW.md；运行时加 --no-rag 可关闭，用于消融对比。

## 5. 随机种子与可复现性

- 固定 random_seed=42；生成参数 temperature=0、top_p=1、seed=42、max_tokens=4000（留足思考 token 后仍有正文预算），并默认关闭 Qwen 思考：官方 vLLM 用 `chat_template_kwargs: {"enable_thinking": false}`（src/config.py）。
- 思考开关的发送形式可用环境变量 THINKING_OFF_STYLE 切换：chat_template（默认，官方内网）/ reasoning（部分第三方网关）/ none（不发任何字段）。
- 接口兼容兜底：若接口以 400 拒绝未知字段（vLLM 的 extra="forbid"），程序自动摘掉被点名的可选参数（chat_template_kwargs、reasoning、top_p、seed、response_format）并立即重试一次，实际摘掉的字段写入 run_log.stripped_params 便于复核；model/messages/temperature/max_tokens 永不摘除。
- 多候选时第 n 个候选用 temperature=0.7、seed=42+n。正式运行按赛方接口确认参数。
- run_log.json 记录：run_id、起止时间、退出码、模型与地址、生成参数与限制、提示词版本及文件哈希、源码指纹（main.py + src + prompts + resources 的 SHA-256）、检索方式与命中卡片；每例的调用用途、尝试次数、耗时、HTTP 状态、请求/响应 SHA-256、token 用量、校验问题与修正、是否保底。日志不含密钥。
- 日志记录每次调用的结束原因（finish_reason）和输入输出文件的 SHA-256。
- 核对方式：重跑后比较 code_fingerprint 与每例 response_sha256。

## 改进验证

```bash
python tools/run_experiment.py --modes baseline,extract,improved,improved+rag [--judge]
```

默认在官方练习集转换文件 data/official_practice_cases.json（8 例）上，同一模型与参数运行各模式，输出 experiments/<时间>/report.md、metrics.json 和人工评分表。指标：成功率、保底率、无规则错误例数、各类规则问题数（含资料外剂量、evidence 对齐告警）、must_not_miss 覆盖率、平均字数、调用次数与耗时。--judge 用同一模型按五个维度打分，仅作预筛，最终以人工评分为准。

## 局限

- 规则校验只拦截可模式化的错误（数值、时序、措辞、剂量），不能判断鉴别是否合理，依赖提示词与人工评审。
- 数值回溯按"数值存在于资料中"判断，挂错检查项时无法发现。
- 保底草稿只保证合规与不编造，临床分析深度有限。

## 开发与自测（tools/、tests/、data/ 不进提交包）

```bash
python -m unittest discover -s tests -v              # 单元 + 端到端（本地模拟模型）
python tools/mock_server.py --port 18080 --faulty    # 模拟模型；--faulty 注入典型错误
python tools/check_output.py                         # 核对 output.json 协议
python tools/pack.py --team <团队名称> --version v1   # 生成 dist/<团队名称>_v1.zip 并在包内自测
```

开发期接入真实 Qwen：本地 Ollama 写 ENDPOINT=http://127.0.0.1:11434/v1/chat/completions、MODEL_NAME=qwen2.5:7b；阿里云百炼可直接设置 API_KEY，或用 tools/dev_proxy.py 在本机补鉴权头。

## 目录

```
main.py                  统一入口
requirements.txt         依赖（仅标准库；requires.txt 为旧入口兼容副本）
resource_manifest.json   知识资源登记
.env.example             配置模板
src/                     config | envfile | case_io | adapter 输入适配 | llm_client | facts | parsing
                         validate | prompts | retrieval 检索与对齐 | fallback 保底 | pipeline | runner
prompts/                 提示词（版本见 manifest.json）
resources/kb/            知识卡片与审核记录
```

演示网页（答辩用，不进提交包）：`python -m venv demo/.venv`，安装 demo/requirements.txt 后运行 demo/app.py，只监听 127.0.0.1、无登录，不要开放到局域网。
