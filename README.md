# 临床智录 —— 住院首次病程记录智能生成

读取入院资料（input.json），调用主办方指定的 Qwen 模型，生成包含"病例特点、拟诊讨论、诊疗计划"三段正文、可溯源依据（source_id）和待核事项的首程草稿，写出 output.json 与 run_log.json。产出仅用于教学比赛评审，不用于真实诊疗。

## 运行

```bash
cd <源码解压目录>
python -m pip install -r requires.txt   # 仅用标准库，无第三方依赖
python main.py                          # 无参数；读取 .env 与 input.json
```

- 配置：根目录 .env，仅含 MODEL_NAME 与 ENDPOINT（模板见 .env.example）。ENDPOINT 为完整请求地址，程序不拼接路径、不发送 Authorization 头、不读取系统代理。
- 输出：根目录 output.json（先写临时文件再整体替换；空输入写 []）与 run_log.json。
- 退出码：0 全部处理完成（允许单条 failed）；1 任务级运行错误；2 配置缺失或输入不符合协议（非数组、case_id 缺失或重复、无法解析）。
- 单条病例资料无效、模型请求失败或结果无法解析时，该条记为 failed（error.code 见下表），继续处理后续病例。

| error.code | 含义 |
|---|---|
| INVALID_CASE | record_cutoff 或 sources 无效 |
| MODEL_TIMEOUT / MODEL_UNAVAILABLE / MODEL_HTTP_ERROR / MODEL_BAD_RESPONSE | 模型请求失败，已达到重试上限 |
| CALL_BUDGET_EXCEEDED | 超出每例调用上限 |
| MODEL_OUTPUT_TRUNCATED | 模型达到生成长度上限，本次正文不完整 |
| OUTPUT_PARSE_ERROR / OUTPUT_SCHEMA_ERROR | 模型输出无法解析，或修复后仍缺正文/结构不合规 |
| OUTPUT_FACT_ERROR | 改进版修复后仍有数值来源错误或把缺失写成正常 |
| INTERNAL_ERROR | 其他单例异常 |

## 方法：提取 → 生成 → 校验 → 修复

每例最多 3 次有效调用（抽取 1、生成 1、修复 ≤1），另留重试余量，上限见 src/config.py。

1. 事实抽取（prompts/system_extract.txt）：模型把资料归类为阳性、阴性、时序、缺失、冲突、初步判断、待解决问题，逐字保留数值和否定词。抽取失败时降级为仅用规则提示，不中断。
2. 规则提示（src/facts.py）：用正则从原文抽出带单位的数值原文和"未提供/不详"条目，作为硬约束写入生成提示：数值只能原样引用；缺失项只能写"未提供/不详"。
3. 首程生成（prompts/system_generate.txt）：共享书写规则（prompts/common_rules.txt）对应评分细则——数值与时序一字不差、否定与缺失原样保留、计划用"拟/建议/复查"、初步判断不写成确诊、鉴别 2—3 个方向、evidence 3—5 条且区分 fact/inference。
4. 确定性修正（src/validate.py normalize_record）：移除不存在的 source_id、截断超过 5 条的 evidence、规范 kind、把资料中明确缺失但未列出的项目补入 review_items。不改动医学正文。
5. 规则校验（src/validate.py audit_record）：Schema 与三段非空；source_id 属于本病例；数值回溯（输出中带单位的数值必须在资料中出现）；病程时间回溯；计划中的"已予/已完成"等已实施表述；"确诊/诊断明确"等过度结论；"未提供"被改写成"正常/否认"。
6. 定向修复（prompts/system_repair.txt）：只把 error 级问题逐条反馈给模型修正，按问题评分择优保留，避免修复变差。

## 可复现性

- 固定生成参数 temperature=0、top_p=1、seed=42、max_tokens=3000，开发验证时关闭 Qwen 推理以给 JSON 留足生成预算（src/config.py）。正式运行按赛方接口确认参数。若接口拒绝参数，记录失败并保留配置以便复核。
- run_log.json 记录：run_id、起止时间、退出码、模型与地址、生成参数与限制、提示词版本及各文件哈希、源码指纹（main.py + src + prompts 的 SHA-256）；每例的调用序号、用途、尝试次数、时间戳、耗时、HTTP 状态、请求/响应 SHA-256、token 用量、各步校验问题与修正。日志不含任何密钥。
- 核对方式：重跑后比较 code_fingerprint 与每例 response_sha256；温度为 0 时差异主要来自服务端随机性或版本变化。
- 日志记录结束原因和输入输出 SHA-256。打包工具核对当前输入、输出、源码及逐例日志，旧版本日志需要随当前源码重新运行生成。

