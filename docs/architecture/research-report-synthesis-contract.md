# 3H-1：受证据约束的报告综合与终态契约

基线：`6497366`（3G Claim Verification）；本阶段为 3H 的**受约束、可审计首版**。它引入真实模型调用驱动的章节编排和重点选择；**不允许模型直接撰写不受约束的事实性自然语言段落**。这不是对开放式长篇 LLM Report Writer 的质量验收。

## 1. 为什么不能直接把 LLM Markdown 写到 7C 报告表

7C 的终态协议不相信 Writer 的返回值：在 `WRITING` Journal 完成及 Run 进入 `COMPLETED` 之前，PostgreSQL 在同一事务、持有 Run 锁的情况下读取真实 Claim/Evidence 并重新计算报告，比较报告正文、引用和 SHA-256。任由 LLM 输出完整 Markdown，既会打破重算保证，也可能引入并不存在的 Evidence ID、不被 3G 支持的结论或忽略冲突。

本阶段选择**模型负责组织，系统负责事实**：

```text
3G VERIFIED Claim + Evidence Manifest + Generation / Verification Receipts
      │
      ├─ WRITING Lease 与 Journal STARTED
      ├─ 事务 A：锁 Run，读取事实快照，计算 input_sha256
      ▼
OpenAI-compatible LLM（事务外，仅返回结构化章节计划）
      │  highlights: [Claim ID]; sections: [{固定章节名, Claim IDs}]
      ▼
Python 检查：每个本 Run SUPPORTED Claim 恰好出现一次；无陌生 ID/无伪造标题
      │
      ├─ 事务 B：重新锁 Run/Journal，重验 Lease/Fencing 与所有 3G 凭据
      ├─ 重算事实快照，逐字段及 input_sha256 比较
      ├─ 从原始 Claim 文本及 Evidence 原文确定性生成 Markdown
      └─ 同事务创建：Run Report Artifact + Synthesis Receipt
      │
      ├─ WRITING Journal COMPLETED：同事务验证报告与综合凭据
      └─ ResearchRun COMPLETED：同事务再次验证报告/引用/Claim/Receipt
```

**任何模型请求都不持有数据库 Run 锁。** 模型请求中执行权过期、撤销或基础数据变更时，第二次事务失败，不会残留部分报告或综合凭据。

## 2. 模型输出契约

模型只能返回 JSON：

```json
{
  "highlights": ["supported-claim-uuid"],
  "sections": [
    {"heading": "Key findings", "claim_ids": ["supported-claim-uuid"]},
    {"heading": "Related findings", "claim_ids": ["another-supported-claim-uuid"]}
  ]
}
```

约束：

- `highlights` 最多三条，只能引用当前 Run 的已验证 SUPPORTED Claim；允许空列表。
- 一至四个非空章节，标题必须取自固定集合：`Key findings`、`Related findings`、`Additional findings`、`Other verified findings`，标题不能承载模型发明的事实。
- 每条 SUPPORTED Claim 恰好出现在一个章节内；多一条、少一条、重复、未知、其他 Run 的 ID 都失败。
- 章节排序与重点选择来自模型；模型不能改写 Claim 文本、Evidence 原文、Source URL、引文位置或结论状态。
- `UNSUPPORTED` 和 `CONFLICTED` 的 Claim 永远不能进入被引用的 SUPPORTED 事实清单。只要 3G Receipt 存在相应判定，渲染器**强制附加**“Conflicts and evidence limitations”，包含候选文本、审核理由和现存精确支持/反驳引文的来源。模型不能删除、隐藏或提升这些结论。
- Markdown 中原始文本转义，链接只能来自数据库业务表中的 HTTP(S) URL，而非 LLM 输出；URL 中 Markdown 结构字符编码处理。

文档保留英文固定节名，是为了防止模型根据自然语言自由生成、却在标题中引入未受证据支持的主张。后续可扩展经审核的标题模板。

## 3. 持久化模型与 Alembic

新表 `research_run_report_syntheses`：

| 字段 | 含义 |
|---|---|
| `run_id` | PK，FK 指向 `research_run_report_artifacts.run_id`，级联删除 |
| `task_id` | 所属 Task FK |
| `fencing_token` | 与 Writing Lease 同代 |
| `model` | 模型标识，参与 Receipt 摘要 |
| `input_sha256` | Run 计划目标、所有参与的 SUPPORTED Claim 文字、证据身份/原文/定位/哈希，及所有不利判定/原文引文的摘要 |
| `plan` | JSONB 结构化章节方案 |
| `receipt_sha256` | SHA256（模型标识 + 输入摘要 + canonical plan） |
| `created_at` | 持久化时间 |

