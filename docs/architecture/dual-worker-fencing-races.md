# 3E-6D-5B — 双 Worker 并发竞争与 Fencing 实证测试

## 目标与边界

在 5A 的子进程 `SIGKILL` 故障验证之后，5B 重点检验**两个仍存活的 Worker** 同时访问同一个 Research Run 的竞争窗口，而不是重复做顺序调用测试。

**本阶段只新增测试与文档，不修改生产代码，也不新增数据库迁移。** 结果用于验证当前 PostgreSQL Run 行作为唯一执行权仲裁点的合理性。与 DeerFlow 宿主的 Agent Checkpointer 不存在隐式共享；Checkpoint 仍是 Graph 状态，不是业务提交凭据。

## 实际源码中的关键同步点（中文解释）

| 源文件（在 `src/enterprise_research` 下） | 中文职责 | 并发约束 |
| --- | --- | --- |
| `infrastructure/postgres/run_repository.py` | Run 状态、Lease 原子 Claim/Renew/Release，Stage Journal | `claim_lease()` 用条件 `UPDATE ... RETURNING`，`start_stage()` / `finish_stage()` 在事务中锁 Run 行 |
| `infrastructure/postgres/fenced_writes.py` | 业务写入执行权保护 | `lock_fenced_stage()` 用 `SELECT ... FOR UPDATE` 验证 task、run、stage、owner、token、expiry |
| `infrastructure/postgres/plan_repository.py` | 计划与研究问题持久化 | 先锁 Run，再在**同一事务**保存 Plan |
| `infrastructure/postgres/document_repository.py` | 抓取文档持久化 | 先锁 Run，再验证 Source 归属，最后同事务保存 Document |
| `infrastructure/postgres/repository.py` | Source、Evidence、Claim 等研究聚合数据 | guarded 聚合写入先锁 Run，再修改聚合表 |
| `application/safe_resume.py` | 重启后的只读对账 | Stage Journal + 业务产物 + 可选 Graph 观察共同组成恢复证明 |
| `application/executor.py` | 正式恢复调度 | `resume()` 先 Inspect、再 Claim、再 Inspect，恢复证明变了就释放 Lease 并停止调度 |

PostgreSQL 默认 `READ COMMITTED` 隔离下：同一行的 `UPDATE`/`SELECT FOR UPDATE` 竞争会等待当前事务结束；等待后对新行版本重新核对条件。因此行锁不仅保护“请求时是否有效”，还定义了业务事务与下一代 Lease Claim 的串行化顺序。此事实以 PostgreSQL 官方事务隔离与行级锁文档为准。

## 新增测试文件

| 文件 | 作用（中文） |
| --- | --- |
| `tests/application/test_dual_worker_preclaim_barrier.py` | **内存版并发门闩**：两个不同 Executor 的第一次 Safe Resume Inspect 全部完成后，才同时 Claim。确认只有一个进入 Searching，无第二次阶段调度。默认回归执行 |
| `tests/infrastructure/test_postgres_dual_worker_races.py` | **真实 PostgreSQL 竞争矩阵**：用独立 Session、受控 Event、行锁以及数据库时钟证明 Claim 独占、代次递增、陈旧写入拒绝、Journal 串行化、恢复调度独占。必须显式启用两个测试标志 |
| `docs/architecture/dual-worker-fencing-races.md` | 本中文架构与验收说明 |

## 并发故障矩阵（5B）

| 代号 | 人为制造的竞争 | 预期验证结果 | 涉及数据 |
| --- | --- | --- | --- |
| R1 | 未执行 Run 行被第三方事务锁住，两个 Worker 同时尝试普通 Claim | 在锁释放前双方均不能完成；放行后恰好一个获得 generation 1 | Run、Lease |
| R2 | 处于 `PLANNING` 的旧 Lease 被测试事务标记过期，两个 Worker 同时尝试 `allow_inflight=True` | 等待同一行锁，恰好一个获得 generation 2；旧 token 无法 Release、Renew、Start/Finish Journal；旧 STARTED 保持待对账 | Run、Lease、Journal |
| R3 | 旧 Worker 的 Plan 保存已取得 Run 行锁，锁保持到 Lease 到期；新 Worker 并发 Claim | 新 Worker 不得越过旧事务；旧事务完成后新代次才能 Claim；新代次出现后旧 Worker 的 Plan 覆盖被拒绝 | Plan、Run |
| R4 | 与 R3 一样，但旧事务保存 Document | 新代次不能与旧 Document 事务重叠；旧代次不能覆盖 Document | Document、Run |
| R5 | 与 R3 一样，但旧事务保存 Evidence 聚合 | 旧事务与接管串行化；后续旧代次不能重新写入 Evidence | Evidence、Run |
| R6 | Stage Journal 的 `finish_stage` 已锁 Run 行，Lease 在锁内到期，同时新 Worker 请求接管 | Journal 事务先完成、新代次后生成；接管后旧 token 不能再次 Finish | Stage Journal、Run |
| R7 | 两个 `ResearchRunExecutor.resume()` 在 Claim 之前完成**相同**的 Stage/Artifact/Checkpoint 只读证明 | 只有一个 Claim 成功、一个 Searching 尝试开始；另一个执行器不调度任务 | Safe Resume、Lease、Journal |

