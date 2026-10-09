# 3E-6D-5C-1 — PostgreSQL 恢复可靠性与延迟基准（Benchmark Harness）

> 代码基线：`0fa66fb`（5B 竞态测试已提交）。**本阶段新增基准工具和测试，不改变生产恢复策略，不新增 Alembic 迁移**。本文描述的是“怎么测”，不是已经得到的实测性能结论。下一步在 WSL2 本地 PostgreSQL 上运行后，才有正式的数据报告。

## 1. 为什么要先精确定义指标

项目目前的 Safe Resume 并非“异常后无条件重试”。它依赖 `Stage Journal`、对应业务产物及可选 Graph Checkpoint 的只读对账，并在显式 `resume()` 内执行 Claim、二次对账、调度下一阶段。一个证据不足的 Run 返回 `PAUSED` 是**正确的安全行为**，而不是恢复失败。研究任务真正完成（搜索、抽取、验证和撰写全部完成）又是另外的指标：本阶段没有测量。

本 Harness 对 **PLANNING → SEARCHING 的显式恢复交接** 进行真实 PostgreSQL 微基准；使用受控、只等待的 Stage Runner，不做 LLM 调用、不运行 DeerFlow 主 Graph、不声称完成整个研究任务。5A 的真实进程 `SIGKILL`、5B 的事务争锁测试仍保持独立；5C 不把人工缩短 Lease 算作一次真实进程故障。

## 2. 工作负载矩阵

| 代号 / `scenario` | 持久化状态与注入 | 执行路径 | 期望观测 |
|---|---|---|---|
| B1 / `ready_two_workers` | PLANNING `COMPLETED` + 合法 Plan；**仅对测试 Run 注入 Lease 过期** | 两个独立 `ResearchRunExecutor.resume()` 同时试图接管，胜者进入 SEARCHING；随后用上一代 Lease 再次尝试 Plan guarded 写入 | 恰好一个下一阶段 dispatch，另一个拒绝；旧代次写入拒绝 |
| B2 / `started_with_plan` | PLANNING `STARTED` + 已提交 Plan；测试 Run Lease 过期 | `ResearchSafeResumeInspector` + `resume()` | 证据不完整，正确 `PAUSED`，无下一阶段 dispatch |
| B3 / `completed_without_plan` | PLANNING `COMPLETED` 但没有 Plan；测试 Run Lease 过期 | 同上 | 业务产物缺失，正确 `PAUSED`，无 dispatch |
| B4 / `contended_initial_claim` | 全新 PENDING Run，由独立连接先 `SELECT ... FOR UPDATE` 锁定 Run 行，再启动一次 `claim_lease` | 在持锁期间保持请求未结束，释放行锁后计时 Claim 返回 | Claim 最终成功，并记录客户端观测的**行锁竞争下 Claim 往返耗时** |

每个场景重复 `--trials N` 次，实际测量样本共 `4 × N`；每场景 `--warmup M` 次只做预热，不计入测量汇总。`--seed` 只固定场景调度列表的乱序，**不能固定 PostgreSQL/OS 的真实调度顺序**。所有 Task 和 Run 都由新 UUID 建立并在本次试验的 `finally` 删除，不会批量操作其他 Task。

**注意：** B4 的指标是“客户端 Claim 请求被受控行锁阻塞时的总往返耗时”，包含调度、数据库连接/执行和持锁等待；**不是** `pg_stat_activity` 或 `pg_stat_statements` 提供的纯 PostgreSQL `Lock wait` 指标。我们不会把它错误命名为“数据库内部锁等待毫秒”。

## 3. 指标定义与分母（严谨版）