新迁移：`c8d4a762e901_add_run_report_synthesis_receipts.py`，父版本：`f3a7c9d25168`，单一新 Head `c8d4a762e901`。

写入由 `PostgresResearchRunReportRepository.write_synthesized_guarded` 完成，**两个 Row 在同一个 PostgreSQL 事务中插入**；存在既有 Report 或 Lease 不合法时都不能写入。通过 Fencing 的新执行代次也不能覆写旧报告。

旧 `ENTERPRISE_RESEARCH_WRITER_BACKEND=verified-claims` 的确定性写作继续可用；其报告没有 synthesis row，按 7C 原路径验证，兼容旧报告和旧 API。

## 4. 完成证明与威胁模型

- `ResearchReportSynthesisService.write_run` 先读取并固定输入，在事务外调用模型，再执行 guarded 写入。
- PostgreSQL `validate_run_terminal_report` 先检查 Writing Journal（终态转移场景），再检查报告所有权与代次。
- 若存在 synthesis row：校验 Task/Run/Fencing，重新读取 3G 的当前 Run 受支持 Claim 和不利判定，计算 `input_sha256`，解码并复核 `plan`、`receipt_sha256`，再次运行**同一确定性渲染器**并逐项比较标题/正文/引用/哈希。
- 如果无 synthesis row：完全保留 7C 原始确定性重算协议。
- **3H 比旧 7C 更严格**：要求当前 Run 实际拥有 3F 和 3G 两份 Receipt；即使有人手工把 Task 级 Claim 设置为 `SUPPORTED`，缺少本 Run 3G Receipt 也不能进入模型综合流程。此规则在输入读取、模型后提交、Writing/Terminal 完成证明三个位置均强制执行。
- 如果 LLM 调用期间 Claim/Evidence 变化或 Lease 被撤销：第二次提交必须失败、无部分报告，Stage Journal 仍保持未完成。Executor 正常捕获异常并按既有 FAILED 路径处理。
- 如果创建报告后改写模型标识、章节方案、正文或验证依据：Writing Journal/Run 的事务门禁拒绝完成。

**边界：** `SHA-256` 是一致性校验，不是防备拥有数据库超级用户权限的恶意修改者的数字签名；`SUPPORTED` 仍代表 3G 语义模型判定而非绝对真理；现有 `GET /runs/{run_id}/report` 读取时主要做存储摘要一致性检查，不代表持续重审所有外部来源。外部网页内容可能在完成后变化，3H 不负责外部时效性追踪。

## 5. 启动配置

3H 新 Writer 显式启用，**必须**配合 PostgreSQL、真实 Planner、Evidence Extractor、3F Generator 和 3G Verifier。插件会拒绝在没有 3G 的情况下启用模型综合写作。

```bash
export ENTERPRISE_RESEARCH_REPOSITORY=postgres
export ENTERPRISE_RESEARCH_PLANNER_BACKEND=openai-compatible
export ENTERPRISE_RESEARCH_CLAIM_BACKEND=openai-compatible
export ENTERPRISE_RESEARCH_CLAIM_VERIFICATION_BACKEND=openai-compatible
export ENTERPRISE_RESEARCH_WRITER_BACKEND=openai-compatible
export ENTERPRISE_RESEARCH_SYNTHESIS_BASE_URL='https://<provider>/v1'
export ENTERPRISE_RESEARCH_SYNTHESIS_MODEL='<model-id>'
# 通过本地私密环境注入 ENTERPRISE_RESEARCH_SYNTHESIS_API_KEY，勿在命令日志分享密钥。
export ENTERPRISE_RESEARCH_SYNTHESIS_TIMEOUT_SECONDS=60
export ENTERPRISE_RESEARCH_SYNTHESIS_TEMPERATURE=0
```

其余 Planner、搜索、检索、证据模型和 3F/3G 配置沿用前阶段。未启用模型综合时默认仍是 `disabled`；不会静默退化成 Noop 或使用 Task 级旧报告。

`GET /api/enterprise-research/tasks/{task_id}/runs/{run_id}/report` 在原响应之外增加可空的 `synthesis_model`、`synthesis_sha256`，便于审计报告来源；Run 未 `COMPLETED` 仍为 HTTP 409。

