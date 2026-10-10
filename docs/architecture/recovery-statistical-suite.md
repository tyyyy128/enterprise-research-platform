# 3E-6D-5C-3：恢复可靠性重复实验与统计报告（中文）

> **范围**：针对已经实现的 5C-1 真实 Safe Resume 恢复交接 Benchmark、5C-2 PostgreSQL TEMP Table 三策略**合成模型**，进行有界多轮采集、完整性审计、分组统计和静态图表生成。绝不把二者的毫秒数合并成一个“策略加速比”。
>
> **变更**：新增离线统计模块、命令行编排器、纯单元测试和受保护的真实 PostgreSQL 冒烟测试；不改动生产 `ResearchRunExecutor.resume()`、5C-1/5C-2 原有算法、SQL Schema、Alembic、DeerFlow 主图或业务任务记录。

## 1. 为什么采用两个工作负载、一个批次编排器？

5C-1 是真实的 `ResearchSafeResumeInspector` 与 `ResearchRunExecutor.resume()`，通过本地开发 PostgreSQL 的 Run/Journal/Plan 来测量**下一个阶段的授权调度**、安全暂停、只读核对、行锁竞争；未调用 LLM/HTTP，没有完成整个研究任务。

5C-2 是**独立的策略示意模型**：`naive_retry_model`、`checkpoint_only_model`、`journal_artifact_fencing_model`，在相同 PLANNING 合成故障窗口中使用 PostgreSQL session-local TEMP Ledger。Guarded 模型**不等同**生产 Safe Resume；前两种“风险”是预先指定策略行为在固定故障矩阵上的后果，不是生产事故率或正式 A/B 实验。

因此 5C-3 按并发度分别汇总两类报告：不跨工作负载相减、相除或计算生产性能提升百分比。两类 Benchmark 独立启动，不共享实验 DB session；批次之间顺序执行，避免不同并发配置互相争抢资源。每个批次仍由原有 CLI 做开发库检查、受保护的数据写入和测试记录。

## 2. 文件分工

| 文件 | 作用 |
|---|---|
| `src/enterprise_research/benchmarking/statistical_suite.py` | 实验配置有界校验；重算原始 JSON 样本指标；按并发度分组；Wilson、零事件上界、批次级 Bootstrap；Markdown/CSV/静态 SVG 输出 |
| `src/enterprise_research/benchmarking/suite_cli.py` | `collect` 顺序调用现有 CLI，保存不可覆盖的 `manifest.json` 和 SHA-256；`analyze` 离线重新校验已完成实验，不连接数据库 |
| `tests/application/test_recovery_statistical_suite.py` | 使用合成的**报告文件**模拟 CLI 输出，验证完整批次、缺失/篡改、零分母、错误/不安全结果、重复执行与分析再现 |
| `tests/infrastructure/test_postgres_statistical_suite.py` | 四环境开关保护的真实 CLI 子进程冒烟：1 个 seed、并发度 1、每种场景 1 次，实际运行两种负载 |
| `docs/architecture/recovery-statistical-suite.md` | 本文件；口径、使用方式、隔离约束和技术报告边界 |

## 3. 有界实验计划与可复现配置

`SuitePlan` 包含 `seeds`（1–5 个不重复 uint32）、`concurrencies`（1–4，不重复）、`repetitions`（1–3）、`trials`（1–100）、`warmup`（0–5）、`lock_hold_ms`（10–150）。最多 **40 次子 Benchmark**，每次都创建新报告目录并保留原始 5C-1/5C-2 的 `report.json`、`samples.csv`、`report.md`。

每个 `(kind, concurrency, seed, repetition)` 对应一个独立子实验。例如下面的正式配置：`3 seeds × 2 concurrency × 2 repetitions × 2 workloads = 24` 批次，5C-1 共 **480** 个原始 Trial（4 场景 × 10 次 × 12 批次），5C-2 共 **3,600** 个模型 Policy Cell（10 窗口 × 3 策略 × 10 次 × 12 批次）。预热不计入上述数字。**相同 seed 的重复轮次不能自动看作独立同分布样本；改变 seed 仅改变测试排列，不消除真实环境中的相关性。**

