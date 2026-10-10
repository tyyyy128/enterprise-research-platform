# 3G — Claim Verification Contract / 研究主张验证契约

> 适用基线：`a295b37`（3F 已合并），新迁移 `f3a7c9d25168`，前序 `b6e9f4c0a21d`。本阶段为可单独验收的候选主张**证据支持性判断**，不是外部事实数据库认证、人工审稿或真实 GLM 质量评测。

## 1. 为什么 3F 不足以让报告完成

3F 的 OpenAI-compatible 模型只能提出候选文本和已有 Evidence ID；合法输出一律以 `ClaimStatus.UNSUPPORTED`、`confidence=0` 落库。3F 的引用完整性只保证 **ID 的存在、归属和字节指纹一致**，无法自动推导“证据内容实际上支持文本结论”。7C 的 Run Writer 要求 `SUPPORTED`，这一步必须建立独立验证调用、Run 级审核凭据和防越权状态变更。

新增的 3G 实现一个可审计的 **第二次模型调用**：输入包含 3F 候选全文、候选引用和选定的 Run Evidence 摘录；模型必须返回每条候选的 `supported / unsupported / conflicted`、置信度、简短理由、正向与反向**原文连续子串**。这是一种模型做出的语义判断，不保证模型一定正确；严肃生产使用仍需质量基准、人审或更强的交叉来源验证。

## 2. 运行链路与默认配置

```text
VERIFYING (STARTED, fenced lease)
  Evidence extraction → 7B Evidence Manifest
  → 3F Claim Generation (write-once; initial UNSUPPORTED)
  → 3G load verification context (Run lock + original 3F/evidence receipts)
  → independently configured LLM Claim verifier (outside SQL transaction)
  → strict structured verdict, per-Claim coverage, exact quote checks
  → reacquire Run lock + STARTED Journal + fencing
  → reload all original Claim/Evidence/Plan inputs and compare SHA
  → ONE SQL transaction: update Claim statuses + insert verifier receipt
  → 7B VERIFYING completion rechecks both 3F and 3G receipts
  → 7C Writer consumes only SUPPORTED candidates recorded by 3G receipt
  → 7C final Run report/terminal contract rechecks 3G receipt again
```

只读默认行为不变；`ENTERPRISE_RESEARCH_CLAIM_VERIFICATION_BACKEND=disabled` 为默认。显式启用 3G 必须同时启用 3F（`ENTERPRISE_RESEARCH_CLAIM_BACKEND=openai-compatible`），真实 PostgreSQL、Planner 与 Evidence Extraction 服务。内存后端或只启用 3G 而禁用 3F 时启动失败，不能静默 no-op。

**环境变量**（API Key 不可写入日志/测试产物）：

| 变量 | 含义、默认值 |
|---|---|
| `ENTERPRISE_RESEARCH_CLAIM_VERIFICATION_BACKEND` | `disabled`（默认）或 `openai-compatible` |
| `ENTERPRISE_RESEARCH_VERIFY_BASE_URL` | 验证模型 OpenAI-compatible `/v1` 基地址，启用时必填 |
| `ENTERPRISE_RESEARCH_VERIFY_API_KEY` | 独立验证模型 Key，启用时必填 |
| `ENTERPRISE_RESEARCH_VERIFY_MODEL` | 非空模型标识，启用时必填 |
| `ENTERPRISE_RESEARCH_VERIFY_TIMEOUT_SECONDS` | `60` |
| `ENTERPRISE_RESEARCH_VERIFY_MAX_CLAIMS` | `12`，不得小于 3F max_candidates |
| `ENTERPRISE_RESEARCH_VERIFY_MAX_EVIDENCE` | `12`，不得小于 3F max_evidence |
| `ENTERPRISE_RESEARCH_VERIFY_MAX_EXCERPT_CHARS` | `1800` |
| `ENTERPRISE_RESEARCH_VERIFY_MAX_QUOTE_CHARS` | `320` |
| `ENTERPRISE_RESEARCH_VERIFY_MAX_RATIONALE_CHARS` | `1000` |
| `ENTERPRISE_RESEARCH_VERIFY_MIN_SUPPORTED_CONFIDENCE` | `0.8`，范围 `(0,1]` |

启用例子（不要将真实 API Key 发到聊天记录）：