## 6. 主要文件

- `application/report_synthesis.py`：严格的模型计划 DTO、覆盖校验、输入/收据摘要、模型调用与 guarded 提交编排。
- `infrastructure/report_synthesis/openai_compatible.py`：JSON-only 的 OpenAI-compatible 适配器，严格 Pydantic extra=forbid，无自由事实性生成。
- `application/report_writing.py`：确定性章节报告渲染、必需的不利证据披露、引用与摘要。
- `infrastructure/postgres/report_artifacts.py`：双次快照读取、事务外 LLM、事务内报告/凭据原子写入、完成/终态重算。
- `infrastructure/postgres/models.py`、新 Alembic 迁移：Run 级 synthesis receipt。
- `plugin.py`、`application/pipeline.py`：显式 Writer 后端和 Fail Closed 分发。
- `api/schemas.py`：报告返回综合模型及凭据指纹。
- `tests/application/test_report_synthesis.py`、`tests/infrastructure/test_openai_compatible_report_synthesis.py`：结构化验证、严格模型协议、稳定渲染、恶意标题/未知 Claim、转义及模型出错。
- `tests/infrastructure/test_postgres_report_synthesis.py`：真实 PostgreSQL 事务/Fencing/故障窗口/篡改测试。

## 7. WSL2 验收顺序（**不要提前 Commit**）

前置：`HEAD=6497366`、`git status --short` 为空，文件已下载至 `D:\Downloads`。

```bash
cd ~/projects/enterprise-research-platform
PATCH=/mnt/d/Downloads/3H-report-synthesis.patch
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
mapfile -t PY_FILES < <(
  { git diff --name-only -- '*.py'; git ls-files --others --exclude-standard | grep '\.py$'; } | sort -u
)
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"
uv run --no-project pytest tests/application/test_report_synthesis.py \
  tests/infrastructure/test_openai_compatible_report_synthesis.py \
  tests/test_plugin_report_synthesis_runtime.py -q
uv run --no-project pytest -q
```

如果仅是 Ruff import 排序/格式问题，可以对**本次改动的 Python 文件**运行 `ruff check --fix` 与 `ruff format` 后重验。先备份本地 `enterprise_research` 开发数据库，确认 DB 环境变量和 host 指向 `127.0.0.1`/`localhost`，再执行：

```bash
uv run --no-project alembic current                  # f3a7c9d25168
uv run --no-project alembic upgrade head
uv run --no-project alembic current                  # c8d4a762e901 (head)
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_SYNTHESIS_TESTS=1 \
uv run --no-project pytest tests/infrastructure/test_postgres_report_synthesis.py -v -rs
```

应收集 **8 个真实 PostgreSQL 用例全 PASS、0 skipped**；它们包括合法模型综合报告进入终态、模型调用期间改动 Claim、撤销 Lease、模型/计划/正文篡改导致 Journal 回滚，以及对不支持候选的强制披露、缺少 3G Receipt 时拒绝历史/人工 SUPPORTED Claim 冒充本 Run 完成结果。

随后建议回归 3G、7C、7B：

```bash
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_VERIFY_CLAIMS_TESTS=1 \
ENTERPRISE_RESEARCH_TERMINAL_PROOF_TESTS=1 \
ENTERPRISE_RESEARCH_STAGE_PROOF_TESTS=1 \
uv run --no-project pytest \
  tests/infrastructure/test_postgres_claim_verification.py \
  tests/infrastructure/test_postgres_terminal_report_contract.py \
  tests/infrastructure/test_postgres_stage_artifact_completion.py -v -rs
```

验收全部成功、核对暂存文件后再提交；不要把隔离环境中的 skipped 误报为真实数据库通过。

## 8. 后续的 3H-2 / 3I

3H-1 模型控制的是**章节排序、Claim 聚类及摘要重点**，最终正文中的事实陈述使用原始已验证 Claim；这是为安全完成契约刻意设置的约束。**它不是可自由改写的、多段自然语言 LLM 深度报告**。后续若要开放模型生成分析性文字，应建立独立的逐句 Claim entailment 复核、跨证据冲突审查和质量基准，不能把 LLM 自评或“出现引用标记”视为可信保证。

3I 需要真实 LLM/provider + Tavily/Extractor + 多轮故障注入的端到端评测，覆盖真实生成质量与成本/延迟，当前环境的 mock 测试不能替代。