每批保存 git HEAD、Python 版本和平台，Suite Manifest 保存 CPU 核心数及顺序计划。当前**没有**系统资源利用率采样、服务端 PostgreSQL 版本快照或外部依赖监控，因此不能对这些指标作出实验结论。

## 4. 不可信报告与失败处理

1. `collect` 必须同时具备 `ENTERPRISE_RESEARCH_RUN_DB_TESTS=1`、`ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK=1`、`ENTERPRISE_RESEARCH_STRATEGY_COMPARISON=1`。子 CLI 再次检查本机端口 5432、数据库名称 `enterprise_research`。**本机/库名检查不能证明不是生产库，操作人必须人为核对。**
2. 父编排器仅用 `sys.executable -m ...` 非 shell 子进程启动原有两个固定模块；单批超时 600 秒。子进程出错，不会继续产生有效 aggregate，也不回显包含内部连接信息的异常详情。
3. 每批完成后，统计器不信任 `summary` 里的数字：将 `samples` 反序列化为现有 Sample dataclass，重算所有违规计数、分母、百分位数、故障矩阵和完整性断言。若出现遗漏、重复单元、阶段处置不一致或 Guarded 模型违规，立即失败。
4. Manifest 保存三份源文件的 SHA-256；`analyze` 从 `SuitePlan` **重新构造固定子路径**，禁止目录符号链接，校验 SHA-256 和 Git HEAD 一致性。摘要的自洽性是数据完整性检查，**SHA-256 Manifest 不是对抗拥有修改权限的攻击者的数字签名**。
5. 采集输出目录必须不存在。失败留下 `complete=false` 的部分 Manifest，**不生成成功报告，不覆盖/删除已完成批次**。分析完整 Manifest 时也会执行相同的原始数据审计。
6. 报告路径选择 Git 项目之外：例如 `~/benchmarks/enterprise-research/5c-3-run01`。图表无 JS/远程资源，SVG 标题和轴说明均为 English。

## 5. 数据分析规则

### 5.1 真实 Safe Resume

对同一并发度下的多个独立批次合并**同一指标**的原始 Trial：`eligible_dispatch.decision_ms`、`pause_decision.decision_ms`、`read_only_inspection.inspect_ms`、`contended_claim_roundtrip.lock_claim_ms`，给出 nearest-rank P50/P95/P99 及准确 `n`。

- `eligible_dispatched / eligible_trials` 是合法恢复阶段调度率；不表示整个研究已完成。
- `correctly_paused / must_pause_trials` 是保守暂停正确率；暂停不算恢复执行失败。
- `unauthorized_dispatches / recovery_trials`、`stale_write_accepted / stale_write_attempts` 均给出分母；零分母输出 `N/A`。
- 每个并发度 >=5 批且 >=3 个不同 seeds，采用**整个批次为抽样单位**的 600 次固定种子 Bootstrap，计算**P50 的探索性 95% 百分位区间**；否则写明 `N/A`。没有宣称 P95/P99 的置信区间。
- 报告额外列出两个成功率的 Wilson 95% 区间，以及事件数为零时 `1 − 0.05^(1/n)` 的单侧 95% 上界。**它们只有在 Bernoulli 独立同分布前提成立时才具有相应频率学含义**；固定场景、同机器和重复种子通常不满足该前提，故是**假设条件下的敏感性计算**，不能声称“生产违规概率低于 X%”。
- 按并发度汇总同种负载的 `sum(trials) / sum(batch_duration_s)`；吞吐包含测试数据创建/清理，但不包含 CLI 启动和预热，不代表完整 Agent 任务吞吐。

### 5.2 固定故障窗口的合成模型

对每个并发度及每个策略模型分别汇总 `unsafe_trials / trials`、`duplicate_effects`、`unproven_advances`、`missing_skips`、`stale_accepted / attempts`，以及各自的决策读取和模拟 SQL 操作 P50/P95/P99。

