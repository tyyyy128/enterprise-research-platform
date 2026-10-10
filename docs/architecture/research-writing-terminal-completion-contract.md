# 3E-6D-7C — Writing & Terminal Completion Contract

> 中文：研究报告写作与最终完成契约。基线 `14a2d46`；本阶段为第一版**确定性、基于已有 SUPPORTED Claim 的报告写入**，并非已经实现 LLM 自动 Claim Generation 或完整自然语言写作。

## 1. 为什么必须新增 Run 级报告？

7A 已解决 Handler 缺失时“空跑成功”；7B 已将 Planning/Searching/Extracting/Verifying 的业务产物核验与 Stage Journal COMPLETED 放入同一 PostgreSQL 事务。但 `ResearchRunExecutor` 原本在 Writing 正常返回后，仍可以单独 `advance_run(..., COMPLETED)`。

现有 `research_reports` 是 **Task 级唯一报告**（`UNIQUE(task_id)`），由人工 API 聚合构建；它没有 `run_id`、`fencing_token` 或本轮 Evidence Manifest 身份。因此**不能**把“Task 下存在旧报告”当成本轮 Writing 的完成证明。

7C 新增一张独立表：

`research_run_report_artifacts`（Run 级最终报告快照）。

| 字段 | 中文作用 |
|---|---|
| `run_id` | 主键，绑定本次 ResearchRun，禁止同 Run 重复生成 |
| `task_id` | 归属任务，防止跨 Task 误读取 |
| `fencing_token` | Writing 时有效的执行代次，旧代次不可冒用 |
| `title` / `body` | 实际持久化的报告标题及 Markdown 正文 |
| `citations` | Claim→Evidence→Source 的结构化引用快照，记录 Evidence Fingerprint |
| `body_sha256` | 报告正文内容指纹 |
| `payload_sha256` | 标题、正文、引用列表整体指纹 |
| `created_at` | PostgreSQL 时钟记载的持久化时间 |

这张表是**一次性写入的报告快照**，与手工 `ResearchReport` 模型分开；现有 Task 级报告 API 行为不变。PG 约束提供主键、外键与基本格式约束，真正的跨对象业务一致性由同事务核验完成。数据库超级用户仍可修改记录，因此“不可变”是**应用层写入契约**，而非数据库物理 WORM。

## 2. Writer 第一版能力范围

选择 `ENTERPRISE_RESEARCH_WRITER_BACKEND=verified-claims`，仅在 PostgreSQL + 真实 Evidence Extractor 配置下启用 `ResearchReportWritingService`。默认 `disabled`，保持 Fail Closed。

1. 检查 WRITING Run 行已锁定且 Lease/Fencing 有效，并要求当前代次 Writing Journal = STARTED。
2. 检查当前代次 Verifying Journal = COMPLETED。复用 **7B 原有验证器**，重新核对 Plan、Search、Retrieval、Evidence Manifest，检查 Document/Evidence 指纹。
3. 读取 Task 的 `SUPPORTED` Claims，**只选择完整证据集合属于当前 Run Evidence Manifest** 的 Claim。含有本 Run 证据和其他 Run 证据的混合 Claim 直接拒绝；只有别的 Run 证据的 Claim 不选入本报告。
4. 为每个选中 Claim 构造结构化引用：`claim_id → evidence_id → source_id / URL / locator / evidence fingerprint`。
5. 使用确定性 Markdown 模板组织标题、事实主张与原文摘录，保存 SHA-256 正文/整体快照；**零个 SUPPORTED Claim 必须失败**，不编造研究结论。
6. 同一 Run 报告只能写一次；不同 Run 有独立产物和引用证明。

此 Writer **不是 LLM 报告撰写系统**。它不自动产生 Claim，不对 Claim 的事实真假做完整语义核验，也不会把零条 Evidence 的 Verifying 结果冒充为研究报告。它只组装已被现有领域服务明确标为 SUPPORTED、并与当前 Run Manifest 一致的 Claim。后续 3F/3G 需要增加自动 Claim Generation 与语义核验，才能建立真正自主的研究闭环。

## 3. 两道原子完成证明

### 3.1 `WRITING Journal COMPLETED` 的证明

