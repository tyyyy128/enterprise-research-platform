# 3E-6D-4C：研究阶段 LangGraph Runtime 与安全恢复接线（第一版）

## 一、验收范围（重要）

此版本在独立的 Enterprise Research 扩展中，**可选**通过真实 `StateGraph.compile(checkpointer=...)` 和 `ainvoke()` 运行研究阶段，并把读取到的 Checkpoint 身份交给已有的 `ResearchSafeResumeInspector`。

- **已接线**：Extension Service 生命周期 → 扩展自有的异步 PostgreSQL Checkpointer → 研究阶段 LangGraph → Stage Journal → Safe Resume 的只读对账。
- **尚未接线**：DeerFlow 主 Agent 的 Graph/Checkpointer。当前上传的宿主 `ExtensionRuntimeDeps` 仅含 `app_store`、`policy`、`session_factory`、`run_evidence_reader`，**没有公开的 Checkpointer/Graph 引用**。不能通过私有 `app.state` 或猴子补丁绕过。
- **不保证**：跨数据库原子双写、Exactly Once 外部调用、自动重放失败的研究阶段、跨进程 Worker 自动恢复、完整的报告阶段业务产物核验。
- **启用条件**：必须主动配置 PostgreSQL 业务仓库、独立 Checkpointer DSN 和可选 Python 依赖；默认仍是原来的非 Graph 执行路径。

## 二、设计动机与职责边界

```text
DeerFlow Gateway
    └─ ExtensionRuntimeDeps（不公开宿主 Checkpointer）
        └─ EnterpriseResearchService
            ├─ PostgreSQL Run / Lease / Fencing （执行权）
            ├─ Stage Journal （业务阶段尝试是否完成）
            ├─ Plan / Source / Document / Evidence （业务事实及产物）
            ├─ ResearchRunExecutor （进度控制、Lease 心跳、完成与失败）
            │   └─ ResearchLangGraphStageRunner （仅在 GRAPH_BACKEND=postgres 时）
            │       └─ StateGraph（单阶段执行节点）
            │           └─ AsyncPostgresSaver（扩展自己管理连接和生命周期）
            └─ ResearchSafeResumeInspector
                ├─ Journal + Artifact （决策主依据）
                └─ LangGraphCheckpointReader （只读附加证明）
```

**为何 Graph 不能标记业务完成？** `StateGraph` 会在节点前后保存图状态；如果中途断电，数据库写入、网络调用与 Checkpoint 不可能天然处于一个跨系统事务中。Graph 只能说明其内部图进度，业务侧仍需要 Journal + Artifact 校验和 Fencing。

**为何不复用 DeerFlow 宿主的 Checkpointer？** 当前公开 Extension API 没提供持有者、释放时机或 namespace 所属协议。擅自读取宿主内部对象会产生版本耦合和生命周期争用；因此该阶段使用扩展独立 Checkpointer，同时可对接宿主将来正式公开的能力。

## 三、核心代码文件（中文释义）

| 路径（相对于仓库根目录） | 职责 |
|---|---|
| `src/enterprise_research/application/langgraph_runtime.py` | **新增**：研究阶段的 LangGraph Adapter。对每个 Stage 构造一个真实 StateGraph 节点，沿用已有业务 StageRunner；严格绑定 `task_id/run_id/stage/fencing_token`，拒绝重用已存在的同代次 Checkpoint。 |
| `src/enterprise_research/application/graph_checkpoint.py` | **既有**：Checkpoint 身份、namespace、只读 `aget_tuple()` 校验。4C 直接复用，不写宿主内部状态。 |
| `src/enterprise_research/application/checkpoint_consistency.py` | **既有**：对账状态（存在/缺失/冲突/不可用/Journal 模糊）。Checkpoint 不能代替完成证明。 |
| `src/enterprise_research/application/safe_resume.py` | **既有**：恢复前后重新核验 Journal、业务产物与 Checkpoint，状态变化则取消接管。 |
| `src/enterprise_research/application/executor.py` | **既有**：持有 Lease、写 Stage Journal、管理心跳、支持显式 `resume()`；Graph 执行仅是可插拔的 StageRunner，避免推翻原有可靠性机制。 |
| `src/enterprise_research/plugin.py` | **修改**：新增可选 PostgreSQL Graph backend，在 ExtensionService.start 时建立并初始化 Checkpointer，stop 时先停止任务再释放 Checkpointer。出错时清理连接。 |
| `pyproject.toml` | **修改**：新增 `langgraph` optional extra，默认不要求运行时安装 LangGraph。 |
| `tests/application/test_langgraph_runtime.py` | **新增**：无外部依赖的伪 Saver/Graph 故障测试，以及在安装 LangGraph 时通过真实 InMemorySaver 验证。 |
| `tests/infrastructure/test_postgres_langgraph_runtime.py` | **新增**：显式启用时对真实 PostgreSQL Journal / Artifact / Checkpointer 做断点恢复实验。需要单独的测试 Checkpointer 数据库。 |

