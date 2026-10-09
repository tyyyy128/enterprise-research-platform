# 3E-6D-5A：研究长任务故障注入与安全恢复验收

> 基线：`879a732`（3E-6D-4C 已提交）。本阶段聚焦测试，不更改运行时业务策略。默认不运行破坏性测试。

## 一、目的、已有能力与边界

当前执行顺序是：

```text
Acquire PostgreSQL Lease（获取执行租约和 Fencing Token）
  → advance_run（持久化进入阶段）
  → start_stage（Stage Journal: STARTED）
  → ResearchStageRunner（可能写业务产物 / LangGraph Checkpoint）
  → finish_stage（Stage Journal: COMPLETED）
  → next stage
```

**核心原则**：Graph Checkpoint 表示 Graph 状态已经被保存；Stage Journal 的 `COMPLETED` 也不是单独足够的业务证明。只有 Journal、对应阶段的持久业务产物以及可选的 Checkpoint 一致性对账通过后，显式 `resume()` 才可能接续下一阶段。`PENDING` 应走普通启动，`PAUSED` 和终态不可自动恢复。

这里实现的是 **故障注入的验证与度量基础**，不是跨 PostgreSQL 与 LangGraph 的分布式事务，也没有“恰好一次执行”保证。失去 Lease 的外部 API 调用，仍需结果复用、幂等键或明确的副作用防护。

## 二、5A 故障矩阵

| 编号 | 故障点 | Journal | Plan 业务产物 | Graph Checkpoint | 期望 Safe Resume | 验证方式 |
|---|---|---|---|---|---|---|
| A1 | 阶段 `STARTED` 后取消 | STARTED | 无 | 未配置 | PAUSED，不能重新执行 | 内存/协作取消 |
| A2 | Plan 已提交但 `finish_stage` 前取消 | STARTED | 有 | 未配置 | PAUSED，不能重复生成 Plan | 内存/协作取消 |
| A3 | Journal 已完成，Checkpoint Reader 故障 | COMPLETED | 有 | UNAVAILABLE | PAUSED，禁止将“无法读取”当“缺失” | 内存/异常注入 |
| P1 | `start_stage` 事务已提交后杀进程 | STARTED | 无 | ABSENT | PAUSED | PostgreSQL / 子进程 SIGKILL |
| P2 | 业务 Plan 提交后，Journal 未完成时杀进程 | STARTED | 有 | ABSENT | PAUSED | PostgreSQL / 子进程 SIGKILL |
| P3 | LangGraph Graph 返回且已写 Checkpoint，Journal 未完成时杀进程 | STARTED | 有 | PRESENT | PAUSED；Checkpoint 不是业务完成证书 | PostgreSQL + LangGraph / SIGKILL |
| P4 | Plan + Journal 已提交、Graph Checkpoint 缺失时杀进程 | COMPLETED | 有 | ABSENT | READY；租约超时后显式从 SEARCHING 开始，不重跑 PLANNING | PostgreSQL / SIGKILL |
| P5 | Journal 已完成却没有 Plan 时杀进程 | COMPLETED | 无 | ABSENT | PAUSED；不能只信 Journal | PostgreSQL / SIGKILL |

**注意 P4 的语义**：本测试通过故意跳过 Graph 调用来构造“业务已提交但 Graph 缺失”的故障窗口，验证已有对账策略；**它并不模拟一场真实的 PostgreSQL 写入失败**。P3 则真正使用 `AsyncPostgresSaver` 和顶层 `StateGraph` 写入 Checkpoint。测试均不调用任何远程 LLM / 搜索提供商。

协作取消与 `SIGKILL` 不等价：协作取消会运行 `finally`，并可能释放 Lease；`SIGKILL` 不执行 Python 的清理代码，因此需要等数据库时钟上的 Lease 自然到期。两者分开测试，避免混淆。

## 三、主要文件职责（中文）

| 文件 | 作用 |
|---|---|
| `tests/application/test_recovery_fault_injection.py` | 通过真实 `ResearchRunExecutor` 注入协作取消；验证业务写入前后被取消时 Stage Journal 保持 `STARTED`，并验证 Checkpoint 读取异常时 fail-closed（拒绝恢复）。不需要 PostgreSQL。 |
| `tests/infrastructure/_process_crash_worker.py` | **仅供测试使用的独立 Python 子进程**。从自己的 PostgreSQL 连接执行一个测试 Run 的 PLANNING，停在指定故障边界并发出 `FAULT_BARRIER` 信号；它自身不发送终止信号。 |
| `tests/infrastructure/test_postgres_process_crash_recovery.py` | 显式开启后创建随机 Test Task / Run，确认子进程提交边界后，只对这个 `Popen` 子进程发送 `SIGKILL`，使用新的 PostgreSQL 连接进行恢复检查，并清理对应业务 Task 与 Graph Thread。 |
| `docs/architecture/recovery-fault-injection.md` | 本故障矩阵、测试安全限制、执行说明和后续阶段设计。 |

不修改 `executor.py`、`safe_resume.py`、`langgraph_runtime.py` 或宿主 DeerFlow 的 Graph Runtime。故障只存在于测试进程，绝不在生产代码植入隐藏的 `kill()` / `os._exit()` 开关。

## 四、安全边界与测试前检查