| JSON 路径 | 定义 | 分母 / 测量范围 |
|---|---|---|
| `rates.safe_resume_dispatch_rate` | 合法 READY、恰好一次授权下一阶段调度且无错误的样本数 / READY 样本数 | 仅 B1。**不等价于完整研究任务恢复成功率** |
| `rates.correct_pause_rate` | 预期不可恢复且实际 PAUSED、零调度且无错误的样本数 / 预期 PAUSED 样本数 | B2+B3，正确暂停是成功的安全决策 |
| `rates.unauthorized_dispatches_per_recovery_trial` | 未授权阶段调度次数 / 恢复决策样本数 | B1+B2+B3；B1 超出一次的部分计为违规，B2/B3 每一次调度都计违规。**次数/试验，不必是概率** |
| `rates.accepted_stale_writes_per_attempt` | 前一代 Lease 的 guarded Plan 写入被接受次数 / 陈旧写入尝试总次数 | 只有确实发生过陈旧写入探针的场景（B1）；分母零时用 JSON `null` 而不是伪造 0% |
| `latency.eligible_dispatch` | 调用两个 `resume()` 开始到胜者 Stage Runner 实际进入 SEARCHING 的单次耗时 | B1 的成功样本，`perf_counter_ns` 单调时间；P50/P95/P99 采用 nearest-rank |
| `latency.pause_decision` | 调用 `resume()` 到返回拒绝并确认无 Dispatch | B2+B3 的正确暂停样本 |
| `latency.read_only_inspection` | 单次预先调用 `inspect()` 的只读对账耗时 | B1+B2+B3 无错误样本；与 `resume()` 内的二次对账分开 |
| `latency.contended_claim_roundtrip` | 受控行锁竞争下 `claim_lease()` 请求到返回的客户端总耗时 | B4 成功样本，不等于数据库内核纯锁等待 |
| `batch.trial_throughput_per_s_including_setup_cleanup` | 全部实测样本数 / 本批 wall-clock 秒数 | 包括测试 Task/Run 建立、持久化、探针、清理；**不是研究任务吞吐量** |
| `all_invariants_passed` | 无异常、B1 调度结果正确、B2/B3 正确暂停、B4 成功测得耗时、违规次数均为零、样本非空 | **任一失败使 CLI 返回非零状态**；即使某个百分比看似很好也不能把整个实验标记成功 |

P50/P95/P99 采用 **nearest-rank（最近秩）**，即排序后取第 `ceil(p×N)` 个样本。小于几十次的试验中 P99 往往就等于样本最大值，不能据此推断生产环境 99 分位；建议先跑小规模验收，再根据机器资源逐步增加次数并重复多轮。

## 4. 新增文件职责

| 路径 | 功能 |
|---|---|
| `src/enterprise_research/benchmarking/metrics.py` | 依赖标准库的不可变样本数据结构、严格分母与分位数计算、写入 JSON/CSV/Markdown、拒绝覆盖旧报告 |
| `src/enterprise_research/benchmarking/postgres_harness.py` | 实际 PostgreSQL Run/Stage/Plan 基准负载、并发调度、陈旧写入探针、受控行锁、隔离数据清理及环境安全校验 |
| `src/enterprise_research/benchmarking/cli.py` | 显式 opt-in CLI、负载配置、版本元数据、报告目录、失败时非零退出码 |
| `tests/application/test_recovery_benchmark_metrics.py` | 零分母、异常/违规不掩盖、百分位、小样本和不可覆盖报告目录等单位测试 |
| `tests/infrastructure/test_postgres_recovery_benchmark.py` | 需要双开关的真实 PostgreSQL `1×4` 小规模 Smoke Test |

报告路径由用户指定，生成 `report.json`（原始样本 + 统计 + 元数据）、`samples.csv`（便于 Excel/Pandas 作图）和 `report.md`（人类可读的摘要）。失败/超时的异常只在报告里保留**异常类名**，而不保留可能含数据库口令的 SQL 异常文本。每个样本同时记录本次随机生成的 `task_id`，便于特殊故障后按 UUID 人工定位剩余数据。

## 5. WSL2 验收前置与安全限制