```text
StageRunner(WRITING) 返回
    → PostgreSQL session.begin
    → Run SELECT FOR UPDATE + Lease/Fencing/Stage 检查
    → 当前 Writing Stage Journal STARTED 行锁
    → Run Report Artifact 存在且 Task/Run/Fence 完全一致
    → Verify Journal & Evidence Manifest & Plan/Source/Document/Evidence 重新核对
    → 重新生成确定性正文和引用，逐字段核对内容及 SHA-256
    → 同事务更新 Writing Journal COMPLETED
```

任一证明不成立，事务回滚，**不得提交 Writing Journal COMPLETED**。Executor 在持有当前 Lease 时走现有失败路径，尝试记录 Writing FAILED 和 Run FAILED。

### 3.2 `ResearchRun WRITING → COMPLETED` 的证明

`PostgresResearchRunRepository.save_guarded()` 使用 `UPDATE ... WHERE lease_owner/token/expiry/status ... RETURNING` 取得 Run 行的唯一写入权，并在**同一事务**执行：

- Current generation Writing Journal 必须已 COMPLETED；
- Run Report 必须存在且与此 Run/Fence 对应；
- 标题、正文、引用结构与当前业务记录重新生成的内容相同；
- Body / Payload SHA-256 一致；
- Verifying 的业务证据证明仍有效。

检查失败则整个 Run `COMPLETED` 更新回滚。**即使调用者没有打开 7B 的 `enforce_artifact_proof` 开关，PostgreSQL 的 WRITING → COMPLETED 也不能绕过新终态契约。** Memory Repository 保留给早期单测使用的简化行为，不能视为生产实现。

### 3.3 事务竞态边界

验证与 `COMPLETED` UPDATE 共享 Run 行锁，业务核验还锁定 Task 与涉及的关系行；执行权 Claim/Release/Renew 同时遵循 Run 行锁，所以本阶段不会出现“同一个旧 Lease 检查通过后，新代次已经接管，仍能提交终态”的明显 TOCTOU 空隙。

**不保证**外部 LLM / 搜索 API Exactly-once，也不保证后续管理员拥有数据库写权限时无法篡改已完成的所有关联业务行。查询 API 会重新核查存储报告的内容哈希；审计级不可变存储/签名仍需后续建设。

## 4. 原有阶段与恢复测试的行为变化

之前某些 5A/5B/4C 用例用 Noop StageRunner 验证“恢复以后是否能调度 Searching”；它们顺带期待最终 Run COMPLETED。7C 后这种用例没有 Report Artifact，**只能证明恢复调度安全，不能证明研究完成**。因此对应联机测试的最终期望应为 `FAILED`，同时仍检查独占 Claim、Planning 不重放、Searching 只调度一次等不变式。

7C 不扩大 Safe Resume 的 Verifying/WRITING 恢复白名单：崩溃时无法证明的 Run 仍 Fail Closed，**不会自动重写已有报告**。写入 Run Report 与 Journal 仍是两个依次发生的事务；若崩溃在两者之间，留下“有报告但 Journal STARTED”的状态，必须人工对账，而非自动重做。

## 5. API / 部署开关

- `GET /api/enterprise-research/tasks/{task_id}/runs/{run_id}/report`：仅 Run 状态为 `COMPLETED` 时返回 Run 级 `title/body/citations` 和哈希；否则返回 HTTP 409；错误 Task/Run 返回 404；Memory backend 无该仓储则 503。
- `ENTERPRISE_RESEARCH_WRITER_BACKEND=disabled`（默认）：WRITING 明确失败。
- `ENTERPRISE_RESEARCH_WRITER_BACKEND=verified-claims`：必须配 PostgreSQL、Evidence Extractor 和受支持的 Claim，否则明确失败。

即便启用 `verified-claims`，**现有 pipeline 尚不会自动创建 SUPPORTED Claim**，因此一般从零研究任务仍将在 WRITING 失败；可以通过受控测试预先构造已验证 Claim，证明这条终态链路。真实自动研究完成属于后续 3F/3G/3H 的集成目标。

## 6. 代码文件地图（中文）

