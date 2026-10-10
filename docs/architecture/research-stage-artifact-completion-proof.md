# 3E-6D-7B — Artifact Completion Proof / 业务产物完成证明

> 基线：已提交 `4f973c1`（7A 已修复 Handler 缺失导致的假完成）。7B 新增数据库迁移 `c4e7b0a6d9f2`，原 head 为 `7c3a1e9d4b62`。所有运行数据库的验证都必须先在确认安全的本地开发库上迁移；**暂不提交补丁，直到 WSL2 真库验收通过**。

## 1. 要消除的风险

7A 将“缺少 Handler 的正常返回”改成明确失败，但仍有另一类风险：Handler 存在且正常返回，**真正的 Plan / Source / Document / Evidence 可能没有持久化，或者与本次 Run 无关**。原 Executor 会将返回 `None` 直接写为 `Stage Journal COMPLETED`。仅在 Executor 中先读产物、再另外调用 `finish_stage(COMPLETED)` 也有 TOCTOU 竞态。

7B 的生产 PostgreSQL 路径将完成条件升级为：

```text
Acquire fenced Run lease
   → Stage Journal STARTED
   → StageRunner 执行业务与受保护写入
   → 在单个 PostgreSQL 事务中：
      1. SELECT research_runs FOR UPDATE 并验证 Lease / Stage / Token / 到期
      2. SELECT STARTED Journal（同代次）FOR UPDATE
      3. 锁定 Task 行和参与证明的业务行
      4. 在同一 Session 中收集业务产物并执行 Completion Validator
      5. 成功：Stage Journal COMPLETED 与本事务同时 COMMIT
      6. 失败：COMPLETED 事务回滚；Executor 尝试持 Lease 写 Journal FAILED、Run FAILED
```

Checkpoint **不是**生产业务完成的唯一证明；7B 不引入跨库分布式事务，也不重放 LLM/搜索工具调用。

## 2. 四阶段证明内容（目前 Writing 仍禁止完成）

| Stage | 成功必须具备的持久化证明 | 明确拒绝的反例 |
|---|---|---|
| `PLANNING` 研究规划 | 当前 Run 唯一关联的 Plan、非空 objective、至少一个非空研究问题、没有重复文本 | Planner 返回，但数据库无 Plan；空问题集合 |
| `SEARCHING` 网页搜索 | 每一个计划问题都能在 WEB Source 的 `metadata.search.hits` 中找到 **本 Run + Question ID + 完整 Query** 的来源证明 | Source 来自别的 Run；只有部分问题有来源；来源没有 URL |
| `EXTRACTING` 来源抓取 | Task 下每个 WEB Source 有匹配 URL 的 Retrieval 结果，至少一个成功正文且正文 SHA-256 与内容一致；失败结果要有本 Run 的 provenance | 没有任何文档；文档 URL 不匹配；只有其他 Run 的失败；内容哈希被改 |
| `VERIFYING` 证据提取 | 存在当前 Run / 当前 Fencing 代次的持久化完成清单，计划工作非空，`completed_chunks == scheduled_chunks`，关联文档哈希及 Evidence 指纹与当前数据库行一致 | Handler 返回零 Evidence 却没处理 Chunk；任务进度不完整；Manifest 来自旧代次；Evidence 内容后来被修改 |
| `WRITING` 报告写作 | **暂无完整 Report Writer / Run-scoped Report Artifact** | 继续沿用 7A，禁止写入成功，留待 7C |

`VERIFYING` 特别说明：**证据为零不等于阶段没有执行**。当至少一个文档参与、调度了非空 Chunk、全部处理完成且无候选通过校验时，可以保存 `evidence_ids=[]` 的完成清单；但如果没有调度任何 Chunk 或未保存清单就返回，不得标记为完成。

## 3. 7B 新增 Evidence Manifest 的存储契约

新增 `research_run_evidence_manifests`，每个 Run 最多一条（PK 为 `run_id`），包括：