注意：R2/R3–R6 中直接在测试里调用底层 `claim_lease(..., allow_inflight=True)` **只是对数据库仲裁机制做锁竞争实验**，不是业务层的“允许重放”。真实程序仍然必须通过 Safe Resume 检查业务产物及 Journal。R2 特意保留旧 `STARTED`，不让测试伪造已经安全恢复。

## 特别重要的过期窗口

测试 R3–R6 故意令旧 Worker **在 Lease 有效时先成功取得行锁**，随后一直持有事务直至数据库时钟认定 Lease 到期；新 Claim 到来时等待此行锁。

**允许发生的事件顺序**：

```
旧 Worker 持有有效 Lease → 取得 Run 行锁 → Lease 在事务内到期
→ 新 Worker 请求接管（被行锁阻塞）
→ 旧业务事务提交/释放锁 → 新 Worker 完成 Claim，代次增加
→ 旧 token 的任何后续 guarded 写入被拒绝
```

这验证的是**无交叉所有权**，而不是“过期瞬间杀死已经取得锁的事务”。即：旧事务可能在墙钟过期之后提交，但在新代次获批之前已经持有独占锁。若未来要求严格的“提交时必须仍在有效期”，需另行设计事务提交前再校验或更严格的租约方案，不可用当前测试声称已具备。

另一个边界：Fencing 保护的是同一 Run 的事务边界。多个不同 Run 对同一 Task 聚合进行读改写可能还有更细的 Task 级并发控制需求，此次**没有证明跨 Run 的共享聚合数据不会丢失更新**。

## 测试隔离与安全限制

- 仅允许 `ENTERPRISE_RESEARCH_RUN_DB_TESTS=1` 与 `ENTERPRISE_RESEARCH_DUAL_WORKER_TESTS=1` 同时启用。
- 真实 PG 用例还核对 `PostgresSettings.host` 属于本地回环地址、数据库名恰好为 `enterprise_research`，以及连接的 `current_database()` 与配置一致。它们不能证明该库一定是开发数据；人工确认后才能运行。
- 每个用例独立生成 Task/Run UUID，只清理自己生成的 Task（依赖外键级联）。不执行全局 TRUNCATE、DROP DATABASE，也不终止 PostgreSQL 或 DeerFlow 进程。
- 所有异步任务在 finally 阶段完成或者取消；持锁事务有明确的退出边界；等待操作使用有限超时，避免测试悬挂。
- 测试同步是 Python `asyncio.Event` 配合真正的 PostgreSQL 不同 Session/连接。不会通过 `time.sleep()` 假扮数据库竞争。
- **R7 的 Checkpoint 观察者是受控只读 Test Double**，用于固定两次恢复证明；它不是对真实 Checkpointer 并发改写的验证。真实 PostgreSQL Checkpointer 的持久化已经在 4C/5A 做过验证，新的跨存储并发注入属于后续工作。

## WSL2 验收步骤

先在 `enterprise-research-platform` 根目录确认 Git HEAD 为 `f874e6b` 且工作区干净，然后应用本阶段补丁。此阶段不用执行 `alembic upgrade`。

```bash
PY_FILES=(
  tests/application/test_dual_worker_preclaim_barrier.py
  tests/infrastructure/test_postgres_dual_worker_races.py
)
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"

unset ENTERPRISE_RESEARCH_RUN_DB_TESTS
unset ENTERPRISE_RESEARCH_DUAL_WORKER_TESTS
uv run --no-project pytest tests/application/test_dual_worker_preclaim_barrier.py -q
uv run --no-project pytest -q
```

确认本地业务库适用于测试、迁移是 `7c3a1e9d4b62 (head)`，然后显式执行：

```bash
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_DUAL_WORKER_TESTS=1 \
uv run --no-project pytest tests/infrastructure/test_postgres_dual_worker_races.py -v -rs
```

必须看到 R1–R7 实际通过、**没有 skip**。本阶段没有使用 `ENTERPRISE_RESEARCH_GRAPH_CHECKPOINT_TEST_DSN`；只涉及业务库与模拟 Checkpoint 只读引用。

后续建议联合回归：

```bash
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
uv run --no-project pytest \
  tests/infrastructure/test_postgres_run_leases.py \
  tests/infrastructure/test_postgres_stage_journal.py \
  tests/infrastructure/test_postgres_fenced_side_effects.py \
  tests/infrastructure/test_postgres_safe_resume.py -q
```

## 简历与面试说明

可准确描述为：**“针对研究长任务设计可复现的双 Worker 同 Run 并发竞争故障矩阵，通过 PostgreSQL 行锁与 Fencing Generation，验证 Claim 的单一成功者、Journal/业务写入串行化以及过期代次写入拒绝；对恢复前后证据证明进行竞争测试。”**

在真实 PostgreSQL 跑完之前只可表述为“设计/实现测试”，不可表述为“已验证通过”。本阶段不声称实现了跨数据库原子提交、外部 LLM Exactly-once、真正多机器故障转移或量化的吞吐改善。这些需要后续 5C 压测与专门的故障实验。
