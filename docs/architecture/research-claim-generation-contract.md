# 3F — Claim Generation：Run 级候选主张与来源证明（首版）

> 基线：`e4ec100`（完成 3E-6D-7C 终态报告契约）；新增迁移：`b6e9f4c0a21d`。
>
> 重要语义：**候选 Claim ≠ 已核验 Claim；证据引用 ≠ 语义蕴含证明。**
> 本阶段不改变 7C 对最终报告的严格要求。正式验收必须在 WSL2 实际运行 Ruff、全量 pytest 与本地 PostgreSQL 测试。

## 1. 背景与设计范围

现有 `ResearchEvidenceExtractionService` 从文档块中抽取**原文子串**，并保存 7B `research_run_evidence_manifests`。这只证明证据的定位和处理完成，不证明“某个研究结论”已被证据支持。7C `ResearchReportWritingService` 则只接受 `ClaimStatus.SUPPORTED` 的 Claim，因此在缺少 Claim 自动生成/验证的情况下，正常从零运行会安全地停在 Writing。

3F 填补**候选生成**环节：

1. 在 `VERIFYING` 阶段的证据提取及 Run 级 Evidence Manifest 已持久化后，构建界限明确的 LLM 输入。
2. 生成器只能返回 `text` 和 `evidence_ids`，**不得返回可信状态**、最终置信度或最终报告。
3. 对每个候选进行身份、数量、长度、引用集合及去重检查；引用必须属于当前 Run 的证据清单与本次受限输入。
4. 使用 Run 行锁、Stage Journal 和单调 Fencing Token 重新检查**当前数据库**，确认模型调用期间输入没有变化。
5. 在同一个 PostgreSQL 事务内写入 `research_claims`、`claim_evidence`、`research_run_claim_generations`。
6. 所有 3F 新建 Claim 都是 `ClaimStatus.UNSUPPORTED`，`confidence=0.0`；不能被 7C Writer 当作支持性结论。

首版选择在现有 `VERIFYING` 内、**证据处理完成之后**运行候选生成器，不增加新的 ResearchRun 状态。下一阶段 3G 在它之后实施独立的语义验证。未配置 3F 后端时，7B 既有 evidence-only 行为保持不变，不会制造假的 Claim。

## 2. 实际执行流程

```text
ResearchPipelineStageRunner.run_stage(VERIFYING, lease)
   │
   ├─ ResearchEvidenceExtractionService.extract_run(..., lease)
   │     ├─ Chunk 级准确摘录 / 去重 / Evidence 写入
   │     └─ Fenced EvidenceCompletionManifest（包括合法零证据）
   │
   ├─ （仅启用 3F 后端）ResearchClaimGenerationService.generate_run
   │     ├─ TX #1: 锁 Run + 校验当前 Lease/Journal/7B Evidence Manifest
   │     ├─ 读取 Run 计划、受限 Evidence 文本和指纹，形成 input_sha256
   │     ├─ 释放事务，调用 OpenAI-compatible 模型
   │     ├─ 只接收 {claims:[{text,evidence_ids:[]}]}
   │     ├─ 数量 / 长度 / 范围 / 重复校验
   │     └─ TX #2: 再锁 Run + 重新计算完整输入快照
   │           ├─ Lease 失效 / Journal 已结束 / Evidence 被改 → 全部拒绝
   │           └─ 插入 UNSUPPORTED Claims + 引用 + Run Receipt（一次提交）
   │
   └─ ResearchRunExecutor.finish_stage(VERIFYING, require_artifact_proof=True)
         ├─ 7B Evidence Manifest 证明
         ├─ 如存在 Claim Receipt，验证 Claim 文本 / 引用 / 摘要
         └─ 同事务将 Stage Journal 标为 COMPLETED
```

**不宣称外部模型调用恰好一次。** LLM 网络 I/O 与数据库事务分离。此方案保证的是：提交前再次验证输入/执行权，候选和凭据原子持久化；重复写入会被拒绝。3E 的 Safe Resume 仍对不确定的 `VERIFYING` 崩溃窗口采取保守策略。

## 3. 数据模型与追踪

### 3.1 复用业务表

- `research_claims`：主张正文，`status='unsupported'`，`confidence=0.0`。
- `claim_evidence`：顺序化的 Evidence FK，不可引用未参与当前输入的 Evidence。
- `research_evidence`：从 7B Manifest 已验证的 Evidence 中选择。