```bash
export ENTERPRISE_RESEARCH_REPOSITORY=postgres
export ENTERPRISE_RESEARCH_CLAIM_BACKEND=openai-compatible
export ENTERPRISE_RESEARCH_CLAIM_VERIFICATION_BACKEND=openai-compatible
export ENTERPRISE_RESEARCH_VERIFY_BASE_URL='https://example-llm-provider/v1'
export ENTERPRISE_RESEARCH_VERIFY_MODEL='configured-verifier-model'
# ENTERPRISE_RESEARCH_VERIFY_API_KEY 从安全的本地环境注入
```

## 3. 语义与机械验证的边界

对所有 3F 生成的候选逐一验证，**不允许遗漏、重复或额外的 Claim ID**。所有证据 ID 必须存在于 3F 选定的当前 Run Evidence 清单中。模型输出的引用原文必须为对应 Evidence 输入摘录的**连续、精确子串**，不是模型对来源的自由转述；限长、禁止重复引用。

| Verdict | 验证器准入规则 | 写回既有 Claim |
|---|---|---|
| `SUPPORTED` | 至少一个来自该 Claim 原始 3F 引用的支持引文；无冲突引文；finite confidence ≥ 0.8 | `status=supported`, `confidence=verdict.confidence` |
| `UNSUPPORTED` | 非空理由；不得声称存在支持引文；可有当前 Run 范围内的反证 | `status=unsupported`, `confidence=0` |
| `CONFLICTED` | 同时有至少一个真实支持引文和一个不同的真实冲突引文；反证可来自 3F 选定范围的其他 Evidence | `status=conflicted`, `confidence=0` |

非 `SUPPORTED` 状态的 `Claim.confidence=0` 表示**不授予作为可信结论使用的正向置信度**；模型对本次判定本身的置信度另存于 Receipt 的 verdict JSON。

判定质量注意：即使一段引用真实存在，它与 Claim 的语义关系仍由 LLM 给出；字符串检查只证明引用**来自正确快照**，无法数学证明蕴含关系。置信度阈值是可配置的保守门槛，不是准确率保证。对于与证据不完全对应的 Claim，模型应返回 `UNSUPPORTED`；有直接矛盾证据时返回 `CONFLICTED`。模型输出不合约则整个阶段失败，绝不默认 `SUPPORTED`。

## 4. 数据库表与不可变性

新表：`research_run_claim_verifications`，`run_id` 主键，外键引用 `research_runs` 与 `research_tasks`；**一 Run 一条**，绑定当前 `fencing_token`。

关键字段：

- `generation_input_sha256`, `generation_output_sha256`：绑定 3F 生成凭据。
- `input_sha256`：绑定 Task Query、Plan Objective/Questions、3F ID/文稿及引用、7B Manifest 文档/证据指纹、限长后的输入 Evidence 原文和 Fencing Token。
- `claim_ids` 与 `candidate_count`：覆盖率证明，不允许缺失候选。
- `verdicts`：每条 Claim 的 Status、置信度、理由、正反两组原文引文。
- `verdicts_sha256`：对有序 canonical JSON verdicts、模型标识与本次限额参数一并做 SHA-256，阻止只修改审计元数据却保持旧摘要。
- `limits`：保存生成本 Receipt 时的实际预算，供事后按**相同长度上限**重新验证。
- `verifier_model`、`created_at`：审计模型标识及创建时间。

限制：该 SHA-256 是**同库业务一致性指纹**，不是具密钥签名；具完全数据库管理员权限者可以同时改写全部业务表和校验字段，因此本阶段**不声称抵御特权 DBA 对所有字段的联合篡改**。如有防抵赖需求，应在后续引入外部不可变审计或密钥签名。

## 5. 事务、Fencing 与恢复语义

`ResearchClaimVerificationService.verify_run()` 分两个阶段：

1. Run 行锁下读取 3F Receipt、7B Manifest、Plan、Candidates 与 Evidence，计算输入指纹后释放事务；调用外部模型，不占用长事务。
2. 收到结果后，重新锁定 Run、验证 Task/Run/Lease Owner/Fencing Token/Lease TTL/`VERIFYING` 状态和 `STARTED` Journal；重新读取全部输入并比较，禁止其它变更。验证原文、决定状态，然后**同事务**更新 `research_claims.status/confidence` 并插入 Run Receipt。

旧 Worker 被撤销 Lease、原文被修改、出现重复写入或模型漏判时，整个事务回滚，既不留下部分 SUPPORTED Claim，也不留下伪造成功的 Receipt。由于 3F 候选及 3G 凭据均是 Run 级 write-once，失败后不能无条件盲重试；与 3E Safe Resume 的保守暂停行为兼容，后续必须设计独立的 VERIFYING 恢复对账规则才能扩大自动续跑范围。

