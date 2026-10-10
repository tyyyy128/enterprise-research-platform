# 3E-6D-5C-2：三种恢复策略的配对实验（受控模型）

> **性质：开发环境专用、模型对比，不是三套生产服务的 A/B 性能测试。**
>
> 本阶段只新增 Benchmark 与测试，不修改 `ResearchRunExecutor.resume()`、数据库业务表、迁移或 DeerFlow 主图。

## 1. 为什么不用 5C-1 的耗时直接比较三种策略？

5C-1 对真实 `ResearchSafeResumeInspector` / `ResearchRunExecutor` / PostgreSQL Run、Journal、Plan 进行恢复交接测量，包含生产实现的数据库访问和执行权竞争。A（Naive Retry）和 B（Checkpoint-only）在本项目中**没有可比较的生产实现**。因此将简化 A/B 的毫秒数与 5C-1 真实 C 的毫秒数相减，会造成无效比较。

5C-2 在相同的**合成 PLANNING 阶段故障窗口**中，对三种明确声明的策略模型做配对实验：

- **A — `naive_retry_model`**：故障后总是重试之前的阶段，不查询 Journal、业务产物或 Checkpoint，也不校验旧 Worker 的执行代次。
- **B — `checkpoint_only_model`**：Checkpoint 为 advanced 就跳到下一阶段，stale 就重试；Checkpoint 读取失败或身份冲突时暂停。不核验 Journal 或持久化产物，不保护陈旧 Worker 写入。
- **C — `journal_artifact_fencing_model`**：必须具有仅一次尝试且已完成的 Journal 模型证明、已保存的 Plan 产物，且 Checkpoint 可观察且无身份冲突，才交接下一阶段；否则保守暂停。旧 Worker 的写入须通过 SQL 代次条件。**这是生产恢复协议的简化示意模型，并没有调用生产 Safe Resume 或真正的 Lease Repository。**

三种策略都使用一样的 PostgreSQL **session-local TEMP tables**、一样的故障快照和一样的副作用事件类型；随机轮换策略执行顺序，避免固定策略顺序导致稳定偏差。每一策略前 `TRUNCATE` 临时表，重新灌入相同快照；所有副作用都进入临时 SQL Ledger。无任何真实 Research Task/Run/Plan 或 API/LLM 调用。

## 2. 预先声明的故障矩阵

| 窗口 | 初始业务产物 | Journal | Checkpoint | 实验意义 |
|---|---|---|---|---|
| `before_effect` | 无 | STARTED | stale | 无副作用时重试可能安全，C 仍保守暂停 |
| `effect_before_journal` | 有 | STARTED | stale | 直接重试可能造成重复副作用 |
| `effect_after_checkpoint` | 有 | STARTED | advanced | 单凭 Checkpoint 跳过未完成的 Journal |
| `completed_checkpoint_stale` | 有 | COMPLETED | stale | Journal 已完成而 Graph 滞后 |
| `completed_checkpoint_advanced` | 有 | COMPLETED | advanced | 正常的一致证明 |
| `completed_missing_artifact` | 无 | COMPLETED | advanced | Checkpoint 和 Journal 均不能替代产物证明 |
| `checkpoint_unavailable` | 有 | COMPLETED | unavailable | Checkpointer 读取失败时保守暂停 |
| `checkpoint_identity_conflict` | 有 | COMPLETED | identity_conflict | Checkpoint 与执行身份冲突 |
| `duplicate_journal_attempts` | 有 | COMPLETED（2 次尝试） | advanced | 多次 Journal 尝试不能当作唯一完成证明 |
| `stale_worker_after_takeover` | 有 | COMPLETED | advanced | 旧代次=1，接管后新代次=2，旧 Worker 再尝试写入 |

这里的“副作用”是临时事件 Ledger 中的一条 `stage` 记录；与真实搜索 API、文档写入或 LLM 调用**不等价**。`stale_worker_after_takeover` 的 Guarded SQL 使用 `WHERE generation = 1`，模拟执行代次保护；它不能替代 5B 中对生产 `save_guarded()` 的真并发验证。

## 3. 怎样判断结果？

| 指标 | 统计口径 |
|---|---|
| `duplicate_effects` | 模型 Ledger 中 `stage` 记录数量超过 1 的超额部分 |
| `missing_artifact_skips` | 跳到下一阶段、但 `stage` Ledger 仍为 0 的次数 |
| `unproven_next_dispatches` | 跳到下一阶段、但 Journal 或产物不能满足明确证明契约的次数 |
| `stale_write_accepted / attempts` | 过期代次模拟 SQL 写入成功次数 / 已明确尝试次数 |
| `proven_next_dispatched / opportunities` | 满足 Journal+产物+可观察 Checkpoint 条件时，是否交接下一阶段 |
| `trials_with_safety_violations` | 发生上述任一安全问题的试验单元数；同一单元不会重复计数 |
| `decision_ms` | 读取本策略需要的 SQL 快照并得出动作的时间，**不是端到端恢复耗时** |
| `operation_ms` | 从策略 SQL 读取到模拟副作用及最终 Ledger 核对、事务提交的耗时 |
| `workload_trials_per_second` | 所有策略单元数 / 实测批次时间，包含临时表创建、重置和清理 |