- `task_id` / `run_id`：**研究任务与执行实例身份**；不允许借用同一 Task 上其他 Run 的证明。
- `fencing_token`：**写入清单时的合法执行代次**，下一代旧代次证明不得直接复用。
- `eligible_document_count`：具备非空文档 Chunk 的文档数量。
- `scheduled_chunk_count` / `completed_chunk_count`：**计划和实际完成的模型提取调用数**，均需正数且相等。
- `verified_count` / `created_count` / `reused_count`：**机械验证与持久化结果计数**，不能为负且不允许自相矛盾。
- `document_hashes`：**所有参与工作负载的成功文档内容哈希**，用于防止改变正文后复用旧清单。
- `evidence_ids` / `evidence_hashes`：**已提取/复用的 Evidence ID 与其 source_id、locator、content 的 SHA-256 指纹**；无证据时合法为空数组、空对象。
- `completed_at`：数据库时钟写入的提交时间。

`PostgresResearchEvidenceManifestRepository.save_guarded()` 在 `lock_fenced_stage()` 下**只写一次**；只有所有 Evidence 写入与工作协程成功返回后才写清单。因此进程在 Evidence 写入后、清单保存前崩溃会留下 **无证明的 VERIFYING**，而非假成功。

完成 Journal 的数据库事务中再次核查 Manifest 与真实文档/Evidence；与完成写入共享 Run 行锁。`ResearchTaskRow` 和所依赖的 Source / Document / Evidence 行也在这个事务中读取并上锁，限制其他受控业务写入的竞态。

**边界提醒**：数据库超级用户或未受控的行政/历史 unfenced 写入不是 Lease 保护的普通生产写路径；7B 并不声称自己能防止所有绕过应用事务模型的修改。不同 Run 共享 Task 聚合写入的跨 Run 并发隔离仍需进一步加强。

## 4. Executor 的兼容策略

`ResearchRunExecutor(..., enforce_artifact_proof=True)` 强制为每个阶段的 `COMPLETED` 调用验证模式，**Extension 的 PostgreSQL 配置已明确启用**，不得给生产配置添加“跳过证明”的后门。使用 `ResearchLangGraphStageRunner` 时仍经同一个 Executor 和 Run Repository 完成证明，图节点返回不代替业务完成。

- 旧测试替身及内存仓储的 `ResearchRunExecutor` 默认不强制持久化证明，以保持既有 Legacy Fixture/测试行为；这不是生产可用性承诺。
- 如果刻意对内存仓储启用 `require_artifact_proof=True`，会明确抛出 `ResearchStageCompletionError`，不会假装内存快照具有 PostgreSQL 原子提交证明。
- 基层 `finish_stage()` API 仍支持测试用的低层“未验证 Journal 构造”；**必须区分测试造数与生产 Executor 的强制验证模式**。
- 证明失败时抛出 `ResearchStageCompletionError`；Executor 在仍持合法 Lease 时尝试落库 `Journal FAILED` 和 `Run FAILED` 并发送失败事件。若此间丢失 Lease，遵守既有 Fencing 逻辑，不以陈旧代次写 FAILED。

## 5. 主要文件（中文职责）

| 文件 | 具体作用 |
|---|---|
| `application/stage_completion.py` | **完成契约与凭据对象**：纯 Python 阶段规则、EvidenceManifest 不变量、Evidence 内容指纹函数 |
| `application/errors.py` | **失败语义**：业务产物不满足阶段完成证明时抛出明确异常 |
| `application/evidence_manifest_repository.py` | **证据清单仓储接口**：Application 层不依赖具体数据库 |
| `application/evidence_extraction_service.py` | **证据提取服务**：在全部 Chunk 完成、Evidence 已提交后，生成和持久化清单；接受合法 0 Evidence |
| `application/run_service.py` / `run_repository.py` | **完成证明 API 契约**：`require_artifact_proof` 仅对 COMPLETED 生效 |
| `application/executor.py` | **阶段完成裁决**：Handler 正常返回后，要求 PostgreSQL 业务证明成功才能完成 Journal；异常转 FAILED |
| `infrastructure/postgres/stage_completion.py` | **同事务数据库验证器**：在 Run 行锁下查询 Plan/Source/Document/Manifest/Evidence 并锁定参与行 |
| `infrastructure/postgres/run_repository.py` | **Fencing + Journal 原子提交点**：调用验证器，失败则不写 COMPLETED |
| `infrastructure/postgres/evidence_manifest_repository.py` | **证据清单实际数据库实现**：当前 VERIFYING Lease 下只写一次 |
| `infrastructure/postgres/models.py` | **ORM Schema**：新增 Evidence Manifest 关系表 |
| `migrations/versions/c4e7b0a6d9f2_add_run_evidence_completion_manifests.py` | **Alembic 数据库迁移**：创建和回退唯一 Run 级清单表 |
| `plugin.py` | **生产装配**：PostgreSQL 模式启用证明并接入 Manifest Repository，启动时检查新表 |
| `tests/application/test_stage_artifact_completion.py` | **不依赖 DB 的反例测试**：空/错/重复清单、合法 0 Evidence、内存仓储拒绝冒充持久化证明 |
| `tests/infrastructure/test_postgres_stage_artifact_completion.py` | **真实 PostgreSQL 验收**：逐阶段缺失产物拒绝、事务回滚、合法 0 Evidence、正文/证据哈希篡改、Noop Runner 失败 |