旧 `ResearchAggregate` 为 **Task 级**，不能直接作为当前 Run 的生成证明；3F 使用新的 Run 级 Receipt 将两者绑定。

### 3.2 新增表 `research_run_claim_generations`

字段：

| 字段 | 用途 |
|---|---|
| `run_id`（PK）/ `task_id` | 绑定一次 Run 与其 Task；Run 内只允许一份不可重复写入的凭据 |
| `fencing_token` | 绑定生成时的执行代次 |
| `input_sha256` | Task 查询、Plan、完整 Evidence/Document 哈希清单、本次输入摘录与生成界限的规范化摘要 |
| `output_sha256` | 候选 ID、规范化文本和有序 Evidence 引用的规范化摘要 |
| `available_evidence_count` | Run Evidence Manifest 中的可用 Evidence 总数 |
| `selected_evidence_ids`（JSONB） | 本次有界模型输入实际使用的 Evidence IDs |
| `claim_ids`（JSONB）/ `candidate_count` | 本轮创建的候选集合和数量 |
| `generator_model` | 模型标识；非验证器 / 不代表质量担保 |
| `created_at` | PostgreSQL 服务器生成的创建时间 |

候选 `claim_id = UUIDv5(run_id, SHA256([文本, 排序后的 Evidence IDs]))`，因此不同 Run 不会因为相同句子而无意共享候选 ID。生成器输出重复项被拒绝，不会产生隐含去重的“部分成功”。数据库收到第二份 Receipt 时拒绝写入，不静默覆盖。

**合法零结果**：如果 Run 的 Evidence Manifest 有完整提取工作但没有有效 Evidence，3F 不请求模型，仍写一份 `candidate_count=0` 的显式 Receipt；如果有 Evidence 但模型返回空列表，也可以形成零候选 Receipt。7C 没有 `SUPPORTED` Claim 仍然会拒绝 Writing，不会假完成。

### 3.3 字节完整性不是语义真理

SHA-256 检测原数据与生成凭据是否发生变化，不能判断模型提出的主张是否被 Evidence 逻辑蕴含。3F 对模型的**唯一**授权是“提出候选、选择引用”。`SUPPORTED/CONFLICTED` 的语义判断属于 3G；模型输出 JSON 如含 `status` 字段会被严格解析器拒绝。

## 4. 配置与资源界限

3F 默认关闭，不影响此前通过的 7C 完成契约。仅在已配置 PostgreSQL、真实 Planner 和 Evidence Extractor 的扩展进程中启用：

```bash
export ENTERPRISE_RESEARCH_CLAIM_BACKEND=openai-compatible
export ENTERPRISE_RESEARCH_CLAIM_BASE_URL='https://your-openai-compatible-provider/v1'
export ENTERPRISE_RESEARCH_CLAIM_MODEL='your-model-id'
# 使用进程私密环境变量提供 ENTERPRISE_RESEARCH_CLAIM_API_KEY；不要写入仓库/测试日志。
```

可选界限（默认值）：

| 环境变量 | 默认值 |
|---|---:|
| `ENTERPRISE_RESEARCH_CLAIM_MAX_EVIDENCE` | 12 |
| `ENTERPRISE_RESEARCH_CLAIM_MAX_EXCERPT_CHARS` | 1800 |
| `ENTERPRISE_RESEARCH_CLAIM_MAX_CANDIDATES` | 12 |
| `ENTERPRISE_RESEARCH_CLAIM_MAX_CLAIM_CHARS` | 600 |
| `ENTERPRISE_RESEARCH_CLAIM_MAX_REFERENCES_PER_CLAIM` | 5 |
| `ENTERPRISE_RESEARCH_CLAIM_TIMEOUT_SECONDS` | 60 |

所有界限必须为正整数，引用数不得大于输入 Evidence 数量上限。输入 Evidence 以 ID 稳定排序，只挑前 N 条并截断输入摘录；`available_evidence_count` 仍保存总数。**这是明确披露的有界抽样，不是对全部证据已充分总结的质量承诺。** 若要提高全面性，可在后续阶段设计分批覆盖与处理清单，不能直接把窗口输出误记为全覆盖。