## 改进验证

```bash
python tools/run_experiment.py --modes baseline,extract,improved
```

在同一批练习病例（data/practice_cases.json）、同一模型和参数下运行三个版本，输出 experiments/<时间>/report.md、metrics.json 和人工评分表.csv：

| 模式 | 内容 |
|---|---|
| baseline | 单次调用，系统提示取自赛题技术规范示例 |
| extract | + 事实抽取 + 数值/缺失项规则提示 + 结构修正 |
| improved | + 规则校验 + 定向修复（提交版本） |

自动指标：成功率、无规则错误例数、各类问题数（数值不在原文、时间不在原文、计划写成已实施、过度确诊、缺失写成正常、无效 source_id）、evidence 合规率、平均字数、平均调用次数与耗时。临床分析质量由两名队员按附件 2 的五个维度填写人工评分表，评分时隐藏 mode 列。

## 局限

- 规则校验只能拦截可模式化的错误（数值、时序、措辞），不能判断鉴别诊断是否合理；该部分依赖提示词与人工评审。
- 数值回溯按"数值存在于资料中"判断，同一数值被挂到错误的检查项上时无法发现。
- 改进版每例约 2—3 次调用，耗时约为基线的 2—3 倍；主办方调用预算更紧时，可在 src/config.py 中关闭修复（MAX_REPAIR_ROUNDS=0）或改用 extract 模式。
- 未使用检索增强（RAG）：赛题要求检索资料须为赛方提供或登记，目前尚无可用资料。

## 开发与自测

```bash
python -m unittest discover -s tests -v         # 规则单测 + 端到端（本地模拟模型）
python tools/mock_server.py --port 18080 --faulty  # 模拟模型；--faulty 注入典型错误
python tools/check_output.py                    # 提交前核对 output.json 协议
python tools/pack.py --team <队伍编号>           # 生成 <队伍编号>_algorithm.zip / _results.zip
```

开发期接入真实 Qwen：

- 本地 Ollama（无密钥，可直接用）：.env 写 ENDPOINT=http://127.0.0.1:11434/v1/chat/completions，MODEL_NAME=qwen2.5:7b。
- 阿里云百炼（需要密钥）：先运行 python tools/dev_proxy.py --upstream https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions（密钥放在环境变量 DASHSCOPE_API_KEY 中），.env 写 ENDPOINT=http://127.0.0.1:18081/v1/chat/completions。代理只在本机补鉴权头，主程序始终无密钥。

正式评测前，按主办方公布的生成参数、调用预算、并发和超时修改 src/config.py，重跑测试后再打包。

## 演示网页（答辩用，不进提交包）

```bash
python -m venv demo/.venv
demo/.venv/Scripts/python -m pip install -r demo/requirements.txt   # gradio==5.50.0
demo/.venv/Scripts/python demo/app.py                              # 打开 http://127.0.0.1:7860
```

- 生成演示：选示例病例或粘贴资料（每行「S01｜内容」），选模式后生成；左侧资料卡片标出被哪条依据引用，右侧显示三段正文、待核事项、evidence 表和规则复检结果。
- 基线 vs 改进：同一病例并排运行 baseline 与 improved。
- 模型地址默认读 .env，也可在页面上改；没有真实模型时可先启动 tools/mock_server.py。
- 只监听 127.0.0.1，无登录，不要开放到局域网或公网。tools/pack.py 不打包 demo/，提交包仍只依赖标准库。

## 目录

```
main.py              统一启动入口
requires.txt         依赖清单（仅标准库）
.env.example         配置模板
src/                 config 参数 | envfile 配置 | case_io 输入输出 | llm_client 模型调用
                     facts 规则抽取 | parsing JSON 解析 | validate 校验 | prompts 提示词组装
                     pipeline 单例流水线 | runner 批量与日志
prompts/             提示词（版本见 manifest.json）
data/                练习病例（DEMO001 来自赛题附件 1，其余为团队自编教学病例）
tools/               实验、自检、打包、模拟模型、开发代理（正式运行不依赖）
tests/               单元与端到端测试
```