`P50/P95/P99` 使用 **nearest-rank** 定义；每种窗口只做 10 次时，不得用 P99 推断生产尾延迟。三种策略执行的 SQL 类型和次数不同，`operation_ms` 差异既包含策略判定也包含模拟副作用操作成本，不能称为“生产 C 相对 A 的性能开销百分比”。

**预期 A/B 本来就会暴露风险。** 只要 PostgreSQL 原始样本符合已声明模型，即使 A/B 有重复副作用，本次“模型实验有效性”也可通过。反之，若 C 有一次违规、模型观测偏离预定义行为、试验不完整、数据清理失败或执行错误，则实验失败，CLI 返回非零码。报告会保留 A/B 的风险计数，并且不会将其描述为某产品真实事故发生率。

## 4. 安全边界

1. 只有同时设置 `ENTERPRISE_RESEARCH_RUN_DB_TESTS=1` 和 `ENTERPRISE_RESEARCH_STRATEGY_COMPARISON=1` 才运行数据库模型实验。
2. 只允许 `ENTERPRISE_RESEARCH_DB_HOST` 为 `127.0.0.1` / `localhost` / `::1`、数据库名为 `enterprise_research`、端口为 `5432`，还会通过 `current_database()` 再确认一次。**本地地址与数据库名检查不能证明这不是生产库；操作人必须确认是开发 PostgreSQL。**
3. 使用 PostgreSQL `CREATE TEMP TABLE`、`pg_temp` 定位数据表。临时表数据仅在各自连接内可见；完成后主动 DROP，连接中断时随会话销毁。无永久 DDL，不会调用生产业务仓储的写入方法。
4. 保留文件到项目外独立目录，例如 `~/benchmarks/enterprise-research/5c-2-run01`。输出目录已存在则拒绝覆盖。
5. 运行时不会 SIGKILL、变更 Lease TTL、调用 DeerFlow 或发起任何外部网络/LLM 请求。PostgreSQL 链接仍是本地 TCP。
6. 并发参数是同时存在的**独立隔离试验组数**，不是三种策略争同一 Run 的多 Worker 测试；真正的生产双 Worker 并发安全由 5B 验证。
7. 每一组的三种策略交替采用**相同的快照**、每次使用新的 Ledger 数据；每组策略顺序依据固定 seed 随机排列。预热样本不计入测量。

## 5. 运行步骤（WSL2）

```bash
cd ~/projects/enterprise-research-platform

# 应用 5C-2 patch 后，先运行代码规范和无 DB 回归
PY_FILES=(
  src/enterprise_research/benchmarking/strategy_comparison.py
  src/enterprise_research/benchmarking/postgres_strategy_comparison.py
  src/enterprise_research/benchmarking/compare_cli.py
  tests/application/test_recovery_strategy_comparison.py
  tests/infrastructure/test_postgres_recovery_strategy_comparison.py
)
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"
uv run --no-project pytest tests/application/test_recovery_strategy_comparison.py -q
uv run --no-project pytest -q

# 确认连接的是本地企业研究开发数据库
pg_isready -h 127.0.0.1 -p 5432
uv run --no-project alembic current

# 真实数据库、小样本模型冒烟（10 种窗口 × 3 种策略 = 30 单元）
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_STRATEGY_COMPARISON=1 \
uv run --no-project pytest \
  tests/infrastructure/test_postgres_recovery_strategy_comparison.py -v -rs

# 正式采样：10 × 3 × 10 = 300 单元；外加 30 个预热单元
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_STRATEGY_COMPARISON=1 \
uv run --no-project python -m enterprise_research.benchmarking.compare_cli \
  --trials 10 --warmup 1 --concurrency 2 --seed 42 \
  --output-dir "$HOME/benchmarks/enterprise-research/5c-2-run01"

cat "$HOME/benchmarks/enterprise-research/5c-2-run01/report.md"
```

若出现 `STOP`、`FAIL`、跳过数据库冒烟用例或 PostgreSQL 报错，不提交 Git，不将报告解释为有效性能结论。

## 6. 文件职责与后续 5C-3

| 文件 | 用途 |
|---|---|
| `strategy_comparison.py` | 九个故障快照、三个明确的策略模型、独立指标核对、报告序列化 |
| `postgres_strategy_comparison.py` | 双开关环境保护、局部 TEMP 表、随机配对、PostgreSQL 副作用模拟 |
| `compare_cli.py` | 命令行参数、不可覆盖的报告、元数据和退出码 |
| `test_recovery_strategy_comparison.py` | 纯模型契约、风险数字、统计分母、报告和安全限制 |
| `test_postgres_recovery_strategy_comparison.py` | 真实 PG 上 27 个试验单元的初始端到端验收 |

5C-3 可在同样的开发数据库上重复多个独立批次、记录机器及数据库版本和资源利用率，使用置信区间评估时延波动；仍需与 5C-1 的真实生产路径数据明确分开。若要得出三套**生产实现**的公平性能比较，必须先实现并隔离三种真正的恢复执行器，使用相同的故障注入、真实业务副作用和基础设施。本阶段不做此声称。