安全与数据边界：Evidence 摘录将传给用户配置的第三方 OpenAI-compatible 模型，组织应审核其数据处理授权。Prompt 明确要求将文本视作不可信数据；它是降低提示注入风险的措施，不是绝对安全性证明。

## 5. 主要源码职责

| 文件 | 具体职责 |
|---|---|
| `application/claim_generation.py` | 候选协议、边界与引用验证、规范化 SHA、UUIDv5、服务编排 |
| `infrastructure/claim_generation/openai_compatible.py` | 严格 JSON 输出、HTTP 错误封装、请求范围限制 |
| `infrastructure/postgres/claim_generation.py` | 两次 Fenced 校验、Run 输入快照、一次性插入 Claims/Receipt |
| `infrastructure/postgres/claim_generation_receipt.py` | 读取持久化候选/引用并重算摘要，供 Stage Journal 完成事务使用 |
| `application/pipeline.py` | 在 VERIFYING 的 Evidence Extraction 后追加可选候选生成 |
| `infrastructure/postgres/stage_completion.py` | 有 Receipt 时启用 Run 候选完整性检查，无 Receipt 时维持 7B 行为 |
| `plugin.py` | 环境配置、PostgreSQL/Planner/Evidence 条件、Provider 依赖注入 |
| `models.py` / `migrations/versions/b6e9f4c0a21d_...py` | 新表、外键、JSONB 形状约束和 Alembic 单头迁移 |

## 6. 验收说明（必须在用户 WSL2 执行）

在 `e4ec100` 干净工作区应用补丁，然后运行针对**已修改及新增**文件的 Ruff。可能需要 `ruff check --fix` 和 `ruff format`：

```bash
cd ~/projects/enterprise-research-platform
PATCH='/mnt/d/Downloads/3F-claim-generation.patch'
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check

mapfile -t PY_FILES < <(
  { git diff --name-only -- '*.py'; git ls-files --others --exclude-standard | grep '\.py$'; } | sort -u
)
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"
uv run --no-project pytest tests/application/test_claim_generation.py tests/infrastructure/test_openai_compatible_claim_generator.py -q
uv run --no-project pytest -q
```

PostgreSQL 迁移只作用于核实过的**本地开发库**，先备份，确认 `alembic current == ea7c2f19b540`，再升级：

```bash
pg_isready -h 127.0.0.1 -p 5432
uv run --no-project alembic current
uv run --no-project alembic upgrade head
uv run --no-project alembic current  # b6e9f4c0a21d (head)

ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_CLAIM_TESTS=1 \
uv run --no-project pytest tests/infrastructure/test_postgres_claim_generation.py -v -rs
```

真库用例覆盖：Run 归属、`UNSUPPORTED` 状态、write-once、外部模型调用期间丢 Lease、Evidence 篡改导致全部拒绝、篡改候选导致 Journal 回滚，以及合法零 Evidence Receipt；建议复跑 7B、7C 的既有 PostgreSQL 用例。

**当前开发沙箱不能连用户的 WSL2 PostgreSQL，Ruff 也不在当前离线环境中。** 这两项务必按日志实际结果验收；不要仅依据本说明标记通过或提交。

## 7. 明确保留的技术边界

1. **3G 未实现**：不会自动将 Claim 升为 `SUPPORTED`；7C 的最终报告契约仍会拒绝无支持主张的任务。
2. **不是语义证实**：严格 Evidence ID/原文指纹不代表该证据蕴含模型文本；验证仍须独立完成。
3. **不是全证据穷举**：当前最多挑选 12 条 Evidence；Receipt 记录实际选择和总量。
4. **不是跨外部模型的 exactly-once**：Lease 过期和进程崩溃可能重复计费，但不会静默重复写入 Run Receipt。
5. **Task 级 Aggregate 仍是整体替换**：其他不遵守 Fenced 写入约束的管理路径可以删除或重写 Task Claims；完成检查会检测已生成 Receipt 的 Claim 缺失/摘要变化，但长期稳定的跨 Run Claim 血缘需要在 3G 或后续继续加强。
6. **无新的 Run 查询 API**：阶段成果目前经原 Task Claim 读接口及 Run Receipt 数据库关系追踪；可在 3G 增加 Run 级候选/审核查询接口。

如任一质量门禁失败，不要提交 Git；先定位日志与失败断言，确认修复后再做 WSL2 验收。