7B `validate_persisted_stage_artifacts(VERIFYING)` 在原有 3F Receipt 后检查已存在的 3G Receipt，逐条比对当前 Claim 状态、置信度、证据摘录与 Receipt 的内容指纹。3G 缺省关闭时 7B 允许没有 3G Receipt（兼容历史数据/3F 单独运行），但**7C 加了最后一道门禁**：只要当前 Run 有 3F 生成凭据，必须有有效 3G 凭据，且仅允许属于该 Run、在 3G verdict 列表中标记 `SUPPORTED` 的 Claim 进入最终 Run Report。历史人工/其他 Run 的 SUPPORTED Claim 不得冒充本次自动结果。

## 6. 文件职责

| 文件 | 职责 |
|---|---|
| `application/claim_verification.py` | 中立 Protocol、限制、Claim/Evidence/Verdict 数据类型、机械判定准入、稳定哈希 |
| `infrastructure/claim_verification/openai_compatible.py` | 带有不可信输入告警的提示词、JSON 结构校验、HTTP 调用与异常边界 |
| `infrastructure/postgres/claim_verification.py` | PostgreSQL input snapshot、Fencing 保护的双阶段提交、持久化及事后回验 |
| `infrastructure/postgres/models.py` | 3G Receipt SQLAlchemy ORM 映射 |
| `migrations/versions/f3a7c9d25168_add_run_claim_verification_receipts.py` | 新表数据库迁移，down_revision 为 3F head |
| `application/pipeline.py` | VERIFYING 严格顺序：Evidence → 3F → 3G |
| `infrastructure/postgres/stage_completion.py` | 将 3G Receipt 检查插入 7B VERIFYING 原子完成证明 |
| `infrastructure/postgres/report_artifacts.py` | 7C Writer 仅消费本 Run 实际 3G 验证的 SUPPORTED 候选 |
| `plugin.py` | 显式启用、配置校验、依赖注入 |

## 7. WSL2 验收门槛（全部在 `~/projects/enterprise-research-platform`）

请确认提交基线 `a295b37` 且 `git status --short` 没有意外修改，然后应用 `3G-claim-verification.patch`。

```bash
git apply --check /mnt/d/Downloads/3G-claim-verification.patch
git apply /mnt/d/Downloads/3G-claim-verification.patch
git diff --check

mapfile -t PY_FILES < <(
  { git diff --name-only -- '*.py'; git ls-files --others --exclude-standard | grep '\.py$'; } | sort -u
)
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"
# 如果只有可自动修复的排序/格式问题，使用 ruff check --fix 和 ruff format 修复并重跑。

uv run --no-project pytest tests/application/test_claim_verification.py \
  tests/infrastructure/test_openai_compatible_claim_verifier.py \
  tests/test_plugin_claim_verification_runtime.py -q
uv run --no-project pytest -q
```

PG：确认环境变量指向**本地开发** `enterprise_research`，先备份，再核对旧版 `b6e9f4c0a21d`，执行：

```bash
uv run --no-project alembic current
uv run --no-project alembic upgrade head
uv run --no-project alembic current  # 预期 f3a7c9d25168 (head)

ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 ENTERPRISE_RESEARCH_VERIFY_CLAIMS_TESTS=1 \
  uv run --no-project pytest tests/infrastructure/test_postgres_claim_verification.py -v -rs
```

**目标**：6 passed、0 skipped。进一步复测 3F（5 条）、7B（3 条）与 7C（2 条）真库用例；检查 `git diff --check`、`git status` 与迁移单 Head。所有通过后方可提交 Git，例如 `feat: add fenced evidence-quoted claim verification`。

## 8. 暂未实现的能力（勿误称为已具备）

- **真实 GLM/API 端到端验收**：本地 HTTP Mock 测试不代表真实模型质量，需上线前做准确率/引用真实性/冲突识别评估。
- **完备自动恢复**：验证凭据存在不意味着 `VERIFYING` 崩溃后可以不经对账自动重试。
- **可信源权重与跨源事实判定**：3G 仅基于 3F 限定的 Evidence 选择集合。不能外推为全网事实检查。
- **更丰富的 3H 报告写作**：7C 仍为支持性 Claim 的确定性 Markdown 报告，不是完整大模型编辑型报告。
- **特权管理员级防篡改**：需额外的不可变审计或签名。

**验收目标**：证明 3F 候选经过独立模型判定与确定性的来源/事务完整性检查后，才可能变成当前 Run 的 `SUPPORTED` Claim；不能只根据候选自身有引用就自动升级状态。