1. **必须显式设置双重开关**：`ENTERPRISE_RESEARCH_RUN_DB_TESTS=1` 和 `ENTERPRISE_RESEARCH_PROCESS_CRASH_TESTS=1`。不开启时，P1–P5 必须 `SKIPPED`，绝不能执行进程终止测试。
2. **只允许本地 PostgreSQL 地址**：Business DB 与 Graph Test DB 的主机都必须是 `localhost` / `127.0.0.1` / `::1`。
3. **必须隔离数据库**：Graph DSN 数据库名限定为 `enterprise_graph_checkpoints_test`，实际 `current_database()` 也必须匹配，且不得与 Business DB 同名。Business DB 必须是已经应用 `7c3a1e9d4b62` 的开发/测试数据库。
4. **只终止自己的 Python 子进程**：测试调用 `Popen.kill()`，不会向系统服务、PostgreSQL、DeerFlow 进程组发送信号。
5. **只清理测试创建的 UUID 资源**：删除随机 Task 对应的级联数据，以及该测试唯一的 `enterprise-research:v2:` Thread。不会清空表、`DROP DATABASE`，也不会删除宿主 Checkpoint。即使测试断言失败，父进程也尝试在 `finally` 回收子进程及资源。
6. **禁止泄漏凭据**：DSN 仅从环境变量获取，绝不作为命令行参数传给子进程；故障日志输出会对原始 DSN/数据库密码做脱敏。请不要把包含密码的环境变量值贴到聊天中。

**重要限制**：如果测试 Python 主进程自身也被外部 `SIGKILL`，其 `finally` 同样无法执行，可能遗留测试数据。此时需按明确的 Test Run UUID 进行人工检查和清理，不允许直接删除整个数据库。

## 五、运行方式（WSL2，先准备已有的隔离 PostgreSQL DB）

确认本次源码基线为 `879a732`，已配置业务数据库环境变量以及：

- `ENTERPRISE_RESEARCH_GRAPH_CHECKPOINT_TEST_DSN`：指向**本机**的 `enterprise_graph_checkpoints_test`，格式 `postgresql://...`；不要发出其值。
- `ENTERPRISE_RESEARCH_DB_*`：指向已经完成 Alembic 迁移的本地业务开发测试库，**不能**指向 Graph Test DB。

首先：

```bash
cd ~/projects/enterprise-research-platform
uv run --no-project alembic current
uv run --no-project ruff check tests/application/test_recovery_fault_injection.py tests/infrastructure/_process_crash_worker.py tests/infrastructure/test_postgres_process_crash_recovery.py
uv run --no-project ruff format --check tests/application/test_recovery_fault_injection.py tests/infrastructure/_process_crash_worker.py tests/infrastructure/test_postgres_process_crash_recovery.py
uv run --no-project pytest tests/application/test_recovery_fault_injection.py -q
```

**先确认 PostgreSQL 仍在监听、本机业务库没有连接到生产环境，以及 Graph Test DSN 仍已设置。** 再单独运行真实 SIGKILL：

```bash
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_PROCESS_CRASH_TESTS=1 \
uv run --no-project pytest \
  tests/infrastructure/test_postgres_process_crash_recovery.py -v -rs
```

预期真实进程终止测试五个参数化案例全部 `PASSED`，不能为 `SKIPPED`。如果出现 `SKIPPED`，检查开关和 DSN 是否传入到同一个 Shell 会话。

最后做全量回归：

```bash
unset ENTERPRISE_RESEARCH_RUN_DB_TESTS ENTERPRISE_RESEARCH_PROCESS_CRASH_TESTS
uv run --no-project pytest -q
git diff --check
git status --short
```

暂不提交 Git，先核对测试结果和暂存路径。`-v -rs` 可以显示每个故障窗口是否真正执行，并显示跳过原因。

## 六、5A 尚未覆盖的情况与后续工作

- **3E-6D-5B：双 Worker 争抢及 Fencing**：进程未死、旧 Worker 继续运行时验证新旧代次冲突，覆盖并发、旧代次业务写入和 Checkpoint 写入交错；需要专门的并发协调测试，而不是把 SIGKILL 测试当成 Worker 竞争证明。
- **3E-6D-5C：量化可靠性**：故障点次数、拒绝不安全恢复比率、错误重复提交率、恢复成功率、恢复耗时 P50/P95。只有压测后才能写实际指标。
- **Verifying / Writing**：由于没有完整的 Run 级 Evidence Manifest 和 Report 持久化证明，即使 Journal 完成也保守 PAUSED。测试将继续保留这个安全边界，后续补全产物协议后再重新评估。
- **外部副作用不可撤销**：搜索 API、外部调用、消息投递等不受 PostgreSQL 回滚控制。幂等键、结果缓存和外部事务语义需要独立验证。
- **运行时自动恢复**：当前研究执行器是显式 `resume()` 模式，测试不宣称已经实现生产级开机自动接管、跨机故障转移或者多节点 HA。

## 七、可用于简历与面试的技术解释

本阶段的价值不是“首次发明 Checkpoint / Fencing”，而是建立基于研究业务产物的**可证伪恢复协议**：先让真实故障在独立进程中发生，再从持久化层重新读取证据。区分“保存了 Graph 状态”“确实完成了阶段”“业务产物可重用”这三种不同事实，防止利用错误的 Checkpoint 或 Journal 状态跳过/重复执行有副作用的任务。

**所有指标必须等后续故障矩阵和恢复压测运行完成后再填写；不能将测试框架设计当成已经取得的实测性能数据。**