- 这里的风险是固定策略 + 固定故障窗口的**描述性结果**；重复更多试验只是测量一致性和 SQL 时延波动，不能将 80/100 等解释成随机事故概率的可信估计。
- 5C-2 三策略操作的 SQL 不完全相同，不能从模拟毫秒数得出生产“Guarded 比 Naive 慢/快多少倍”。
- 不为模型风险率计算事故率置信区间，避免将合成结果误包装为外部真实事件。

### 5.3 图表

`analysis/recovery_dispatch_p50.svg`：各并发度的**真实** Safe Resume 合法调度 P50（ms）及样本数。

`analysis/model_unsafe_trials.svg`：固定故障矩阵下三策略的模型 Unsafe Trial 百分比。**展示规则对比，不展示生产事故概率。**

另外输出 `analysis/report.json`（结构化分析）、`report.md`（中文技术报告）、`runs.csv`（批次元数据）、`metrics.csv`（单指标完整 `n`、百分位数、可用的 P50 区间）。

## 6. WSL2 验收顺序

### 6.1 代码和隔离回归

```bash
cd ~/projects/enterprise-research-platform

PY_FILES=(
  src/enterprise_research/benchmarking/statistical_suite.py
  src/enterprise_research/benchmarking/suite_cli.py
  tests/application/test_recovery_statistical_suite.py
  tests/infrastructure/test_postgres_statistical_suite.py
)
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"
uv run --no-project pytest tests/application/test_recovery_statistical_suite.py -q
uv run --no-project pytest -q
```

### 6.2 真实数据库冒烟（四项显式开关）

**先人工确认**当前 PostgreSQL 是本地开发库且迁移已更新：

```bash
pg_isready -h 127.0.0.1 -p 5432
uv run --no-project alembic current
printf 'HOST=%s DB=%s\n' "${ENTERPRISE_RESEARCH_DB_HOST:-127.0.0.1}" "${ENTERPRISE_RESEARCH_DB_NAME:-UNSET}"

ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK=1 \
ENTERPRISE_RESEARCH_STRATEGY_COMPARISON=1 \
ENTERPRISE_RESEARCH_STATISTICAL_SUITE=1 \
uv run --no-project pytest tests/infrastructure/test_postgres_statistical_suite.py -v -rs
```

目标 1 passed、0 skipped；如果未开启四个开关，应该明确 SKIP，不允许把 skip 当成验证通过。

### 6.3 小规模采样后再做大样本

```bash
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_RECOVERY_BENCHMARK=1 \
ENTERPRISE_RESEARCH_STRATEGY_COMPARISON=1 \
uv run --no-project python -m enterprise_research.benchmarking.suite_cli collect \
  --seeds 42,43,44 --concurrencies 1,2 --repeats 2 \
  --trials 10 --warmup 1 --lock-hold-ms 25 \
  --output-dir "$HOME/benchmarks/enterprise-research/5c-3-run01"

cat "$HOME/benchmarks/enterprise-research/5c-3-run01/analysis/report.md"
```

此配置是 **24 个子 Benchmark**；初次运行也可降低为 `--seeds 42 --concurrencies 1 --repeats 1 --trials 2 --warmup 0`，验证端到端生成和清理后再扩大样本。新的正式批次必须指定不同 `--output-dir`，不能覆盖旧结果。

### 6.4 不连接数据库的重复分析

```bash
uv run --no-project python -m enterprise_research.benchmarking.suite_cli analyze \
  --suite-dir "$HOME/benchmarks/enterprise-research/5c-3-run01" \
  --output-dir "$HOME/benchmarks/enterprise-research/5c-3-analysis02"
```

分析时重新验摘要与全部原始文件哈希；输入批次只读，输出目录必须是全新的。

## 7. 验收边界

合格的 5C-3 报告必须标明：运行提交号、种子/并发/轮次、原始分母、每个指标样本量、是否经过真实 PostgreSQL、是否存在不安全重放或陈旧写入、P95/P99 的小样本限制、实验与生产任务的范围区别。没有证据时，不写出“零风险”“Exactly Once 保证”“生产成功率 100%”或“三方案生产性能优势”等断言。

更大规模的多主机、不同硬件、长时间运行、真实搜索/LLM 副作用与端到端重启时间测量，属于后续扩展，不由本阶段样本自动证明。