1. 必须确认实际 Git 基线为 `0fa66fb`、应用 Patch 后工作区文件符合预期、Ruff 全通过。
2. PostgreSQL 必须为**本机开发/测试数据库**。代码双重校验 `ENTERPRISE_RESEARCH_DB_HOST` 属于 `127.0.0.1`/`localhost`/`::1`，`ENTERPRISE_RESEARCH_DB_PORT` 为 `5432`，配置与连接后的 `current_database()` 都恰好等于 `enterprise_research`。**本地地址不代表一定不是生产库；需由人工再次确认**。
3. 必须同时设置 `ENTERPRISE_RESEARCH_RUN_DB_TESTS=1` 和 `ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK=1`。缺一时 CLI 拒绝运行；PostgreSQL 测试跳过。另有 `--trials 1..100`、`--concurrency 1..4`、`--warmup 0..10`、`--lock-hold-ms 10..150` 上限，防止随意高负载。
4. 运行前确认 `uv run --no-project alembic current` 为 `7c3a1e9d4b62 (head)`，`pg_isready -h 127.0.0.1 -p 5432` 接受连接。不要贴数据库口令、连接 URL。
5. 不修改宿主 DeerFlow，不触发外部研究接口、不 `DROP DATABASE`、不 `TRUNCATE`、不自动清理任何历史 Run。仅在自身试验 Task 的范围内进行定向删除。异常处理会尝试清理，但如果整个基准进程被 `SIGKILL` 或 DB 断线，`finally` 无法确保清理成功；可根据 `task_id` 定向排查。
6. **本阶段不连接真实 Graph Checkpointer**。其独立 PostgreSQL Checkpointer 已在 4C/5A 验证，本次不量化它的读取成本。不能把 B1 当成带 Checkpointer 的端到端延迟。更不能把 B1 当成 5A 的真实 `SIGKILL` 恢复。

## 6. 分步执行（请不要一开始就跑 100 次）

```bash
cd ~/projects/enterprise-research-platform

PY_FILES=(
  src/enterprise_research/benchmarking/__init__.py
  src/enterprise_research/benchmarking/metrics.py
  src/enterprise_research/benchmarking/postgres_harness.py
  src/enterprise_research/benchmarking/cli.py
  tests/application/test_recovery_benchmark_metrics.py
  tests/infrastructure/test_postgres_recovery_benchmark.py
)
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"

unset ENTERPRISE_RESEARCH_RUN_DB_TESTS ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK
uv run --no-project pytest tests/application/test_recovery_benchmark_metrics.py -q
uv run --no-project pytest -q
```

确认开发库之后，先执行 **单次/场景的 PG 冒烟测试**：

```bash
pg_isready -h 127.0.0.1 -p 5432
uv run --no-project alembic current

ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK=1 \
uv run --no-project pytest tests/infrastructure/test_postgres_recovery_benchmark.py -v -rs
```

冒烟通过后，再执行一轮有 Warmup 的可重复采样（示例 `10×4=40` 个**测量**样本，另有每场景一次不计入的预热）：

```bash
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK=1 \
uv run --no-project python -m enterprise_research.benchmarking.cli \
  --trials 10 \
  --warmup 1 \
  --concurrency 2 \
  --lock-hold-ms 25 \
  --seed 42 \
  --output-dir "$HOME/benchmarks/enterprise-research/5c-1-run01"

cat "$HOME/benchmarks/enterprise-research/5c-1-run01/report.md"
```

报告目录建议放在项目 Git 工作区之外，避免误暂存原始运行数据。再次实验必须使用**新输出目录**，例如 `5c-1-run02`；CLI 不覆盖已有报告。可以用 `python -m json.tool "$HOME/benchmarks/enterprise-research/5c-1-run01/report.json" >/dev/null` 确认报告 JSON 正常。不需要手工执行任何危险的清库命令。

## 7. 尚不能得出的结论（留待后续 5C-2/5C-3）

- 不提供“普通重试 vs Checkpoint-only vs Journal+Artifact+Fencing”的相对加速倍数。这三种策略目前并没有在**等价副作用、同等资源约束、同样重放判断**下实现公平的真实运行基线；凭空构造数值会误导。5C-2 应先定义可复用 Side-effect Simulator 和一致的工作负载契约，再对比策略安全率和成本。
- 不包含真实外部服务幂等性、跨进程/跨机器 HA、自动调度恢复、Verifying/Writing 的完整产物证明、Graph Checkpointer 查询延迟。不能宣称 exactly-once 或 99.99% 恢复成功率。
- 当前四类场景是保守的局部场景集合，不能将零次观察到的违规推断成生产环境总体“违规概率为零”。若需要置信界，请按统计假设、样本独立性和负载覆盖先行评估。
- 报告 Markdown 的 Throughput 指 trial throughput（包括建数据和删数据），不是端到端 Agent/研究任务每秒吞吐。

**下一步**：你在 WSL2 验收 Ruff、默认回归、PG 小样本和正式采样报告后，我们再确认结果并独立提交 5C-1；随后设计 5C-2 统一对比负载与 5C-3 统计可视化。