## 6. 本地 WSL2 正式验收（严格开发库隔离）

1. 提交基线必须为 `4f973c1`、工作区应用前必须干净。先备份本地开发数据库。不要对生产数据库执行升级和破坏性故障测试。
2. `git apply --check` → `git apply` → `git diff --check`。迁移文件尚未提交，但 Alembic 可以发现。
3. 在 WSL2 中准备所需变量：`ENTERPRISE_RESEARCH_DB_NAME=enterprise_research`、`ENTERPRISE_RESEARCH_DB_USER`、`ENTERPRISE_RESEARCH_DB_PASSWORD`，主机限制 `127.0.0.1`，端口缺省 `5432`。**不在终端日志中回显密码**。
4. `uv run --no-project alembic current` 原版本预期 `7c3a1e9d4b62`，然后 `uv run --no-project alembic upgrade head`，再次 `current` 应为 **`c4e7b0a6d9f2 (head)`**。
5. Ruff（所有新增/修改 Python 文件，含迁移）必须 `check` 和 `format --check` 均通过；否则只修导入与格式，不掩盖语义错误。
6. `uv run --no-project pytest tests/application/test_stage_artifact_completion.py -q`。
7. `uv run --no-project pytest -q`；DB 相关跳过是预期现象，不算 PostgreSQL 通过。
8. 仅本地开发库（DB host/name 由测试再检查）：

```bash
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_STAGE_PROOF_TESTS=1 \
uv run --no-project pytest \
  tests/infrastructure/test_postgres_stage_artifact_completion.py -v -rs
```

目标 **3 passed、0 skipped**。用例对随机 Task/Run 定向创建并删除，不会清理整个数据库；测试要求 Schema 已迁移。再运行原 7A PostgreSQL 回归，确认 Handler 缺失仍 FAIL 而不是“成功”。

7B 是新 Schema 与生产 Executor 语义变更。至少执行一次 5A/5B 中受影响的真实数据库 Fencing/Journal 用例，再考虑提交；如果报错先保留完整 traceback。对于包含旧 5C 恢复微基准结果的性能结论，不能不经重新测量就直接套用新版本的数据。

## 7. 仍不声称解决的问题

- `ResearchSafeResumeInspector` 依旧对 `VERIFYING` 和 `WRITING` **保守暂停**。7B 只证明正常执行后的 `VERIFYING → Journal COMPLETED`，没有完成崩溃后在 Verifying 接续到 Writing 的证明统一；需要后续阶段将 Manifest 接入恢复对账。
- `WRITING → COMPLETED` 仍无自动 Writer 和 Run 范围 Report Artifact，属于 **7C**。
- 不能从 Manifest 的 `completed_chunk_count` 反推外部 LLM/API Exactly-once；只表示可信服务已观察到所有已调度协程正常返回并持久化回执。
- 内存仓储没有跨进程的 Durable Proof，也不是生产用数据库。
- 未验证多机 HA、任意管理者 Unfenced 写入、跨 Run 共享 Task 的完整串行化或端到端研究报告质量。