| 文件 | 职责 |
|---|---|
| `application/report_writing.py` | **报告写作模型和纯函数**：受支持 Claim、Evidence 引用、Markdown、SHA-256 |
| `infrastructure/postgres/report_artifacts.py` | **事务内 Report Writer**：锁 Run、核对当前代次 Evidence/Claim、写入一次性报告、再次验证 |
| `infrastructure/postgres/stage_completion.py` | **Writing 阶段完成证明**：接入 7B 的原子 Stage Journal 提交机制 |
| `infrastructure/postgres/run_repository.py` | **终态强校验**：Run CAS 更新与报告证明同一 PostgreSQL 事务 |
| `application/pipeline.py` | **真实 Writing 分发**：只有显式配置 Writer 才能运行 |
| `plugin.py` | **依赖装配与运行开关**：注册 Writer、检查新表、添加 Run 报告 API |
| `api/schemas.py` | **Run Report API DTO**：返回正文、引用和校验值 |
| `infrastructure/postgres/models.py` | **新 ORM 表** |
| `migrations/versions/ea7c2f19b540_add_run_report_completion_artifacts.py` | **Alembic Schema 迁移**：前一个 Head `c4e7b0a6d9f2` |
| `tests/application/test_run_report_completion.py` | **离线确定性渲染 / 注入防护 / Writer 缺失测试** |
| `tests/infrastructure/test_postgres_terminal_report_contract.py` | **真实 PostgreSQL：未写报告/被篡改/没有支持 Claim/未完成 Journal/合法终态** |

## 7. WSL2 安全验收（待用户执行）

前提：HEAD = `14a2d46`；工作区空；仅本地 `127.0.0.1:5432` 的 `enterprise_research` 开发数据库（非生产）。请先备份数据库；不要在聊天中粘贴数据库口令。

```bash
cd ~/projects/enterprise-research-platform
PATCH='/mnt/d/Downloads/3E-6D-7C-writing-terminal-contract.patch'
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
mapfile -t PY_FILES < <( { git diff --name-only -- '*.py'; git ls-files --others --exclude-standard | grep '\.py$'; } | sort -u )
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"
uv run --no-project pytest tests/application/test_run_report_completion.py -q
uv run --no-project pytest -q
```

数据库迁移：

```bash
uv run --no-project alembic current
# 预期原 head: c4e7b0a6d9f2
uv run --no-project alembic upgrade head
uv run --no-project alembic current
# 7C 新 head: ea7c2f19b540
```

7C 专用 PostgreSQL 验收：

```bash
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_TERMINAL_PROOF_TESTS=1 \
uv run --no-project pytest tests/infrastructure/test_postgres_terminal_report_contract.py -v -rs
```

预期：2 passed，0 skipped（包括成功/负例）；再用已有 7B 的 `ENTERPRISE_RESEARCH_STAGE_PROOF_TESTS=1` 和 7A 的 `ENTERPRISE_RESEARCH_STAGE_CONTRACT_TESTS=1` 复测。若你的原 4C/5B PostgreSQL 测试开关有特殊配置，应在独立的、可清理的测试环境完成回归，不能仅凭默认 pytest 中的 skipped 推断通过。

真实数据库未在隔离环境执行；先完成测试验收，再提交 Git：

`feat: add fenced run report and terminal completion contract`

## 8. 遗留事项

1. **自动 Claim Generation**：本阶段不生成 Claim，也不自动标记 SUPPORTED；未来需要独立 LLM 提议、跨来源支持验证。
2. **高级报告写作**：这里是确定性、引用驱动的结构化 Markdown 摘要，不是完整的 LLM Narrative Writer。
3. **可恢复 Writing**：需要 Report Manifest/Journals 的明确崩溃恢复证明，不应跳过现有 Safe Resume 保守限制。
4. **并行 Task/Run**：共享 Task 级聚合写入仍需强化更细粒度的并发更新协议。
5. **权限与审计**：报告 GET 路由目前没有独立 RBAC；部署时必须继承宿主可信身份校验，避免跨用户读取研究信息。
6. **统计与验收**：新增场景需要真实 PostgreSQL 及 WSL2 的 Ruff、默认回归、原有 Fencing 联机验收，才算 7C 完成。