## 四、安全执行时序

```text
Acquire Run Lease (generation N)
    ↓
Advance stage & STARTED Journal（事务＋fencing）
    ↓
构造 ResearchGraphCheckpointBinding(task_id, run_id, stage, N)
    ↓
只读检测本命名空间是否已经有 Checkpoint
    ├─ 已存在：拒绝隐式 replay，阶段标记失败/暂停核验
    └─ 不存在：创建单节点 LangGraph，并 await graph.ainvoke(config)
                     ↓
              StageRunner 执行（所有业务写入继续 fence）
                     ↓
              LangGraph Checkpoint 完成
                     ↓
              再次读取 Checkpoint，确认持久化身份
                     ↓
              Executor 在业务数据库写 Stage Journal COMPLETED
```

`thread_id` 是 `enterprise-research:v2:{task_id}:{run_id}:{stage}:generation:{N}`，而 `checkpoint_ns` 固定为根 Graph 的 `""`。LangGraph 顶层 StateGraph 会清空调用者传入的非空 `checkpoint_ns`；旧版 v1 设计会导致 Graph 已写入但 Reader 查询不到。新版把不同 Run、业务阶段和 Fencing 代次隔离在独立 Thread，并由同一个配置读写。v2 前缀避免误读原有 v1 标识。持久化 State 仅含关联 ID、阶段名、代次及 `stage_returned`，不存原始网页正文或模型密钥。

### 故障决策矩阵

| 故障窗口 | 策略 |
|---|---|
| `STARTED` Journal 后 Checkpoint 已创建，业务产物缺失 | **暂停**；禁止因为存在 checkpoint 就跳过该 Stage |
| 业务 Plan 已落库、Journal 未完成或失败 | **暂停**；保留人工对账路径，不自动重试外部副作用 |
| Journal `COMPLETED`、Plan 等产物完整，Checkpoint 缺失 | 可以继续**核验**并申请新代次的 Lease；原有 4B 规则仍适用 |
| Checkpoint 后端读取失败 | **暂停**；不可混同于“没有 Checkpoint” |
| Checkpoint namespace 或 checkpoint_id 与请求不符 | **暂停**；保护 Run/代次身份 |
| 同一代次重复调用 Graph stage runner | **拒绝**；否则 `ainvoke` 可能静默返回上一次完成的 graph 结果 |
| 旧 Worker 丢失 Lease 却尝试业务写入 | **拒绝**；由既有 PostgreSQL Fencing 机制保护 |
| 恢复检查与成功 claim Lease 之间证明发生变化 | **释放 Lease、暂停**；沿用 4B 的两次 Inspect |

> 当前策略是保守的 **fail-closed（失败关闭）**，不是简单地调用 `ainvoke(None)` 强行从 Graph 内部 checkpoint 自动续跑。以后需要更细的节点级恢复时，先把外部副作用拆成幂等、可验证的节点，并设计跨 Graph/Journal 的安全推进机制。

## 五、运行开关与安装

在项目对应 Python 环境安装扩展及额外依赖（示例；先确认当前环境）：

```bash
uv pip install -e '.[langgraph]'
```

以下环境变量需要在**可信配置文件**中提供，绝不可提交真实密钥：

```bash
ENTERPRISE_RESEARCH_REPOSITORY=postgres
ENTERPRISE_RESEARCH_GRAPH_BACKEND=postgres
ENTERPRISE_RESEARCH_GRAPH_CHECKPOINT_DSN='postgresql://USER:REDACTED@127.0.0.1:5432/enterprise_graph_checkpoints'
```

`GRAPH_BACKEND` 取值只有 `disabled`（默认）和 `postgres`。建议为 `ENTERPRISE_RESEARCH_GRAPH_CHECKPOINT_DSN` 建立扩展专属数据库账号与数据库，避免与 DeerFlow 主 Graph 共用表或配置。需要授予扩展 Checkpointer 初始化相关表的权限；`saver.setup()` 由 Extension Service 的启动流程执行。不要用产品数据库做破坏性测试。

**现有 Planner / Tavily / Retrieval / Evidence 配置不变**。Graph 模式只替换 StageRunner 的调度执行层，不会自动生成不存在的知识库或报告产物。

## 六、验证方式（按顺序）

### 1. 静态检查、默认回归

```bash
cd ~/projects/enterprise-research-platform
uv run --no-project ruff check \
  src/enterprise_research/application/__init__.py \
  src/enterprise_research/application/langgraph_runtime.py \
  src/enterprise_research/plugin.py \
  tests/application/test_langgraph_runtime.py \
  tests/infrastructure/test_postgres_langgraph_runtime.py
uv run --no-project ruff format --check \
  src/enterprise_research/application/__init__.py \
  src/enterprise_research/application/langgraph_runtime.py \
  src/enterprise_research/plugin.py \
  tests/application/test_langgraph_runtime.py \
  tests/infrastructure/test_postgres_langgraph_runtime.py
uv run --no-project pytest -q
```

### 2. LangGraph 真正的 MemorySaver 测试（需安装 extra）

```bash
uv run --no-project pytest tests/application/test_langgraph_runtime.py -q
```

确认不再跳过 `test_real_langgraph_in_memory_checkpoint_if_available`。

### 3. PostgreSQL 完整集成测试（在隔离测试 DB 中，主动开启）

测试使用现有 `ENTERPRISE_RESEARCH_DB_*` 变量指向**已迁移的开发业务数据库**，并使用**单独的测试 Checkpointer 数据库**：

```bash
export ENTERPRISE_RESEARCH_GRAPH_CHECKPOINT_TEST_DSN='postgresql://USER:REDACTED@127.0.0.1:5432/enterprise_graph_checkpoints_test'
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 uv run --no-project pytest \
  tests/infrastructure/test_postgres_langgraph_runtime.py \
  tests/infrastructure/test_postgres_checkpoint_consistency.py \
  tests/infrastructure/test_postgres_safe_resume.py -q
```

本集成测试验证：真实 Graph 写入 → STARTED 不允许恢复 → Artifact + Journal 完成允许恢复 → 关闭并重新建立 Checkpointer 连接 → 新 Worker 从**下一业务阶段**继续，不重复执行 Planning。测试不需要真实联网调用 LLM（由测试替身提供确定性业务产物）。

注意：在 WSL2 真正运行数据库及 LangGraph 测试**之前**，不能称为“生产级端到端验收通过”。

## 七、后续工作

1. 3E-6D-4C 当前版在 WSL2 完成真实 `InMemorySaver` 与 `AsyncPostgresSaver` 集成验收，检查 host 配置及重复启动关闭。
2. 3E-6D-5：多进程 Worker 接管、数据库断连、Checkpoint 读/写故障注入与恢复压测，统计重复执行风险、P95 恢复耗时、恢复成功率。
3. 扩展本身的 Evidence 产物 run-scoped manifest 与 Report 持久化，补齐 VERIFYING/WRITING 的可信恢复证明。
4. DeerFlow 若将来公开受支持的 Graph/Checkpointer Extension API，再评估是否从独立 Graph 升级为宿主统一生命周期，避免私有耦合。

## 参考（核实接口时使用）

- LangGraph Checkpointer：<https://docs.langchain.com/oss/python/langgraph/persistence>
- LangGraph 持久化：<https://docs.langchain.com/oss/python/langgraph/add-memory>
- DeerFlow 宿主依据：本次上传的 `backend/packages/extension-api/deerflow_extension_api/contracts.py`、`backend/packages/harness/deerflow/agents/factory.py`、`backend/app/gateway/app.py`。
